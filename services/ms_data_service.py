"""MS Data Service — Receivables AND Payables.

Loads MS receivables and payables Excel files on first use and provides
fast lookup by trade_id, composite key (date + instrument + account), and
full scan.  Each flow type is indexed separately.

The MS data is READ-ONLY. We never write back to the source files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd

from broker_recon_flow.config import get_ms_data_config
from broker_recon_flow.schemas.canonical_trade import MSTradeRecord, FlowType
from broker_recon_flow.utils.logger import get_logger

logger = get_logger(__name__)

# ── Per-flow-type singleton state ────────────────────────────────────────────
_datasets: dict[str, dict] = {}   # key = flow_type value → {"df", "tid_idx", "comp_idx", "cache"}

# Canonical column name aliases: map whatever MS uses → standard name
_MS_COLUMN_ALIASES: dict[str, str] = {
    # trade_id variants
    "trade id": "trade_id",
    "tradeid": "trade_id",
    "deal id": "trade_id",
    "deal no": "trade_id",
    "ref": "trade_id",
    "reference": "trade_id",
    "order id": "trade_id",
    # trade_date variants
    "trade date": "trade_date",
    "tradedate": "trade_date",
    "date": "trade_date",
    "deal date": "trade_date",
    # instrument / product
    "instrument": "instrument",
    "product": "instrument",
    "product description": "instrument",
    "product code": "instrument",
    "exchange contract code": "instrument",
    "product group name": "instrument",
    "commodity": "instrument",
    "security": "instrument",
    "ticker": "instrument",
    # buy/sell
    "buy sell": "buy_sell",
    "buysell": "buy_sell",
    "buy/sell": "buy_sell",
    "side": "buy_sell",
    "direction": "buy_sell",
    "b/s": "buy_sell",
    # quantity
    "quantity": "quantity",
    "qty": "quantity",
    "volume": "quantity",
    "lots": "quantity",
    # price
    "price": "price",
    "unit price": "price",
    "trade price": "price",
    "strike price": "price",
    # client / account
    "client account": "client_account",
    "client account number": "client_account",
    "client account no": "client_account",
    "account": "client_account",
    "client": "client_account",
    "client name": "client_account",
    "sub account name": "client_account",
    "sub account code": "client_account",
    "fund": "client_account",
    "portfolio": "client_account",
    # brokerage
    "brokerage amount": "brokerage_amount",
    "brokerage": "brokerage_amount",
    "commission": "brokerage_amount",
    "commission amount": "brokerage_amount",
    "fee": "brokerage_amount",
    # commission rate
    "commission rate": "commission_rate",
    "brokerage rate": "commission_rate",
    "rate": "commission_rate",
    "rate case": "commission_rate",
    # currency
    "currency": "currency",
    "ccy": "currency",
    # broker code
    "broker code": "broker_code",
    "exec broker code": "broker_code",
    "broker": "broker_code",
    "broker id": "broker_code",
    "broker name": "broker_code",
    "master broker name": "broker_code",
}


def _normalize_ms_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename MS Excel columns to canonical names where aliases exist.

    When multiple raw columns map to the same canonical name, keep only the
    first non-empty one to avoid shadowing.
    """
    rename_map = {}
    seen_canonical: set[str] = set()
    for col in df.columns:
        key = col.strip().lower()
        canonical = _MS_COLUMN_ALIASES.get(key)
        if canonical and canonical not in seen_canonical:
            rename_map[col] = canonical
            seen_canonical.add(canonical)
    if rename_map:
        # Only keep columns we're renaming + columns that weren't renamed
        cols_to_drop = [
            c for c in df.columns
            if c.strip().lower() in _MS_COLUMN_ALIASES
            and c not in rename_map
        ]
        if cols_to_drop:
            df = df.drop(columns=cols_to_drop)
        df = df.rename(columns=rename_map)
        logger.debug("MS column renames: %s (dropped %d duplicate-mapped cols)", rename_map, len(cols_to_drop))
    return df


def _row_to_ms_trade(row: dict, flow_type: str = FlowType.RECEIVABLE.value) -> MSTradeRecord:
    return MSTradeRecord(
        flow_type=flow_type,
        trade_id=str(row.get("trade_id", "")) or None,
        trade_date=str(row.get("trade_date", "")) or None,
        instrument=str(row.get("instrument", "")) or None,
        buy_sell=str(row.get("buy_sell", "")) or None,
        quantity=_safe_float(row.get("quantity")),
        price=_safe_float(row.get("price")),
        client_account=str(row.get("client_account", "")) or None,
        brokerage_amount=_safe_float(row.get("brokerage_amount")),
        commission_rate=_safe_float(row.get("commission_rate")),
        currency=str(row.get("currency", "")) or None,
        broker_code=str(row.get("broker_code", "")) or None,
        raw_row=row,
    )


def _safe_float(val) -> Optional[float]:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _resolve_file_path(flow_type: str) -> Path:
    """Return the absolute path for the MS data Excel for the given flow type."""
    cfg = get_ms_data_config()
    base = Path(__file__).parent.parent

    if flow_type == FlowType.PAYABLE.value:
        raw = cfg.get("payables_file", "")
    else:
        # Default / receivable — also support legacy "file_path" key
        raw = cfg.get("receivables_file", "") or cfg.get("file_path", "")

    return (base / raw).resolve()


def load_ms_data(flow_type: str = FlowType.RECEIVABLE.value, force_reload: bool = False) -> pd.DataFrame:
    """Load MS Excel for the given flow type (singleton per type). Returns normalized DataFrame."""
    global _datasets

    ds = _datasets.get(flow_type)
    if ds is not None and not force_reload:
        return ds["df"]

    file_path = _resolve_file_path(flow_type)

    if not file_path.exists():
        logger.warning("MS %s data file not found: %s. Continuing with empty dataset.", flow_type, file_path)
        _datasets[flow_type] = {"df": pd.DataFrame(), "tid_idx": {}, "comp_idx": {}, "cache": []}
        return _datasets[flow_type]["df"]

    logger.info("Loading MS %s data from %s", flow_type, file_path)
    try:
        df = pd.read_excel(file_path, dtype=str)
    except Exception as exc:
        logger.error("Failed to load MS %s data: %s", flow_type, exc)
        _datasets[flow_type] = {"df": pd.DataFrame(), "tid_idx": {}, "comp_idx": {}, "cache": []}
        return _datasets[flow_type]["df"]

    df = _normalize_ms_columns(df)
    df = df.fillna("")

    # Build indexes
    tid_idx: dict[str, list[dict]] = {}
    comp_idx: dict[str, list[dict]] = {}
    for _, row in df.iterrows():
        row_dict = row.to_dict()
        tid = str(row_dict.get("trade_id", "")).strip()
        if tid:
            tid_idx.setdefault(tid.upper(), []).append(row_dict)

        date = str(row_dict.get("trade_date", "")).strip()
        instr = str(row_dict.get("instrument", "")).strip()
        acct = str(row_dict.get("client_account", "")).strip()
        composite = f"{date}|{instr.upper()}|{acct.upper()}"
        if date or instr:
            comp_idx.setdefault(composite, []).append(row_dict)

    cache = [_row_to_ms_trade(row.to_dict(), flow_type) for _, row in df.iterrows()]

    _datasets[flow_type] = {"df": df, "tid_idx": tid_idx, "comp_idx": comp_idx, "cache": cache}

    logger.info(
        "MS %s data loaded: %d rows, %d trade_id entries, %d composite entries",
        flow_type, len(df), len(tid_idx), len(comp_idx),
    )
    return df


def find_by_trade_id(trade_id: str, flow_type: str = FlowType.RECEIVABLE.value) -> Optional[MSTradeRecord]:
    load_ms_data(flow_type)
    ds = _datasets.get(flow_type, {})
    rows = ds.get("tid_idx", {}).get(trade_id.upper().strip(), [])
    return _row_to_ms_trade(rows[0], flow_type) if rows else None


def find_by_composite(
    trade_date: str, instrument: str, client_account: str = "",
    flow_type: str = FlowType.RECEIVABLE.value,
) -> Optional[MSTradeRecord]:
    load_ms_data(flow_type)
    ds = _datasets.get(flow_type, {})
    key = f"{trade_date.strip()}|{instrument.strip().upper()}|{client_account.strip().upper()}"
    rows = ds.get("comp_idx", {}).get(key, [])
    return _row_to_ms_trade(rows[0], flow_type) if rows else None


def get_all_ms_trades(flow_type: str = FlowType.RECEIVABLE.value) -> list[MSTradeRecord]:
    load_ms_data(flow_type)
    ds = _datasets.get(flow_type, {})
    return list(ds.get("cache", []))


def ms_data_stats(flow_type: str = FlowType.RECEIVABLE.value) -> dict:
    df = load_ms_data(flow_type)
    ds = _datasets.get(flow_type, {})
    return {
        "flow_type": flow_type,
        "total_rows": len(df),
        "columns": list(df.columns),
        "trade_id_count": len(ds.get("tid_idx", {})),
        "composite_count": len(ds.get("comp_idx", {})),
    }
