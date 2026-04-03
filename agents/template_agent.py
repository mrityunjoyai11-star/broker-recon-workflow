"""Agent 5 — Template Generator.

Produces a 5-sheet Excel workbook:
  1. Summary        — pipeline + reconciliation KPIs
  2. Broker Trades  — all extracted broker trades
  3. Matched        — broker vs MS matched rows (green)
  4. Mismatches     — broker vs MS mismatched rows with diff columns (red)
  5. Exceptions     — new (broker-only) + missing (MS-only) trades (amber)
"""

from __future__ import annotations

import io
from datetime import datetime

import pandas as pd

from broker_recon_flow.schemas.canonical_trade import (
    ExtractionResult, ReconciliationResult, ReconciliationMatch, TradeRecord,
)
from broker_recon_flow.utils.logger import get_logger

logger = get_logger(__name__)

BROKER_COLS = [
    "invoice_id", "invoice_date",
    "trade_id", "trade_date", "instrument", "exchange", "buy_sell",
    "quantity", "unit", "price", "delivery_start", "delivery_end",
    "counterparty", "client_account", "brokerage_rate", "brokerage_amount",
    "currency", "source_file",
]

MS_COLS = [
    "trade_id", "trade_date", "instrument", "buy_sell",
    "quantity", "price", "client_account", "brokerage_amount",
    "commission_rate", "currency", "broker_code",
]

# Fields that appear in the reconciliation differences dict and get their own
# named diff columns in the Mismatches sheet (broker value / MS value / delta).
_DIFF_FIELDS = ["quantity", "price", "brokerage_amount", "currency", "buy_sell"]


def run_template_generation(
    extraction: ExtractionResult,
    reconciliation: ReconciliationResult,
    broker_name: str | None = None,
) -> dict[str, bytes]:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_broker = (broker_name or "unknown").replace(" ", "_")
    filename = f"recon_{safe_broker}_{timestamp}.xlsx"
    xlsx_bytes = _build_workbook(extraction, reconciliation, broker_name, timestamp)
    logger.info("Generated report: %s (%d bytes)", filename, len(xlsx_bytes))
    return {filename: xlsx_bytes}


def _build_workbook(
    extraction: ExtractionResult,
    reconciliation: ReconciliationResult,
    broker_name: str | None,
    timestamp: str,
) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
        wb = writer.book

        # ── Formats ──────────────────────────────────────────────────────
        hdr = wb.add_format({"bold": True, "bg_color": "#2c5f8a", "font_color": "white", "border": 1})
        cell = wb.add_format({"border": 1})
        num = wb.add_format({"border": 1, "num_format": "#,##0.00"})
        green = wb.add_format({"border": 1, "bg_color": "#d4edda"})
        red = wb.add_format({"border": 1, "bg_color": "#f8d7da"})
        amber = wb.add_format({"border": 1, "bg_color": "#fff3cd"})
        title = wb.add_format({"bold": True, "font_size": 13, "font_color": "#2c5f8a"})

        # ── Sheet 1: Summary ─────────────────────────────────────────────
        _write_summary(writer, wb, title, hdr, cell, extraction, reconciliation, broker_name, timestamp)

        # ── Sheet 2: Broker Trades ───────────────────────────────────────
        broker_df = _trades_df(extraction.trades)
        _write_df(writer, wb, "Broker Trades", broker_df, title, hdr, cell, num,
                  f"Extracted Broker Trades — {broker_name or 'Unknown'} ({len(broker_df)} records)")

        # ── Sheet 3: Matched ─────────────────────────────────────────────
        matched_df = _reconciliation_matches_df(reconciliation.matched, "MATCH")
        _write_df(writer, wb, "Matched", matched_df, title, hdr, green, num,
                  f"Matched Trades ({len(matched_df)} records)")

        # ── Sheet 4: Mismatches ──────────────────────────────────────────
        mismatch_df = _reconciliation_matches_df(reconciliation.mismatched, "MISMATCH")
        _write_df(writer, wb, "Mismatches", mismatch_df, title, hdr, red, num,
                  f"Mismatched Trades ({len(mismatch_df)} records)")

        # ── Sheet 5: Exceptions ──────────────────────────────────────────
        exc_df = _exceptions_df(reconciliation.new_trades, reconciliation.missing_trades)
        _write_df(writer, wb, "Exceptions", exc_df, title, hdr, amber, num,
                  f"Exceptions — New/Missing ({len(exc_df)} records)")

    return buf.getvalue()


# ── Sheet writers ────────────────────────────────────────────────────────────

def _write_summary(writer, wb, title_fmt, hdr_fmt, cell_fmt,
                   extraction, reconciliation, broker_name, timestamp):
    s = reconciliation.summary
    rows = [
        ["Broker", broker_name or "Unknown"],
        ["Timestamp", timestamp],
        ["Extraction Method", extraction.extraction_method],
        ["Extraction Confidence", f"{extraction.confidence:.1%}"],
        ["Broker Trade Count", extraction.trade_count],
        ["MS Trade Count", s.get("ms_trade_count", 0)],
        ["Matched", s.get("matched_count", 0)],
        ["Mismatched", s.get("mismatched_count", 0)],
        ["New (Broker Only)", s.get("new_trades_count", 0)],
        ["Missing (MS Only)", s.get("missing_trades_count", 0)],
        ["Match Rate", s.get("match_rate", "N/A")],
        ["Broker Total Brokerage", s.get("broker_total_brokerage", 0)],
        ["MS Total Brokerage", s.get("ms_total_brokerage", 0)],
        ["Brokerage Difference", s.get("difference", 0)],
        ["Extraction Warnings", len(extraction.warnings)],
    ]
    df = pd.DataFrame(rows, columns=["Metric", "Value"])
    df.to_excel(writer, sheet_name="Summary", startrow=2, index=False)
    ws = writer.sheets["Summary"]
    ws.write(0, 0, "Reconciliation Summary", title_fmt)
    ws.write(1, 0, f"Generated: {timestamp}")
    _fmt_headers(ws, df, hdr_fmt, 2)
    ws.set_column(0, 0, 30)
    ws.set_column(1, 1, 25)


def _write_df(writer, wb, sheet_name, df, title_fmt, hdr_fmt, cell_fmt, num_fmt, title_text):
    df.to_excel(writer, sheet_name=sheet_name, startrow=2, index=False)
    ws = writer.sheets[sheet_name]
    ws.write(0, 0, title_text, title_fmt)
    _fmt_headers(ws, df, hdr_fmt, 2)
    for ci, col in enumerate(df.columns):
        max_w = max(len(str(col)), df[col].astype(str).str.len().max() if not df.empty else 0)
        ws.set_column(ci, ci, min(max_w + 4, 40))


def _fmt_headers(ws, df, hdr_fmt, row):
    for ci, col in enumerate(df.columns):
        ws.write(row, ci, col, hdr_fmt)


# ── Data builders ────────────────────────────────────────────────────────────

def _trades_df(trades: list[TradeRecord]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame(columns=BROKER_COLS)
    rows = [{c: getattr(t, c, None) for c in BROKER_COLS} for t in trades]
    return pd.DataFrame(rows)[BROKER_COLS]


def _reconciliation_matches_df(matches: list[ReconciliationMatch], status: str) -> pd.DataFrame:
    rows = []
    for m in matches:
        row: dict = {"status": status, "mismatch_reason": m.mismatch_reason, "confidence_score": m.confidence_score}
        if m.broker_trade:
            for c in BROKER_COLS:
                row[f"broker_{c}"] = getattr(m.broker_trade, c, None)
        if m.ms_trade:
            for c in MS_COLS:
                row[f"ms_{c}"] = getattr(m.ms_trade, c, None)
        # Expand differences dict into named columns so reviewers can see
        # broker value vs MS value vs numeric delta without parsing JSON.
        if m.differences:
            for field in _DIFF_FIELDS:
                if field in m.differences:
                    d = m.differences[field]
                    row[f"diff_{field}_broker"] = d.get("broker")
                    row[f"diff_{field}_ms"] = d.get("ms")
                    if "diff" in d:       # numeric delta present
                        row[f"diff_{field}_delta"] = d["diff"]
                    elif "reason" in d:
                        row[f"diff_{field}_delta"] = d["reason"]
            # Keep the raw JSON blob too for fields outside _DIFF_FIELDS
            extra = {k: v for k, v in m.differences.items() if k not in _DIFF_FIELDS}
            if extra:
                row["other_differences"] = str(extra)
        rows.append(row)
    if not rows:
        return pd.DataFrame(columns=["status"])
    return pd.DataFrame(rows)


def _exceptions_df(new_trades: list[ReconciliationMatch], missing_trades: list[ReconciliationMatch]) -> pd.DataFrame:
    rows = []
    for m in new_trades:
        row = {"exception_type": "NEW (Broker Only)", "mismatch_reason": m.mismatch_reason}
        if m.broker_trade:
            for c in BROKER_COLS:
                row[f"broker_{c}"] = getattr(m.broker_trade, c, None)
        rows.append(row)
    for m in missing_trades:
        row = {"exception_type": "MISSING (MS Only)", "mismatch_reason": m.mismatch_reason}
        if m.ms_trade:
            for c in MS_COLS:
                row[f"ms_{c}"] = getattr(m.ms_trade, c, None)
        rows.append(row)
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=["exception_type"])


# ── Parsed Trades Excel (saved on HITL approval) ───────────────────────────

def save_parsed_trades_excel(
    extraction: ExtractionResult,
    broker_name: str | None = None,
) -> tuple[bytes, str]:
    """Build a clean Excel of the HITL-approved extracted trades and return
    (bytes, filename).  Saved to data/parsed_files/ by the persist node.

    Sheets:
      1. Trades        — one row per extracted trade (all canonical fields)
      2. Warnings      — extraction warnings / quality notes
      3. Summary       — counts, method, confidence
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_broker = (broker_name or "unknown").replace(" ", "_")
    filename = f"parsed_trades_{safe_broker}_{timestamp}.xlsx"

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
        wb = writer.book
        hdr = wb.add_format({"bold": True, "bg_color": "#2c5f8a", "font_color": "white", "border": 1})
        num = wb.add_format({"border": 1, "num_format": "#,##0.00"})
        cell = wb.add_format({"border": 1})
        title_fmt = wb.add_format({"bold": True, "font_size": 13, "font_color": "#2c5f8a"})

        # Sheet 1: All trades
        trades_df = _trades_df(extraction.trades)
        # Add duplicate trade_id flag so reviewer can spot them easily
        if not trades_df.empty:
            id_counts = trades_df["trade_id"].value_counts()
            trades_df["duplicate_trade_id"] = trades_df["trade_id"].map(
                lambda x: "YES" if id_counts.get(x, 0) > 1 else ""
            )
        trades_df.to_excel(writer, sheet_name="Trades", startrow=2, index=False)
        ws = writer.sheets["Trades"]
        ws.write(0, 0, f"Parsed Trades — {broker_name or 'Unknown'} ({len(trades_df)} records)", title_fmt)
        _fmt_headers(ws, trades_df, hdr, 2)
        for ci, col in enumerate(trades_df.columns):
            max_w = max(len(str(col)), trades_df[col].astype(str).str.len().max() if not trades_df.empty else 0)
            ws.set_column(ci, ci, min(max_w + 4, 40))

        # Sheet 2: Warnings
        warn_df = pd.DataFrame({"warning": extraction.warnings or ["No warnings"]})
        warn_df.to_excel(writer, sheet_name="Warnings", startrow=2, index=False)
        ws2 = writer.sheets["Warnings"]
        ws2.write(0, 0, "Extraction Warnings", title_fmt)
        _fmt_headers(ws2, warn_df, hdr, 2)
        ws2.set_column(0, 0, 80)

        # Sheet 3: Summary
        summary_rows = [
            ["Broker", broker_name or "Unknown"],
            ["Timestamp", timestamp],
            ["Extraction Method", extraction.extraction_method],
            ["Confidence", f"{extraction.confidence:.1%}"],
            ["Total Trades", extraction.trade_count],
            ["Duplicate Trade IDs", int((trades_df["duplicate_trade_id"] == "YES").sum()) if not trades_df.empty and "duplicate_trade_id" in trades_df.columns else 0],
            ["Warnings", len(extraction.warnings or [])],
        ]
        summary_df = pd.DataFrame(summary_rows, columns=["Metric", "Value"])
        summary_df.to_excel(writer, sheet_name="Summary", startrow=2, index=False)
        ws3 = writer.sheets["Summary"]
        ws3.write(0, 0, "Extraction Summary", title_fmt)
        _fmt_headers(ws3, summary_df, hdr, 2)
        ws3.set_column(0, 0, 30)
        ws3.set_column(1, 1, 25)

    logger.info("Parsed trades Excel: %s (%d trades)", filename, len(extraction.trades))
    return buf.getvalue(), filename
