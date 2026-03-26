"""Column Matcher — fuzzy matching of raw broker column names to canonical fields.

Uses rapidfuzz for token-sort-ratio scoring against a rich synonym dictionary.
"""

from __future__ import annotations

from typing import Optional

from rapidfuzz import fuzz, process

from broker_recon_flow.utils.logger import get_logger

logger = get_logger(__name__)

# ── Canonical field → list of expected synonyms ────────────────────────────
CANONICAL_SYNONYMS: dict[str, list[str]] = {
    "trade_id": [
        "trade id", "trade_id", "tradeid", "deal id", "deal_id", "deal no",
        "deal number", "transaction id", "trx id", "ref", "reference",
        "order id", "ticket", "ticket no", "confirmation no",
    ],
    "trade_date": [
        "trade date", "trade_date", "tradedate", "date", "deal date",
        "transaction date", "value date", "booking date",
    ],
    "invoice_date": [
        "invoice date", "invoice_date", "billing date", "statement date",
    ],
    "instrument": [
        "instrument", "product", "commodity", "security", "ticker",
        "description", "contract", "underlying", "asset",
    ],
    "exchange": [
        "exchange", "market", "venue", "place of trade", "trading venue",
    ],
    "buy_sell": [
        "buy sell", "buysell", "b/s", "side", "direction", "bs",
        "trade type", "buy/sell",
    ],
    "quantity": [
        "quantity", "qty", "volume", "lots", "number of lots", "no of lots",
        "size", "units", "notional qty",
    ],
    "unit": [
        "unit", "units", "lot size", "contract size",
    ],
    "price": [
        "price", "unit price", "trade price", "execution price",
        "average price", "avg price",
    ],
    "delivery_start": [
        "delivery start", "delivery from", "from date", "period start",
        "start date", "delivery period start",
    ],
    "delivery_end": [
        "delivery end", "delivery to", "to date", "period end",
        "end date", "delivery period end",
    ],
    "counterparty": [
        "counterparty", "counter party", "broker", "dealer", "cpty",
    ],
    "client_account": [
        "client account", "account", "client", "fund", "portfolio",
        "account name", "client name", "account code",
    ],
    "brokerage_rate": [
        "brokerage rate", "commission rate", "rate", "fee rate",
        "broker rate", "brokerage %",
    ],
    "brokerage_amount": [
        "brokerage amount", "brokerage", "commission", "commission amount",
        "fee", "broker fee", "brokerage fee", "total commission",
    ],
    "currency": [
        "currency", "ccy", "curr", "currency code",
    ],
}

# Flatten synonym list into lookup: synonym → canonical
_FLAT: dict[str, str] = {}
for canonical, synonyms in CANONICAL_SYNONYMS.items():
    for syn in synonyms:
        _FLAT[syn.lower()] = canonical


def match_column(raw_col: str, threshold: float = 0.70) -> Optional[str]:
    """
    Match a raw column name to a canonical field name.

    1. Exact match (after lower/strip)
    2. Fuzzy match using rapidfuzz token_sort_ratio against all synonyms

    Returns canonical field name or None if no match above threshold.
    """
    normalized = raw_col.strip().lower()

    # 1. Exact
    if normalized in _FLAT:
        return _FLAT[normalized]

    # 2. Fuzzy — compare against all known synonyms
    all_synonyms = list(_FLAT.keys())
    best = process.extractOne(
        normalized,
        all_synonyms,
        scorer=fuzz.token_sort_ratio,
        score_cutoff=threshold * 100,
    )
    if best is not None:
        matched_syn, score, _ = best
        canonical = _FLAT[matched_syn]
        logger.debug(
            "Fuzzy column match: '%s' → '%s' via '%s' (score=%.1f)",
            raw_col, canonical, matched_syn, score,
        )
        return canonical

    return None


def build_column_mapping(raw_columns: list[str], threshold: float = 0.70) -> dict[str, str]:
    """
    Given a list of raw column names, return a dict mapping each raw name
    to its canonical field name (skipping unmatched columns).
    """
    mapping: dict[str, str] = {}
    unmatched: list[str] = []

    for col in raw_columns:
        canonical = match_column(col, threshold=threshold)
        if canonical:
            # Prefer the first match for each canonical field
            if canonical not in mapping.values():
                mapping[col] = canonical
        else:
            unmatched.append(col)

    if unmatched:
        logger.debug("Unmatched columns (need LLM): %s", unmatched)

    return mapping


def get_unmatched_columns(raw_columns: list[str], threshold: float = 0.70) -> list[str]:
    """Return columns that could NOT be fuzzy-matched."""
    return [col for col in raw_columns if match_column(col, threshold=threshold) is None]
