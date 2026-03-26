"""Agent 3 — Field Extraction Agent.

4-tier extraction strategy:
  Tier 1: YAML template (known broker)
  Tier 2: TemplateCache DB (HITL-approved auto-learned mapping)
  Tier 3: Fuzzy column match (rapidfuzz, no LLM)
  Tier 4: LLM column mapping / full-text extraction (fallback)

Result is cached after HITL approval (persist_agent writes to TemplateCache).
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from broker_recon_flow.config import get_agent_config
from broker_recon_flow.parsers.pdf_parser import PDFParser
from broker_recon_flow.parsers.excel_parser import ExcelParser
from broker_recon_flow.parsers.template_parser import load_broker_template, dataframe_to_trades
from broker_recon_flow.schemas.canonical_trade import ExtractionResult, TradeRecord
from broker_recon_flow.services.column_matcher import build_column_mapping, get_unmatched_columns
from broker_recon_flow.services.llm_service import invoke_llm_json
from broker_recon_flow.utils.logger import get_logger

logger = get_logger(__name__)

LLM_COLUMN_MAP_PROMPT = """You are a financial data extraction specialist.
Map the given raw column names to the canonical brokerage schema fields:
trade_id, trade_date, instrument, exchange, buy_sell, quantity, unit, price,
delivery_start, delivery_end, counterparty, client_account, brokerage_rate,
brokerage_amount, currency, invoice_id, invoice_date

Return ONLY a JSON object:
{
  "column_mapping": {"Raw Column": "canonical_field", ...},
  "confidence": 0.0-1.0,
  "unmapped_columns": ["columns that don't map to any canonical field"]
}"""

LLM_FULL_EXTRACT_PROMPT = """You are a financial data extraction specialist.
Extract ALL trade records from the following brokerage document text.

Canonical fields: trade_id, trade_date, instrument, exchange, buy_sell, quantity,
unit, price, delivery_start, delivery_end, counterparty, client_account,
brokerage_rate, brokerage_amount, currency, invoice_id, invoice_date

CRITICAL RULES:
- Extract EVERY row as a separate trade — never merge or deduplicate
- Numeric fields must be numbers, not strings
- Normalize buy_sell: B/Buy/Bought → "BUY", S/Sell/Sold → "SELL"
- Dates in YYYY-MM-DD format where possible
- Use "ROW-N" as trade_id if no unique ID exists

Return ONLY a JSON object:
{
  "trades": [{...}, ...],
  "invoice_id": "...",
  "confidence": 0.0-1.0
}"""


def run_extraction(
    pdf_path: str | None,
    excel_path: str | None,
    template_type: str | None,
    broker_name: str | None = None,
    invoice_id: str | None = None,
    cached_column_mapping: dict | None = None,
    db_session=None,
    sipdo_prompt: str | None = None,
) -> tuple[ExtractionResult, dict | None]:
    """Run 5-tier extraction. Returns (ExtractionResult, column_mapping_used).

    Tiers:
      1. YAML template
      2. TemplateCache DB (HITL-approved)
      3. Fuzzy column match (rapidfuzz)
      4. SIPDO-optimized prompt (passed in or cache lookup)
      5. Generic LLM fallback
    """
    logger.info("Extraction: template=%s, broker=%s", template_type, broker_name)
    cfg = get_agent_config("extraction")
    threshold = cfg.get("fuzzy_match_threshold", 0.70)

    # Tier 1: Load YAML template
    template = None
    if template_type:
        try:
            template = load_broker_template(template_type)
        except FileNotFoundError:
            logger.warning("Template '%s' not found", template_type)

    trades: list[TradeRecord] = []
    warnings: list[str] = []
    extraction_method = "template"
    column_mapping_used: dict | None = None

    # Choose primary source file (PDF preferred for broker statements)
    primary_path = pdf_path or excel_path
    primary_type = "pdf" if pdf_path else "excel"

    if template:
        # Tier 1: YAML template extraction
        t, w = _extract_with_template(primary_path, template, primary_type, broker_name, invoice_id)
        trades, warnings, extraction_method = t, w, "template"
        column_mapping_used = template.get("column_mappings")
    elif cached_column_mapping:
        # Tier 2: Cached template from DB
        t, w = _extract_with_cached_mapping(primary_path, primary_type, cached_column_mapping, broker_name, invoice_id)
        trades, warnings, extraction_method = t, w, "cached_template"
        column_mapping_used = cached_column_mapping
    else:
        # Tier 3 + 4 + 5: Fuzzy then SIPDO then LLM
        t, w, method, mapping = _extract_fuzzy_or_llm(
            primary_path, primary_type, broker_name, invoice_id, threshold, cfg,
            sipdo_prompt=sipdo_prompt,
        )
        trades, warnings, extraction_method = t, w, method
        column_mapping_used = mapping

    # If PDF had no trades and we also have Excel, try Excel
    if not trades and pdf_path and excel_path:
        logger.info("PDF extraction empty, trying Excel fallback")
        if template:
            t, w = _extract_with_template(excel_path, template, "excel", broker_name, invoice_id)
        else:
            t, w, extraction_method, mapping = _extract_fuzzy_or_llm(
                excel_path, "excel", broker_name, invoice_id, threshold, cfg,
                sipdo_prompt=sipdo_prompt,
            )
            column_mapping_used = mapping
        trades = t
        warnings.extend(w)

    confidence = 0.9 if extraction_method == "template" else (0.85 if "cached" in extraction_method else 0.70)
    if not trades:
        confidence = 0.0
        warnings.append("No trades extracted")

    result = ExtractionResult(
        trades=trades,
        trade_count=len(trades),
        extraction_method=extraction_method,
        confidence=confidence,
        warnings=warnings,
    )
    logger.info("Extraction: %d trades via %s", len(trades), extraction_method)
    return result, column_mapping_used


# ── Tier 1: YAML Template ───────────────────────────────────────────────────

def _extract_with_template(
    file_path: str, template: dict, source_type: str,
    broker_name: str | None, invoice_id: str | None,
) -> tuple[list[TradeRecord], list[str]]:
    warnings: list[str] = []
    if source_type == "pdf":
        parser = PDFParser(file_path)
        tables = parser.extract_tables()
        if not tables:
            warnings.append("No tables found in PDF")
            return [], warnings
        all_trades: list[TradeRecord] = []
        for tbl in tables:
            all_trades.extend(dataframe_to_trades(tbl, template, file_path, "pdf", broker_name, invoice_id))
        return all_trades, warnings
    else:
        parser = ExcelParser(file_path)
        primary = parser.get_primary_table()
        if primary is None:
            warnings.append("No data table in Excel")
            return [], warnings
        return dataframe_to_trades(primary, template, file_path, "excel", broker_name, invoice_id), warnings


# ── Tier 2: Cached Mapping ──────────────────────────────────────────────────

def _extract_with_cached_mapping(
    file_path: str, source_type: str, column_mapping: dict,
    broker_name: str | None, invoice_id: str | None,
) -> tuple[list[TradeRecord], list[str]]:
    synthetic_template = {
        "broker_name": broker_name,
        "column_mappings": column_mapping,
        "value_rules": {"buy_sell": {r"B|Buy|BUY": "BUY", r"S|Sell|SELL": "SELL"}, "currency": {"default": "USD"}},
    }
    return _extract_with_template(file_path, synthetic_template, source_type, broker_name, invoice_id)


# ── Tier 3 + 4 + 5: Fuzzy then SIPDO then LLM ──────────────────────────────

def _extract_fuzzy_or_llm(
    file_path: str, source_type: str,
    broker_name: str | None, invoice_id: str | None,
    threshold: float, cfg: dict,
    sipdo_prompt: str | None = None,
) -> tuple[list[TradeRecord], list[str], str, dict | None]:
    warnings: list[str] = []

    # Get primary DataFrame
    if source_type == "pdf":
        parser = PDFParser(file_path)
        tables = parser.extract_tables()
        df = tables[0] if tables else None
    else:
        parser = ExcelParser(file_path)
        df = parser.get_primary_table()

    if df is not None and not df.empty:
        raw_cols = list(df.columns)
        mapping = build_column_mapping(raw_cols, threshold=threshold)
        unmatched = get_unmatched_columns(raw_cols, threshold=threshold)

        coverage = len(mapping) / max(len(raw_cols), 1)
        if coverage >= 0.5:
            # Tier 3: Fuzzy match covers enough columns
            logger.info("Fuzzy match: %d/%d columns mapped (coverage=%.0f%%)", len(mapping), len(raw_cols), coverage * 100)
            if unmatched:
                warnings.append(f"Fuzzy: {len(unmatched)} columns unmatched: {unmatched}")
            synthetic = {
                "broker_name": broker_name,
                "column_mappings": mapping,
                "value_rules": {"buy_sell": {r"B|Buy|BUY": "BUY", r"S|Sell|SELL": "SELL"}, "currency": {"default": "USD"}},
            }
            trades = dataframe_to_trades(df, synthetic, file_path, source_type, broker_name, invoice_id)
            if trades:
                return trades, warnings, "fuzzy_match", mapping

        # Tier 4: SIPDO-optimized prompt (passed in from optimizer or cache)
        if sipdo_prompt:
            logger.info("Using SIPDO-optimized prompt for extraction")
            col_info = f"Broker: {broker_name or 'Unknown'}\nColumns: {list(df.columns)}\nData:\n{df.to_string()}"
            trades = _sipdo_extract(sipdo_prompt, col_info, file_path, source_type, broker_name, invoice_id)
            if trades:
                return trades, warnings, "sipdo_optimized", None

        # If no SIPDO prompt passed in, check cache
        if not sipdo_prompt and broker_name:
            try:
                from broker_recon_flow.services.prompt_cache import get_cached_prompt
                cached_sipdo = get_cached_prompt(broker_name)
                if cached_sipdo:
                    logger.info("SIPDO cache hit for broker=%s", broker_name)
                    col_info = f"Broker: {broker_name}\nColumns: {list(df.columns)}\nData:\n{df.to_string()}"
                    trades = _sipdo_extract(cached_sipdo, col_info, file_path, source_type, broker_name, invoice_id)
                    if trades:
                        return trades, warnings, "sipdo_cached", None
            except Exception as exc:
                logger.warning("SIPDO cache lookup failed: %s", exc)

        # Tier 5a: Generic LLM column mapping on the DataFrame
        if cfg.get("use_llm_fallback", True):
            trades, llm_mapping = _llm_map_and_extract(df, file_path, source_type, broker_name, invoice_id)
            if trades:
                return trades, warnings, "llm_column_map", llm_mapping

    # Tier 5b: LLM full-text extraction
    if cfg.get("use_llm_fallback", True):
        if source_type == "pdf":
            if not isinstance(parser, PDFParser):
                parser = PDFParser(file_path)
            text = parser.extract_full_text()
        else:
            if not isinstance(parser, ExcelParser):
                parser = ExcelParser(file_path)
            sheets = parser.read_all_sheets()
            text = "\n\n".join(f"Sheet: {n}\n{df.to_string()}" for n, df in sheets.items())

        if text.strip():
            trades = _llm_full_extract(text, file_path, source_type, broker_name, invoice_id)
            if trades:
                return trades, warnings, "llm_full_extract", None

    warnings.append("All extraction tiers exhausted with no results")
    return [], warnings, "failed", None


def _sipdo_extract(
    sipdo_prompt: str, context: str, source_file: str, source_type: str,
    broker_name: str | None, invoice_id: str | None,
) -> list[TradeRecord]:
    """Run a SIPDO-optimized prompt to extract trades."""
    result = invoke_llm_json(sipdo_prompt, context, max_tokens=16000)
    if result.get("parse_error"):
        return []
    trades: list[TradeRecord] = []
    for raw in result.get("trades", []):
        try:
            trades.append(TradeRecord(
                invoice_id=result.get("invoice_id") or invoice_id,
                broker_name=broker_name,
                source_file=source_file,
                source_type=source_type,
                trade_id=raw.get("trade_id"),
                trade_date=str(raw["trade_date"]) if raw.get("trade_date") else None,
                instrument=raw.get("instrument"),
                exchange=raw.get("exchange"),
                buy_sell=raw.get("buy_sell"),
                quantity=_flt(raw.get("quantity")),
                unit=raw.get("unit"),
                price=_flt(raw.get("price")),
                delivery_start=str(raw["delivery_start"]) if raw.get("delivery_start") else None,
                delivery_end=str(raw["delivery_end"]) if raw.get("delivery_end") else None,
                counterparty=raw.get("counterparty"),
                client_account=raw.get("client_account"),
                brokerage_rate=_flt(raw.get("brokerage_rate")),
                brokerage_amount=_flt(raw.get("brokerage_amount")),
                currency=raw.get("currency"),
            ))
        except Exception as exc:
            logger.warning("Skipping SIPDO trade row: %s", exc)
    return trades


def _llm_map_and_extract(
    df: pd.DataFrame, source_file: str, source_type: str,
    broker_name: str | None, invoice_id: str | None,
) -> tuple[list[TradeRecord], dict | None]:
    col_info = f"Broker: {broker_name or 'Unknown'}\nColumns: {list(df.columns)}\nSample:\n{df.head(10).to_string()}"
    result = invoke_llm_json(LLM_COLUMN_MAP_PROMPT, col_info)
    if result.get("parse_error") or not result.get("column_mapping"):
        return [], None
    llm_mapping = result["column_mapping"]
    synthetic = {
        "broker_name": broker_name,
        "column_mappings": llm_mapping,
        "value_rules": {"buy_sell": {r"B|Buy|BUY": "BUY", r"S|Sell|SELL": "SELL"}, "currency": {"default": "USD"}},
    }
    return dataframe_to_trades(df, synthetic, source_file, source_type, broker_name, invoice_id), llm_mapping


def _llm_full_extract(
    text: str, source_file: str, source_type: str,
    broker_name: str | None, invoice_id: str | None,
) -> list[TradeRecord]:
    context = f"Broker: {broker_name or 'Unknown'}\n\n{text[:12000]}"
    result = invoke_llm_json(LLM_FULL_EXTRACT_PROMPT, context, max_tokens=16000)
    if result.get("parse_error"):
        return []
    trades: list[TradeRecord] = []
    for raw in result.get("trades", []):
        try:
            trades.append(TradeRecord(
                invoice_id=result.get("invoice_id") or invoice_id,
                broker_name=broker_name,
                source_file=source_file,
                source_type=source_type,
                trade_id=raw.get("trade_id"),
                trade_date=str(raw["trade_date"]) if raw.get("trade_date") else None,
                instrument=raw.get("instrument"),
                exchange=raw.get("exchange"),
                buy_sell=raw.get("buy_sell"),
                quantity=_flt(raw.get("quantity")),
                unit=raw.get("unit"),
                price=_flt(raw.get("price")),
                delivery_start=str(raw["delivery_start"]) if raw.get("delivery_start") else None,
                delivery_end=str(raw["delivery_end"]) if raw.get("delivery_end") else None,
                counterparty=raw.get("counterparty"),
                client_account=raw.get("client_account"),
                brokerage_rate=_flt(raw.get("brokerage_rate")),
                brokerage_amount=_flt(raw.get("brokerage_amount")),
                currency=raw.get("currency"),
            ))
        except Exception as exc:
            logger.warning("Skipping LLM trade row: %s", exc)
    return trades


def _flt(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
