"""SIPDO Prompt Optimizer — generates broker-specific extraction prompts.

Implements a simplified SIPDO pipeline:
  1. Document Analysis — understand broker doc structure
  2. Field Decomposition — classify canonical fields as SIMPLE / COMPLEX
  3. Seed Prompt Generation — create initial extraction prompt
  4. Optimization Loop (3-5 iterations with progressive difficulty):
     - Generate synthetic variation of the broker document
     - Evaluate current prompt against the variation
     - Diagnose errors and refine prompt
     - Regression check against all previous variations
  5. Consistency Audit — verify final prompt against original document
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from broker_recon_flow.parsers.pdf_parser import PDFParser
from broker_recon_flow.parsers.excel_parser import ExcelParser
from broker_recon_flow.services.llm_service import invoke_llm, invoke_llm_json
from broker_recon_flow.utils.logger import get_logger

logger = get_logger(__name__)

# How many SIPDO iterations to run
MAX_ITERATIONS = 4

# Domain knowledge for brokerage extraction
DOMAIN_KNOWLEDGE = """
# Brokerage Statement Domain Knowledge

## Canonical Trade Fields
trade_id, trade_date, instrument, exchange, buy_sell, quantity, unit, price,
delivery_start, delivery_end, counterparty, client_account, brokerage_rate,
brokerage_amount, currency, invoice_id, invoice_date

## Common Broker Layout Patterns
- Tabular: single table with headers in row 1, one trade per row
- Multi-section: summary section + detailed trade table
- Multi-page: trade table spans multiple pages with repeated headers
- Nested: grouped by instrument/date with sub-rows

## Common Extraction Challenges
- Column headers on row 2+ (row 1 may be a title or blank)
- Merged cells spanning multiple columns
- Date formats: DD/MM/YYYY, MM/DD/YYYY, YYYY-MM-DD, DD-Mon-YY
- Number formats: 1,234.56 (US) vs 1.234,56 (EU) vs 1234.56 (raw)
- Buy/Sell indicators: B/S, Buy/Sell, BUY/SELL, Bought/Sold, Purchase/Sale
- Brokerage may need calculation from rate * notional
- Currency may be in header rather than per-row
- Some brokers use "Qty" for lots and "Volume" for units
"""

# ── System prompts for SIPDO sub-agents ──────────────────────────────────────

DOCUMENT_ANALYST_PROMPT = """You are a financial document analyst. Analyze the structure
of this broker statement and describe:
1. Table layout (row/column structure, where headers are)
2. Data types per column (dates, numbers, text, codes)
3. Any anomalies (merged cells, multi-page indicators, subtotals)
4. Key patterns (how buy/sell is indicated, date format, number format)

{domain_knowledge}

Return JSON:
{{
  "layout_type": "tabular|multi_section|multi_page|nested",
  "header_row": <integer>,
  "column_descriptions": {{"col_name": "description", ...}},
  "date_format": "detected format",
  "number_format": "US|EU|raw",
  "buy_sell_format": "detected format",
  "anomalies": ["list of anomalies"],
  "currency_location": "per_row|header|missing"
}}"""

FIELD_DECOMPOSER_PROMPT = """You are a field decomposition specialist.
Given the document analysis below, classify each canonical extraction field
as SIMPLE (direct column mapping) or COMPLEX (needs transformation/calculation).

Canonical fields: trade_id, trade_date, instrument, exchange, buy_sell, quantity,
unit, price, delivery_start, delivery_end, counterparty, client_account,
brokerage_rate, brokerage_amount, currency, invoice_id, invoice_date

Return JSON:
{{
  "simple_fields": {{"field_name": "source_column", ...}},
  "complex_fields": {{"field_name": "transformation_needed", ...}},
  "unmappable_fields": ["fields not present in document"]
}}"""

SEED_PROMPT_ARCHITECT_PROMPT = """You are a prompt architect specializing in financial data extraction.
Create an extraction prompt for a specific broker document format.

The prompt must:
1. Instruct the LLM to extract ALL trade records into the canonical schema
2. Include specific rules for this broker's format (date parsing, buy/sell normalization, etc.)
3. Handle the identified complexities (calculated fields, multi-row merges, etc.)
4. Return a JSON array of trade objects

{domain_knowledge}

Document Analysis: {analysis}
Field Decomposition: {decomposition}

Return ONLY the extraction prompt text (no wrapping JSON). The prompt should be
self-contained — when given the raw document text, it produces correct JSON output."""

SYNTHETIC_DATA_GENERATOR_PROMPT = """You are a synthetic data generator for financial documents.
Given the original broker document structure, create a REALISTIC variation that tests
the extraction prompt's robustness.

Difficulty level: {difficulty}/5
- Level 1: Reorder columns
- Level 2: Change date/number formats
- Level 3: Remove optional columns, add extra noise columns
- Level 4: Simulate multi-page breaks, add subtotal rows
- Level 5: Combine all above challenges

Original document structure:
{doc_structure}

Return a JSON object with:
{{
  "synthetic_text": "the synthetic document text/table as a string",
  "expected_trades": [list of expected trade dicts with canonical field names],
  "variation_description": "what was changed"
}}"""

EVALUATOR_PROMPT = """You are a financial data extraction evaluator.
Compare the extracted trades against the expected trades and score accuracy.

Expected trades:
{expected}

Extracted trades:
{extracted}

Score each field match across all trades. Return JSON:
{{
  "overall_accuracy": 0.0-1.0,
  "field_scores": {{"field_name": accuracy_float, ...}},
  "errors": [
    {{"trade_index": N, "field": "field_name", "expected": "...", "got": "...", "error_type": "missing|wrong_value|wrong_format"}}
  ],
  "summary": "brief description of extraction quality"
}}"""

ERROR_ANALYST_PROMPT = """You are an extraction error analyst.
Analyze WHY the extraction prompt failed on certain fields and propose targeted fixes.

Current extraction prompt:
{current_prompt}

Evaluation results:
{evaluation}

Document structure:
{doc_structure}

Return JSON:
{{
  "root_causes": ["list of root cause descriptions"],
  "fixes": [
    {{"target": "what to fix in the prompt", "fix": "specific change to make", "priority": "high|medium|low"}}
  ]
}}"""

PROMPT_REFINER_PROMPT = """You are a prompt refiner. Apply the suggested fixes to improve
the extraction prompt while preserving all existing correct behavior.

Current prompt:
{current_prompt}

Fixes to apply:
{fixes}

CRITICAL: The refined prompt must still correctly extract all previously working cases.
Return ONLY the updated prompt text (no wrapping JSON)."""


def run_optimization(
    broker_name: str,
    pdf_path: str | None = None,
    excel_path: str | None = None,
    expected_trades: list[dict] | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run the SIPDO optimization pipeline and return the result.

    Returns dict with keys: optimized_prompt, accuracy_score, iteration_count, trace
    """
    def _progress(msg: str):
        logger.info("[SIPDO] %s", msg)
        if progress_callback:
            progress_callback(f"SIPDO: {msg}")

    _progress(f"Starting optimization for broker '{broker_name}'")
    trace: list[dict] = []

    # ── Step 1: Extract document text / structure ────────────────────────
    doc_text = ""
    column_names: list[str] = []
    sample_rows = ""

    if excel_path:
        try:
            parser = ExcelParser(excel_path)
            df = parser.get_primary_table()
            if df is not None and not df.empty:
                column_names = list(df.columns)
                sample_rows = df.head(15).to_string()
                doc_text = f"Columns: {column_names}\n\n{df.to_string()}"
        except Exception as exc:
            logger.warning("Excel parse for SIPDO failed: %s", exc)

    if not doc_text and pdf_path:
        try:
            parser = PDFParser(pdf_path)
            doc_text = parser.extract_full_text()[:12000]
            tables = parser.extract_tables()
            if tables:
                column_names = list(tables[0].columns)
                sample_rows = tables[0].head(15).to_string()
        except Exception as exc:
            logger.warning("PDF parse for SIPDO failed: %s", exc)

    if not doc_text:
        _progress("No document text extracted — cannot optimize")
        return {"optimized_prompt": "", "accuracy_score": 0.0, "iteration_count": 0, "trace": []}

    doc_structure = f"Broker: {broker_name}\nColumns: {column_names}\nSample:\n{sample_rows}"

    # ── Step 2: Document Analysis ────────────────────────────────────────
    _progress("Step 1/5: Analyzing document structure...")
    analysis = invoke_llm_json(
        DOCUMENT_ANALYST_PROMPT.format(domain_knowledge=DOMAIN_KNOWLEDGE),
        f"Broker: {broker_name}\n\n{doc_text[:8000]}",
    )
    trace.append({"step": "document_analysis", "result": analysis})

    # ── Step 3: Field Decomposition ──────────────────────────────────────
    _progress("Step 2/5: Decomposing extraction fields...")
    decomposition = invoke_llm_json(
        FIELD_DECOMPOSER_PROMPT,
        f"Document analysis:\n{_safe_json(analysis)}\n\nColumns: {column_names}",
    )
    trace.append({"step": "field_decomposition", "result": decomposition})

    # ── Step 4: Seed Prompt Generation ───────────────────────────────────
    _progress("Step 3/5: Generating seed extraction prompt...")
    seed_prompt = invoke_llm(
        SEED_PROMPT_ARCHITECT_PROMPT.format(
            domain_knowledge=DOMAIN_KNOWLEDGE,
            analysis=_safe_json(analysis),
            decomposition=_safe_json(decomposition),
        ),
        f"Broker: {broker_name}\nSample data:\n{sample_rows}",
    )
    trace.append({"step": "seed_prompt", "prompt_length": len(seed_prompt)})

    # ── Step 5: Optimization Loop ────────────────────────────────────────
    current_prompt = seed_prompt
    best_accuracy = 0.0
    best_prompt = seed_prompt
    all_synthetic_cases: list[dict] = []

    for iteration in range(1, MAX_ITERATIONS + 1):
        difficulty = min(iteration, 5)
        _progress(f"Step 4/5: Optimization iteration {iteration}/{MAX_ITERATIONS} (difficulty={difficulty})...")

        # Generate synthetic variation
        synthetic = invoke_llm_json(
            SYNTHETIC_DATA_GENERATOR_PROMPT.format(
                difficulty=difficulty,
                doc_structure=doc_structure,
            ),
            f"Original columns: {column_names}\nSample:\n{sample_rows}",
        )

        if synthetic.get("parse_error"):
            _progress(f"  Iteration {iteration}: synthetic generation failed, skipping")
            trace.append({"step": f"iteration_{iteration}", "error": "synthetic_gen_failed"})
            continue

        synthetic_text = synthetic.get("synthetic_text", "")
        expected = synthetic.get("expected_trades", [])
        all_synthetic_cases.append(synthetic)

        # Run current prompt against synthetic data
        extracted_raw = invoke_llm_json(current_prompt, f"Broker: {broker_name}\n\n{synthetic_text}")
        extracted_trades = extracted_raw.get("trades", [])

        # Evaluate
        evaluation = invoke_llm_json(
            EVALUATOR_PROMPT.format(
                expected=_safe_json(expected),
                extracted=_safe_json(extracted_trades),
            ),
            f"Variation: {synthetic.get('variation_description', 'unknown')}",
        )

        accuracy = float(evaluation.get("overall_accuracy", 0.0))
        _progress(f"  Iteration {iteration}: accuracy={accuracy:.0%}")
        trace.append({
            "step": f"iteration_{iteration}",
            "accuracy": accuracy,
            "variation": synthetic.get("variation_description"),
            "error_count": len(evaluation.get("errors", [])),
        })

        if accuracy > best_accuracy:
            best_accuracy = accuracy
            best_prompt = current_prompt

        # If accuracy is already very high, skip further refinement
        if accuracy >= 0.95:
            _progress(f"  Iteration {iteration}: accuracy ≥95%, stopping early")
            break

        # Error analysis + prompt refinement
        if evaluation.get("errors"):
            error_analysis = invoke_llm_json(
                ERROR_ANALYST_PROMPT.format(
                    current_prompt=current_prompt[:4000],
                    evaluation=_safe_json(evaluation),
                    doc_structure=doc_structure,
                ),
                "Analyze extraction errors and suggest prompt fixes.",
            )

            fixes = error_analysis.get("fixes", [])
            if fixes:
                refined = invoke_llm(
                    PROMPT_REFINER_PROMPT.format(
                        current_prompt=current_prompt,
                        fixes=_safe_json(fixes),
                    ),
                    "Apply fixes while preserving existing correct behavior.",
                )
                if refined and len(refined) > 100:
                    current_prompt = refined

        # Regression check on previous cases
        if iteration > 1 and all_synthetic_cases:
            prev = all_synthetic_cases[-2] if len(all_synthetic_cases) >= 2 else all_synthetic_cases[0]
            prev_text = prev.get("synthetic_text", "")
            if prev_text:
                regress_raw = invoke_llm_json(current_prompt, f"Broker: {broker_name}\n\n{prev_text}")
                regress_eval = invoke_llm_json(
                    EVALUATOR_PROMPT.format(
                        expected=_safe_json(prev.get("expected_trades", [])),
                        extracted=_safe_json(regress_raw.get("trades", [])),
                    ),
                    "Regression check on previous variation.",
                )
                regress_acc = float(regress_eval.get("overall_accuracy", 0.0))
                if regress_acc < best_accuracy - 0.1:
                    _progress(f"  Iteration {iteration}: regression detected ({regress_acc:.0%}), reverting prompt")
                    current_prompt = best_prompt

    # ── Step 6: Consistency Audit ────────────────────────────────────────
    _progress("Step 5/5: Final consistency audit...")
    audit_result = invoke_llm_json(
        best_prompt,
        f"Broker: {broker_name}\n\n{doc_text[:10000]}",
    )
    audit_trades = audit_result.get("trades", [])

    # If we have expected trades (from HITL approval), evaluate against them
    if expected_trades:
        final_eval = invoke_llm_json(
            EVALUATOR_PROMPT.format(
                expected=_safe_json(expected_trades),
                extracted=_safe_json(audit_trades),
            ),
            "Final consistency audit against ground truth.",
        )
        best_accuracy = max(best_accuracy, float(final_eval.get("overall_accuracy", 0.0)))

    trace.append({"step": "consistency_audit", "final_trade_count": len(audit_trades)})
    _progress(f"Optimization complete: accuracy={best_accuracy:.0%}, iterations={min(MAX_ITERATIONS, len(trace) - 3)}")

    return {
        "optimized_prompt": best_prompt,
        "accuracy_score": best_accuracy,
        "iteration_count": MAX_ITERATIONS,
        "trace": trace,
    }


def _safe_json(obj: Any) -> str:
    """Convert to JSON string for prompt injection, with truncation."""
    import json
    try:
        text = json.dumps(obj, default=str, indent=2)
        return text[:6000]
    except Exception:
        return str(obj)[:6000]
