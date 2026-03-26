"""MS Receivables Data Service.

Loads the MS receivables Excel on first use and provides fast lookup
by trade_id, composite key (date + instrument + account), and full scan.

The MS data is READ-ONLY. We never write back to the source file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd

from broker_recon_flow.config import get_ms_data_config
from broker_recon_flow.schemas.canonical_trade import MSTradeRecord
from broker_recon_flow.utils.logger import get_logger

logger = get_logger(__name__)

# ── Singleton state ─────────────────────────────────────────────────────────
_df: Optional[pd.DataFrame] = None
_trade_id_index: dict[str, list[dict]] = {}     # trade_id → [row dicts]
_composite_index: dict[str, list[dict]] = {}    # "date|instrument|account" → [row dicts]
_ms_trades_cache: Optional[list] = None          # cached MSTradeRecord list

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


def _row_to_ms_trade(row: dict) -> MSTradeRecord:
    return MSTradeRecord(
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


def load_ms_data(force_reload: bool = False) -> pd.DataFrame:
    """Load MS receivables Excel into memory (singleton). Returns normalized DataFrame."""
    global _df, _trade_id_index, _composite_index, _ms_trades_cache

    if _df is not None and not force_reload:
        return _df

    cfg = get_ms_data_config()
    raw_path = cfg.get("file_path", "")

    # Resolve path relative to broker_recon_flow/
    base = Path(__file__).parent.parent
    file_path = (base / raw_path).resolve()

    if not file_path.exists():
        logger.warning("MS data file not found: %s. Continuing with empty dataset.", file_path)
        _df = pd.DataFrame()
        _ms_trades_cache = []
        return _df

    logger.info("Loading MS receivables data from %s", file_path)
    try:
        _df = pd.read_excel(file_path, dtype=str)
    except Exception as exc:
        logger.error("Failed to load MS data: %s", exc)
        _df = pd.DataFrame()
        _ms_trades_cache = []
        return _df

    _df = _normalize_ms_columns(_df)
    _df = _df.fillna("")

    # Build indexes — use lists to handle duplicate keys
    _trade_id_index = {}
    _composite_index = {}
    for _, row in _df.iterrows():
        row_dict = row.to_dict()
        tid = str(row_dict.get("trade_id", "")).strip()
        if tid:
            _trade_id_index.setdefault(tid.upper(), []).append(row_dict)

        date = str(row_dict.get("trade_date", "")).strip()
        instr = str(row_dict.get("instrument", "")).strip()
        acct = str(row_dict.get("client_account", "")).strip()
        composite = f"{date}|{instr.upper()}|{acct.upper()}"
        if date or instr:
            _composite_index.setdefault(composite, []).append(row_dict)

    # Cache the MSTradeRecord list
    _ms_trades_cache = [_row_to_ms_trade(row.to_dict()) for _, row in _df.iterrows()]

    logger.info(
        "MS data loaded: %d rows, %d trade_id entries, %d composite entries",
        len(_df),
        len(_trade_id_index),
        len(_composite_index),
    )
    return _df


def find_by_trade_id(trade_id: str) -> Optional[MSTradeRecord]:
    load_ms_data()
    rows = _trade_id_index.get(trade_id.upper().strip(), [])
    return _row_to_ms_trade(rows[0]) if rows else None


def find_by_composite(trade_date: str, instrument: str, client_account: str = "") -> Optional[MSTradeRecord]:
    load_ms_data()
    key = f"{trade_date.strip()}|{instrument.strip().upper()}|{client_account.strip().upper()}"
    rows = _composite_index.get(key, [])
    return _row_to_ms_trade(rows[0]) if rows else None


def get_all_ms_trades() -> list[MSTradeRecord]:
    load_ms_data()
    return list(_ms_trades_cache or [])


def ms_data_stats() -> dict:
    df = load_ms_data()
    return {
        "total_rows": len(df),
        "columns": list(df.columns),
        "trade_id_count": len(_trade_id_index),
        "composite_count": len(_composite_index),
    }
