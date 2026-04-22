"""Agent 3 — Field Extraction Agent.

5-tier extraction strategy:
  Tier 1: YAML template (known broker)
  Tier 2: TemplateCache DB (HITL-approved auto-learned mapping)
  Tier 3: Fuzzy column match on combined pdfplumber tables (no LLM)
  Tier 4: LLM column-mapping on the combined DataFrame (1–2 LLM calls)
  Tier 5: Concurrent page-by-page LLM fallback (10 workers, no timeout risk)

PDF primary strategy — why this is efficient:
  pdfplumber extracts ALL tables from ALL pages (already proven: 177 tables in
  ~20s for TFS).  We fingerprint every table's column schema, group by that
  fingerprint, concat the dominant group into one combined DataFrame (~250 rows),
  then run the same Tier 3/4 pipeline on it.  That means ~250 trades in 1–2 LLM
  calls instead of 177 sequential calls.

  Only if table-concat + column-mapping fails do we fall back to Tier 5:
  concurrent page-by-page LLM extraction (ThreadPoolExecutor with 10 workers),
  which runs all pages in parallel without a sequential timeout.

Result is cached after HITL approval (persist_agent writes to TemplateCache).
"""

from __future__ import annotations

import math
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
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

# Maximum chars per LLM chunk for the text-based fallback path.
_CHUNK_CHARS = 30_000
# Overlap between text chunks so a trade split at a boundary isn't lost.
_CHUNK_OVERLAP = 500
# Concurrent workers for the parallel page-fallback (Tier 5).
_PDF_WORKERS = 10
# Minimum rows a pdfplumber table must have to be included in the schema group.
_MIN_TABLE_ROWS = 2

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

LLM_FULL_EXTRACT_PROMPT = """You are a specialist at reading raw PDF text of brokerage invoices and
extracting every trade row.

The text below is from ONE page of a broker invoice PDF.  The page may contain
one or more tables with trade details or no trades at all (e.g. cover pages,
summary pages, T&C pages).  The tables are represented as whitespace-aligned or
pipe-separated columns in the raw text.

Your job:
1. Identify ALL rows that represent individual trade / deal records.
2. For EACH trade row, extract as many of the canonical fields below as you can
   find.  Leave a field as null if it is not present.
3. Skip header rows, sub-total rows, grand-total rows, blank rows, and
   non-trade narrative text.
4. If the page has NO trade rows at all, return {"trades": [], "confidence": 1.0}.

Canonical fields (use these exact names in your output):
  trade_id, trade_date, instrument, exchange, buy_sell, quantity, unit, price,
  delivery_start, delivery_end, counterparty, client_account, brokerage_rate,
  brokerage_amount, currency, invoice_id, invoice_date

Formatting rules:
- Numeric fields (quantity, price, brokerage_rate, brokerage_amount) must be
  numbers, not strings.  Remove any thousand separators.
- Normalize buy_sell to "BUY" or "SELL".
- Dates in YYYY-MM-DD format where possible.
- If no explicit trade_id, use "ROW-<n>" where n is the row position on page.
- If a field value spans multiple raw-text lines due to wrapping, merge them.

Return ONLY valid JSON (no markdown fences, no commentary):
{
  "trades": [ {"trade_id": ..., "trade_date": ..., ...}, ... ],
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
      5. Generic LLM fallback (chunked for large PDFs)
    """
    logger.info("Extraction: template=%s, broker=%s", template_type, broker_name)
    cfg = get_agent_config("extraction")
    threshold = cfg.get("fuzzy_match_threshold", 0.70)

    # ── Get expected row count from Excel (used as sanity check) ─────────
    excel_row_count = 0
    if excel_path:
        try:
            ep = ExcelParser(excel_path)
            excel_primary = ep.get_primary_table()
            if excel_primary is not None:
                excel_row_count = len(excel_primary)
                logger.info("Excel reference row count: %d", excel_row_count)
        except Exception as exc:
            logger.warning("Unable to read Excel for row count: %s", exc)

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
            excel_row_count=excel_row_count,
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
                excel_row_count=excel_row_count,
            )
            column_mapping_used = mapping
        trades = t
        warnings.extend(w)

    # ── Excel cross-check ────────────────────────────────────────────────
    if excel_row_count and trades:
        ratio = len(trades) / excel_row_count
        if ratio < 0.5:
            warnings.append(
                f"Extraction coverage low: {len(trades)}/{excel_row_count} "
                f"trades extracted vs Excel rows ({ratio:.0%})"
            )
            logger.warning("Low extraction coverage: %d/%d (%.0f%%)", len(trades), excel_row_count, ratio * 100)

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
    excel_row_count: int = 0,
) -> tuple[list[TradeRecord], list[str], str, dict | None]:
    warnings: list[str] = []

    # ── PDF path ────────────────────────────────────────────────────────
    if source_type == "pdf":
        parser = PDFParser(file_path)

        # Extract ALL pdfplumber tables from ALL pages, then concat the ones
        # that share the same column structure into one combined DataFrame.
        tables = parser.extract_tables()
        combined_df = _concat_pdf_tables_by_schema(tables, broker_name)

        # Track the best result from table-based tiers so Tier 5 can compare
        _table_best: tuple[list[TradeRecord], list[str], str, dict | None] | None = None

        if combined_df is not None and not combined_df.empty:
            raw_cols = list(combined_df.columns)
            mapping = build_column_mapping(raw_cols, threshold=threshold)
            unmatched = get_unmatched_columns(raw_cols, threshold=threshold)
            coverage = len(mapping) / max(len(raw_cols), 1)

            # Tier 3: fuzzy match covers ≥50% of canonical fields — free, no LLM
            if coverage >= 0.5:
                logger.info(
                    "Tier 3 fuzzy match on combined PDF tables: %d/%d cols (%.0f%%)",
                    len(mapping), len(raw_cols), coverage * 100,
                )
                if unmatched:
                    warnings.append(f"Fuzzy: {len(unmatched)} columns unmatched: {unmatched}")
                synthetic = {
                    "broker_name": broker_name,
                    "column_mappings": mapping,
                    "value_rules": {"buy_sell": {r"B|Buy|BUY": "BUY", r"S|Sell|SELL": "SELL"}, "currency": {"default": "USD"}},
                }
                trades = dataframe_to_trades(combined_df, synthetic, file_path, source_type, broker_name, invoice_id)
                if trades and _is_extraction_adequate(len(trades), len(combined_df), excel_row_count):
                    return trades, warnings, "fuzzy_match", mapping
                elif trades:
                    logger.info(
                        "Tier 3 fuzzy produced %d trades but inadequate (combined_df=%d rows, excel_ref=%d); continuing to higher tiers",
                        len(trades), len(combined_df), excel_row_count,
                    )
                    _table_best = (trades, warnings[:], "fuzzy_match", mapping)

            # Tier 4a: LLM column-mapping on the combined DataFrame (1 LLM call
            # on headers + 10-row sample, then apply the mapping to all ~250 rows)
            if cfg.get("use_llm_fallback", True):
                logger.info("Tier 4 LLM column-mapping on combined PDF tables (%d rows)", len(combined_df))
                trades, llm_mapping = _llm_map_and_extract(
                    combined_df, file_path, source_type, broker_name, invoice_id,
                )
                if trades and _is_extraction_adequate(len(trades), len(combined_df), excel_row_count):
                    return trades, warnings, "llm_column_map", llm_mapping
                elif trades and (not _table_best or len(trades) > len(_table_best[0])):
                    _table_best = (trades, warnings[:], "llm_column_map", llm_mapping)

            # Tier 4b: SIPDO chunked on the combined DataFrame if a SIPDO
            # prompt is available (trades in proper tabular form, cheap chunks)
            sipdo = sipdo_prompt
            if not sipdo and broker_name:
                try:
                    from broker_recon_flow.services.prompt_cache import get_cached_prompt
                    sipdo = get_cached_prompt(broker_name, pdf_path=file_path)
                    if sipdo:
                        logger.info("SIPDO cache hit for broker=%s", broker_name)
                except Exception as exc:
                    logger.warning("SIPDO cache lookup failed: %s", exc)
            if sipdo:
                logger.info("Tier 4b SIPDO chunked on combined PDF tables")
                trades = _sipdo_chunked_extract(sipdo, combined_df, file_path, source_type, broker_name, invoice_id)
                if trades and _is_extraction_adequate(len(trades), len(combined_df), excel_row_count):
                    return trades, warnings, "sipdo_table_extract", None
                elif trades and (not _table_best or len(trades) > len(_table_best[0])):
                    _table_best = (trades, warnings[:], "sipdo_table_extract", None)

        # Tier 5: concurrent page-by-page LLM fallback — table tiers produced
        # no results or inadequate results. Run all pages in parallel with
        # _PDF_WORKERS workers so we never block sequentially on 177 calls.
        if cfg.get("use_llm_fallback", True):
            prompt = sipdo_prompt
            method = "sipdo_concurrent_page"
            if not prompt and broker_name:
                try:
                    from broker_recon_flow.services.prompt_cache import get_cached_prompt
                    prompt = get_cached_prompt(broker_name, pdf_path=file_path)
                    if prompt:
                        logger.info("SIPDO cache hit (concurrent fallback) for broker=%s", broker_name)
                except Exception as exc:
                    logger.warning("SIPDO cache lookup failed: %s", exc)
            if not prompt:
                prompt = LLM_FULL_EXTRACT_PROMPT
                method = "llm_concurrent_page"

            logger.info(
                "Tier 5 concurrent PDF extraction (%s, %d workers)",
                method, _PDF_WORKERS,
            )
            trades = _llm_concurrent_pdf_extract(parser, prompt, file_path, broker_name, invoice_id)
            if trades:
                # If we also have a table-based fallback, return whichever got more trades
                if _table_best and len(_table_best[0]) > len(trades):
                    logger.info(
                        "Tier 5 produced %d trades, table fallback had %d — using table result",
                        len(trades), len(_table_best[0]),
                    )
                    return _table_best
                return trades, warnings, method, None

        # Return table-based fallback if Tier 5 produced nothing
        if _table_best and _table_best[0]:
            logger.info("Tier 5 failed, returning table-based fallback (%d trades)", len(_table_best[0]))
            return _table_best

        warnings.append("All extraction tiers exhausted with no results")
        return [], warnings, "failed", None

    # ── Non-PDF (Excel) path ─────────────────────────────────────────────
    parser = ExcelParser(file_path)
    df = parser.get_primary_table()

    if df is not None and not df.empty:
        raw_cols = list(df.columns)
        mapping = build_column_mapping(raw_cols, threshold=threshold)
        unmatched = get_unmatched_columns(raw_cols, threshold=threshold)

        coverage = len(mapping) / max(len(raw_cols), 1)
        if coverage >= 0.5:
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

        # SIPDO on DataFrame (Excel only)
        if sipdo_prompt:
            logger.info("Using SIPDO-optimized prompt for Excel extraction")
            trades = _sipdo_chunked_extract(sipdo_prompt, df, file_path, source_type, broker_name, invoice_id)
            if trades:
                return trades, warnings, "sipdo_optimized", None
        if not sipdo_prompt and broker_name:
            try:
                from broker_recon_flow.services.prompt_cache import get_cached_prompt
                cached_sipdo = get_cached_prompt(broker_name)  # Excel: no pdf_path
                if cached_sipdo:
                    logger.info("SIPDO cache hit for broker=%s", broker_name)
                    trades = _sipdo_chunked_extract(cached_sipdo, df, file_path, source_type, broker_name, invoice_id)
                    if trades:
                        return trades, warnings, "sipdo_cached", None
            except Exception as exc:
                logger.warning("SIPDO cache lookup failed: %s", exc)

        # Tier 5a: LLM column mapping on the DataFrame
        if cfg.get("use_llm_fallback", True):
            trades, llm_mapping = _llm_map_and_extract(df, file_path, source_type, broker_name, invoice_id)
            if trades:
                return trades, warnings, "llm_column_map", llm_mapping

    # Tier 5b: LLM chunked text extraction (Excel fallback)
    if cfg.get("use_llm_fallback", True):
        if not isinstance(parser, ExcelParser):
            parser = ExcelParser(file_path)
        sheets = parser.read_all_sheets()
        text = "\n\n".join(f"Sheet: {n}\n{d.to_string()}" for n, d in sheets.items())
        trades = _llm_chunked_text_extract(text, file_path, source_type, broker_name, invoice_id)
        if trades:
            return trades, warnings, "llm_chunked_extract", None

    warnings.append("All extraction tiers exhausted with no results")
    return [], warnings, "failed", None


def _sipdo_extract(
    sipdo_prompt: str, context: str, source_file: str, source_type: str,
    broker_name: str | None, invoice_id: str | None,
) -> list[TradeRecord]:
    """Run a SIPDO-optimized prompt to extract trades from a single chunk."""
    result = invoke_llm_json(sipdo_prompt, context, max_tokens=16000)
    return _parse_llm_trade_result(result, source_file, source_type, broker_name, invoice_id)


def _sipdo_chunked_extract(
    sipdo_prompt: str, df: pd.DataFrame, source_file: str, source_type: str,
    broker_name: str | None, invoice_id: str | None,
) -> list[TradeRecord]:
    """Run SIPDO extraction in chunks over a DataFrame to handle large docs."""
    total_rows = len(df)
    if total_rows == 0:
        return []

    # For small DataFrames, single call is fine
    df_text = df.to_string()
    header = f"Broker: {broker_name or 'Unknown'}\nColumns: {list(df.columns)}\n"

    if len(df_text) <= _CHUNK_CHARS:
        context = f"{header}Data:\n{df_text}"
        return _sipdo_extract(sipdo_prompt, context, source_file, source_type, broker_name, invoice_id)

    # Chunk by rows - estimate chars per row
    chars_per_row = max(len(df_text) // total_rows, 1)
    rows_per_chunk = max(_CHUNK_CHARS // chars_per_row, 10)
    n_chunks = math.ceil(total_rows / rows_per_chunk)
    logger.info("SIPDO chunked extraction: %d rows in %d chunks (%d rows/chunk)", total_rows, n_chunks, rows_per_chunk)

    all_trades: list[TradeRecord] = []
    for i in range(n_chunks):
        start = i * rows_per_chunk
        end = min(start + rows_per_chunk, total_rows)
        chunk_df = df.iloc[start:end]
        context = f"{header}Data (rows {start+1}-{end} of {total_rows}):\n{chunk_df.to_string()}"
        logger.info("SIPDO chunk %d/%d: rows %d-%d", i + 1, n_chunks, start + 1, end)
        chunk_trades = _sipdo_extract(sipdo_prompt, context, source_file, source_type, broker_name, invoice_id)
        all_trades.extend(chunk_trades)
        logger.info("SIPDO chunk %d/%d: extracted %d trades (running total: %d)", i + 1, n_chunks, len(chunk_trades), len(all_trades))

    return all_trades


def _is_extraction_adequate(trade_count: int, combined_df_rows: int, excel_row_count: int) -> bool:
    """Check if extracted trade count is adequate to accept as final result.

    Returns True if extraction is good enough to return immediately.
    Returns False if we should continue trying higher tiers.
    """
    if trade_count == 0:
        return False
    # If we have an Excel reference, require at least 20% coverage and ≥ 3 trades
    if excel_row_count > 0:
        return trade_count >= max(3, int(0.2 * excel_row_count))
    # No Excel reference — accept if ≥ 3 trades or ≥ 30% of available rows
    return trade_count >= max(3, int(0.3 * combined_df_rows))


def _concat_pdf_tables_by_schema(
    tables: list[pd.DataFrame],
    broker_name: str | None,
) -> pd.DataFrame | None:
    """Group pdfplumber tables by column-schema fingerprint and concat the
    best group into a single combined DataFrame.

    Strategy:
      1. Normalise column names (strip, lower) and build a frozen tuple as the
         fingerprint.  Tables that are too small (_MIN_TABLE_ROWS) are dropped.
      2. Group tables by fingerprint.
      3. Score each group by canonical field coverage (fuzzy match) × row count.
         The group whose columns best match canonical trade fields is selected,
         not just the group with the most tables.
      4. Concat that group → one DataFrame with all trade rows.

    Returns None if no usable tables are found.
    """
    if not tables:
        return None

    groups: dict[tuple, list[pd.DataFrame]] = defaultdict(list)
    for df in tables:
        if len(df) < _MIN_TABLE_ROWS:
            continue
        # Normalise: strip whitespace, lowercase, replace newlines
        norm_cols = tuple(
            str(c).strip().lower().replace("\n", " ") for c in df.columns
        )
        groups[norm_cols].append(df)

    if not groups:
        logger.warning("No usable pdfplumber tables found (all below %d rows)", _MIN_TABLE_ROWS)
        return None

    # Score each group: canonical field coverage is primary, row count is tiebreaker.
    # This ensures we pick the table with trade data, not just the most-repeated table.
    best_schema = None
    best_group = None
    best_score = -1.0

    for schema, group in groups.items():
        col_names = [str(c) for c in schema]
        mapping = build_column_mapping(col_names)
        canonical_coverage = len(mapping) / max(len(col_names), 1)
        total_rows = sum(len(df) for df in group)
        # Primary: canonical coverage (0-1), secondary: row count (scaled down)
        score = canonical_coverage * 100.0 + total_rows * 0.01
        logger.debug(
            "Table group schema=%d cols, %d tables, %d rows, canonical_coverage=%.0f%%, score=%.2f",
            len(schema), len(group), total_rows, canonical_coverage * 100, score,
        )
        if score > best_score:
            best_score = score
            best_schema = schema
            best_group = group

    dominant_schema, dominant_group = best_schema, best_group
    n_schemas = len(groups)
    n_tables = len(dominant_group)
    logger.info(
        "PDF table schema groups: %d unique schemas; dominant has %d tables "
        "(%d columns) → broker=%s",
        n_schemas, n_tables, len(dominant_schema), broker_name or "Unknown",
    )

    combined = pd.concat(dominant_group, ignore_index=True)
    # Drop rows that are entirely NaN or duplicated headers (pdfplumber
    # sometimes re-emits the header row as a data row on each page)
    if dominant_schema:
        # If a row's values match the column headers exactly it's a repeated
        # header — drop it
        header_vals = list(dominant_schema)
        mask = combined.apply(
            lambda row: [str(v).strip().lower() for v in row.values] == header_vals,
            axis=1,
        )
        if mask.any():
            combined = combined[~mask].reset_index(drop=True)
            logger.info("Dropped %d repeated header rows from combined DataFrame", mask.sum())

    combined = combined.dropna(how="all").reset_index(drop=True)
    logger.info(
        "Combined PDF DataFrame: %d rows × %d cols from %d tables",
        len(combined), len(combined.columns), n_tables,
    )
    return combined


def _llm_concurrent_pdf_extract(
    parser: PDFParser, prompt: str, source_file: str,
    broker_name: str | None, invoice_id: str | None,
) -> list[TradeRecord]:
    """Extract trades from a PDF by running LLM calls for all pages in parallel.

    Uses _PDF_WORKERS concurrent threads so wall-clock time is
    roughly (n_pages / _PDF_WORKERS) × single_call_latency.
    For 177 pages with 10 workers that is ~18 parallel batches instead of
    177 sequential calls, and no single blocking timeout.
    """
    pages = parser.extract_pages_text()
    if not pages:
        return []

    n_pages = len(pages)
    logger.info(
        "Tier 5 concurrent PDF extraction: %d pages, %d workers",
        n_pages, _PDF_WORKERS,
    )

    def _call_page(page_idx: int, page_text: str) -> tuple[int, list[TradeRecord]]:
        if not page_text or not page_text.strip():
            return page_idx, []
        context = (
            f"Broker: {broker_name or 'Unknown'}\n"
            f"Page {page_idx + 1} of {n_pages}\n\n"
            f"{page_text}"
        )
        result = invoke_llm_json(prompt, context, max_tokens=16000)
        return page_idx, _parse_llm_trade_result(result, source_file, "pdf", broker_name, invoice_id)

    # Collect results keyed by page index so we can reconstruct order
    results: dict[int, list[TradeRecord]] = {}
    with ThreadPoolExecutor(max_workers=_PDF_WORKERS) as executor:
        futures = {
            executor.submit(_call_page, i, page_text): i
            for i, page_text in enumerate(pages)
        }
        for future in as_completed(futures):
            try:
                page_idx, page_trades = future.result()
                results[page_idx] = page_trades
                if page_trades:
                    logger.info(
                        "Page %d/%d: %d trades",
                        page_idx + 1, n_pages, len(page_trades),
                    )
            except Exception as exc:
                page_idx = futures[future]
                logger.warning("Page %d LLM call failed: %s", page_idx + 1, exc)
                results[page_idx] = []

    # Reassemble in page order and deduplicate
    all_trades: list[TradeRecord] = []
    for i in range(n_pages):
        all_trades.extend(results.get(i, []))

    logger.info(
        "Concurrent PDF extraction complete: %d trades from %d pages",
        len(all_trades), n_pages,
    )
    return _deduplicate_trades(all_trades)


def _llm_chunked_text_extract(
    text: str, source_file: str, source_type: str,
    broker_name: str | None, invoice_id: str | None,
) -> list[TradeRecord]:
    """Extract trades from text by splitting into character-based chunks."""
    if not text.strip():
        return []

    total_len = len(text)
    if total_len <= _CHUNK_CHARS:
        context = f"Broker: {broker_name or 'Unknown'}\n\n{text}"
        result = invoke_llm_json(LLM_FULL_EXTRACT_PROMPT, context, max_tokens=16000)
        return _parse_llm_trade_result(result, source_file, source_type, broker_name, invoice_id)

    # Split on line boundaries to avoid cutting mid-row
    step = _CHUNK_CHARS - _CHUNK_OVERLAP
    n_chunks = math.ceil(total_len / step)
    logger.info("LLM chunked text extraction: %d chars in ~%d chunks", total_len, n_chunks)

    all_trades: list[TradeRecord] = []
    pos = 0
    chunk_idx = 0
    while pos < total_len:
        end = min(pos + _CHUNK_CHARS, total_len)
        # Snap to next newline to avoid splitting mid-line
        if end < total_len:
            nl = text.find("\n", end)
            if nl != -1 and nl - end < 500:
                end = nl + 1

        chunk = text[pos:end]
        context = (
            f"Broker: {broker_name or 'Unknown'}\n"
            f"Text chunk {chunk_idx + 1} (chars {pos + 1}-{end} of {total_len})\n\n"
            f"{chunk}"
        )
        chunk_idx += 1
        logger.info("LLM text chunk %d: chars %d-%d (%d chars)", chunk_idx, pos + 1, end, len(chunk))
        result = invoke_llm_json(LLM_FULL_EXTRACT_PROMPT, context, max_tokens=16000)
        chunk_trades = _parse_llm_trade_result(result, source_file, source_type, broker_name, invoice_id)
        all_trades.extend(chunk_trades)
        logger.info("LLM text chunk %d: extracted %d trades (running total: %d)", chunk_idx, len(chunk_trades), len(all_trades))

        pos = end - _CHUNK_OVERLAP if end < total_len else total_len

    return _deduplicate_trades(all_trades)


def _parse_llm_trade_result(
    result: Any, source_file: str, source_type: str,
    broker_name: str | None, invoice_id: str | None,
) -> list[TradeRecord]:
    """Parse an LLM JSON response into TradeRecord objects."""
    if isinstance(result, list):
        raw_trades = result
        top_invoice_id = invoice_id
    elif isinstance(result, dict):
        if result.get("parse_error"):
            return []
        raw_trades = result.get("trades", [])
        top_invoice_id = result.get("invoice_id") or invoice_id
    else:
        return []

    trades: list[TradeRecord] = []
    for raw in raw_trades:
        if not isinstance(raw, dict):
            continue
        try:
            trades.append(TradeRecord(
                invoice_id=top_invoice_id,
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
            logger.warning("Skipping trade row: %s", exc)
    return trades


def _deduplicate_trades(trades: list[TradeRecord]) -> list[TradeRecord]:
    """Remove duplicate trades that may appear due to chunk overlap.

    Also warns if the same trade_id appears more than once with different data
    (genuine data quality issue vs. overlap duplicate).
    """
    seen_fp: set[str] = set()
    seen_tid: dict[str, int] = {}   # trade_id → count of distinct fingerprints
    unique: list[TradeRecord] = []
    for t in trades:
        # Full fingerprint — catches true duplicates from chunk overlap
        fp = f"{t.trade_id}|{t.trade_date}|{t.instrument}|{t.quantity}|{t.price}|{t.buy_sell}"
        if fp in seen_fp:
            continue
        seen_fp.add(fp)
        unique.append(t)
        # Track trade_id occurrences separately to detect genuine conflicts
        if t.trade_id:
            seen_tid[t.trade_id] = seen_tid.get(t.trade_id, 0) + 1

    if len(trades) != len(unique):
        logger.info("Deduplication: %d → %d trades", len(trades), len(unique))

    # Warn on trade_id conflicts (same id, different content — not just overlap)
    duplicated_ids = [tid for tid, cnt in seen_tid.items() if cnt > 1]
    if duplicated_ids:
        logger.warning(
            "Duplicate trade_ids detected (%d ids appear >1 time — possible "
            "page-number-based ids like ROW-1 reused across pages): %s",
            len(duplicated_ids), duplicated_ids[:10],
        )
    return unique


def _llm_map_and_extract(
    df: pd.DataFrame, source_file: str, source_type: str,
    broker_name: str | None, invoice_id: str | None,
) -> tuple[list[TradeRecord], dict | None]:
    col_info = f"Broker: {broker_name or 'Unknown'}\nColumns: {list(df.columns)}\nSample:\n{df.head(10).to_string()}"
    result = invoke_llm_json(LLM_COLUMN_MAP_PROMPT, col_info)
    if not isinstance(result, dict) or result.get("parse_error") or not result.get("column_mapping"):
        return [], None
    llm_mapping = result["column_mapping"]
    synthetic = {
        "broker_name": broker_name,
        "column_mappings": llm_mapping,
        "value_rules": {"buy_sell": {r"B|Buy|BUY": "BUY", r"S|Sell|SELL": "SELL"}, "currency": {"default": "USD"}},
    }
    return dataframe_to_trades(df, synthetic, source_file, source_type, broker_name, invoice_id), llm_mapping


def _flt(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
