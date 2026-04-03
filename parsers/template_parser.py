"""Template Parser — YAML-driven column mapping and trade extraction."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from broker_recon_flow.schemas.canonical_trade import TradeRecord
from broker_recon_flow.utils.logger import get_logger

logger = get_logger(__name__)

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


def load_broker_template(template_name: str) -> dict:
    path = TEMPLATES_DIR / f"{template_name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Broker template not found: {path}")
    with open(path) as f:
        template = yaml.safe_load(f)
    logger.info("Loaded broker template: %s", template_name)
    return template


def list_available_templates(flow_type: str | None = None) -> list[str]:
    """Return template names, optionally filtered by flow_type compatibility."""
    templates = [p.stem for p in TEMPLATES_DIR.glob("*.yaml") if not p.stem.startswith("_")]
    if flow_type is None:
        return templates
    compatible = []
    for name in templates:
        try:
            tmpl = load_broker_template(name)
            allowed = tmpl.get("flow_types")  # None/missing = any flow type
            if allowed is None or flow_type in allowed:
                compatible.append(name)
        except Exception:
            compatible.append(name)  # if we can't read it, include it
    return compatible


def map_columns(df: pd.DataFrame, column_mappings: dict[str, str]) -> pd.DataFrame:
    rename_map: dict[str, str] = {}
    df_cols_lower = {str(c).strip().lower(): str(c).strip() for c in df.columns}

    for broker_col, canonical_col in column_mappings.items():
        bk = broker_col.strip().lower()
        if bk in df_cols_lower:
            rename_map[df_cols_lower[bk]] = canonical_col
        else:
            # Partial containment match
            for dc_lower, dc_orig in df_cols_lower.items():
                if bk in dc_lower or dc_lower in bk:
                    if dc_orig not in rename_map:
                        rename_map[dc_orig] = canonical_col
                        break

    logger.info("Column mapping: %d/%d columns mapped", len(rename_map), len(df.columns))
    return df.rename(columns=rename_map)


def clean_value(value: Any, field: str, value_rules: dict) -> Any:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None

    value_str = str(value).strip()

    if field == "buy_sell" and "buy_sell" in value_rules:
        for pattern, replacement in value_rules["buy_sell"].items():
            if re.fullmatch(pattern, value_str, re.IGNORECASE):
                return replacement
        return value_str.upper()

    if field in ("quantity", "price", "brokerage_rate", "brokerage_amount"):
        cleaned = re.sub(r"[,$€£\s]", "", value_str)
        if cleaned.startswith("(") and cleaned.endswith(")"):
            cleaned = "-" + cleaned[1:-1]
        try:
            return float(cleaned)
        except ValueError:
            return None

    if field == "currency":
        if value_str:
            return value_str.upper()
        return value_rules.get("currency", {}).get("default")

    return value_str


def dataframe_to_trades(
    df: pd.DataFrame,
    template: dict,
    source_file: str,
    source_type: str,
    broker_name: str | None = None,
    invoice_id: str | None = None,
) -> list[TradeRecord]:
    mapped_df = map_columns(df, template.get("column_mappings", {}))
    value_rules = template.get("value_rules", {})
    canonical_fields = set(TradeRecord.model_fields.keys())
    trades: list[TradeRecord] = []

    # Skip patterns from template (e.g. "Total", "Subtotal" rows)
    skip_patterns = [re.compile(p, re.IGNORECASE) for p in template.get("skip_patterns", [])]

    for idx, row in mapped_df.iterrows():
        # Check row skip patterns: if any cell matches, skip the row
        row_text = " ".join(str(v) for v in row.values if pd.notna(v))
        if any(p.search(row_text) for p in skip_patterns):
            continue

        trade_data: dict[str, Any] = {
            "broker_name": broker_name or template.get("broker_name"),
            "invoice_id": invoice_id,
            "source_file": source_file,
            "source_type": source_type,
            "raw_row": {str(k): str(v) for k, v in row.items() if pd.notna(v)},
        }

        for col in mapped_df.columns:
            if col in canonical_fields:
                trade_data[col] = clean_value(row[col], col, value_rules)

        # Skip structurally empty rows
        if not any(trade_data.get(f) for f in ["trade_id", "trade_date", "instrument", "quantity"]):
            continue

        try:
            trades.append(TradeRecord(**trade_data))
        except Exception as exc:
            logger.warning("Skipping row %s: %s", idx, exc)

    logger.info("Converted %d trades from %s", len(trades), source_file)
    return trades
