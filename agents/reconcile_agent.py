"""Agent 4 — Reconciliation Engine (Broker vs MS Data).

Matches extracted broker trades against MS receivables data.
Deterministic / no LLM — pure algorithmic reconciliation.

Match strategy (in order):
  1. trade_id exact match
  2. Composite key: trade_date + instrument + client_account
  3. Fuzzy: instrument + date + buy_sell
"""

from __future__ import annotations

from broker_recon_flow.config import get_agent_config
from broker_recon_flow.schemas.canonical_trade import (
    TradeRecord, MSTradeRecord,
    ReconciliationResult, ReconciliationMatch, ReconciliationStatus,
    FlowType,
)
from broker_recon_flow.services import ms_data_service as ms_svc
from broker_recon_flow.utils.logger import get_logger

logger = get_logger(__name__)


def run_reconciliation(
    broker_trades: list[TradeRecord],
    broker_name: str | None = None,
    flow_type: str = FlowType.RECEIVABLE.value,
) -> ReconciliationResult:
    """Reconcile broker trades against MS data (receivables or payables)."""
    ms_trades = ms_svc.get_all_ms_trades(flow_type=flow_type)
    logger.info(
        "Reconciling %d broker trades against %d MS trades (broker=%s, flow=%s)",
        len(broker_trades), len(ms_trades), broker_name, flow_type,
    )

    cfg = get_agent_config("reconciliation")
    tolerance = cfg.get("tolerance", {})

    matched: list[ReconciliationMatch] = []
    mismatched: list[ReconciliationMatch] = []
    new_trades: list[ReconciliationMatch] = []   # in broker, not in MS
    missing_trades: list[ReconciliationMatch] = []  # in MS, not in broker

    # Build MS indexes
    ms_by_id: dict[str, MSTradeRecord] = {}
    ms_by_composite: dict[str, MSTradeRecord] = {}
    for ms in ms_trades:
        if ms.trade_id:
            ms_by_id[ms.trade_id.upper()] = ms
        comp = _composite_key(ms.trade_date, ms.instrument, ms.client_account)
        if comp:
            ms_by_composite[comp] = ms

    matched_ms_ids: set[str] = set()

    for broker in broker_trades:
        ms_trade = _find_ms_trade(broker, ms_by_id, ms_by_composite, ms_trades, matched_ms_ids)

        if ms_trade is None:
            new_trades.append(ReconciliationMatch(
                broker_trade=broker,
                ms_trade=None,
                status=ReconciliationStatus.NEW,
                mismatch_reason="No matching MS trade found",
            ))
            continue

        matched_ms_ids.add(_ms_uid(ms_trade))
        diffs = _compare(broker, ms_trade, tolerance)

        if not diffs:
            score = _confidence_score(broker, ms_trade)
            matched.append(ReconciliationMatch(
                broker_trade=broker,
                ms_trade=ms_trade,
                status=ReconciliationStatus.MATCH,
                confidence_score=score,
            ))
        else:
            reason = _describe_mismatch(diffs)
            score = _confidence_score(broker, ms_trade)
            mismatched.append(ReconciliationMatch(
                broker_trade=broker,
                ms_trade=ms_trade,
                status=ReconciliationStatus.MISMATCH,
                mismatch_reason=reason,
                differences=diffs,
                confidence_score=score,
            ))

    # MS trades not matched by any broker trade — filter to same broker
    relevant_ms_trades: list[MSTradeRecord] = []
    for ms in ms_trades:
        uid = _ms_uid(ms)
        if broker_name and ms.broker_code:
            if broker_name.upper() not in ms.broker_code.upper():
                continue
        relevant_ms_trades.append(ms)
        if uid not in matched_ms_ids:
            missing_trades.append(ReconciliationMatch(
                broker_trade=None,
                ms_trade=ms,
                status=ReconciliationStatus.MISSING,
                mismatch_reason="MS trade not present in broker statement",
            ))

    broker_total = sum(t.brokerage_amount or 0 for t in broker_trades)
    ms_total = sum(t.brokerage_amount or 0 for t in relevant_ms_trades)
    summary = {
        "broker_trade_count": len(broker_trades),
        "ms_trade_count": len(relevant_ms_trades),
        "matched_count": len(matched),
        "mismatched_count": len(mismatched),
        "new_trades_count": len(new_trades),
        "missing_trades_count": len(missing_trades),
        "match_rate": f"{len(matched) / max(len(broker_trades), 1) * 100:.1f}%",
        "broker_total_brokerage": round(broker_total, 2),
        "ms_total_brokerage": round(ms_total, 2),
        "difference": round(broker_total - ms_total, 2),
    }

    logger.info("Reconciliation: %s", summary)
    return ReconciliationResult(
        matched=matched,
        mismatched=mismatched,
        new_trades=new_trades,
        missing_trades=missing_trades,
        summary=summary,
    )


# ── Matching Helpers ────────────────────────────────────────────────────────

def _find_ms_trade(
    broker: TradeRecord,
    ms_by_id: dict[str, MSTradeRecord],
    ms_by_composite: dict[str, MSTradeRecord],
    all_ms: list[MSTradeRecord],
    matched_ms_ids: set[str],
) -> MSTradeRecord | None:
    # Match 1: trade_id exact
    if broker.trade_id:
        ms = ms_by_id.get(broker.trade_id.upper())
        if ms and _ms_uid(ms) not in matched_ms_ids:
            return ms

    # Match 2: composite key
    comp = _composite_key(broker.trade_date, broker.instrument, broker.client_account)
    if comp:
        ms = ms_by_composite.get(comp)
        if ms and _ms_uid(ms) not in matched_ms_ids:
            return ms

    # Match 3: fuzzy (instrument + date + buy_sell)
    return _fuzzy_find(broker, all_ms, matched_ms_ids)


def _composite_key(date: str | None, instrument: str | None, account: str | None) -> str:
    d = (date or "").strip()
    i = (instrument or "").strip().upper()
    a = (account or "").strip().upper()
    if not (d or i):
        return ""
    return f"{d}|{i}|{a}"


def _ms_uid(ms: MSTradeRecord) -> str:
    return f"{ms.trade_id or ''}|{ms.trade_date or ''}|{ms.instrument or ''}|{ms.client_account or ''}"


def _fuzzy_find(
    broker: TradeRecord, all_ms: list[MSTradeRecord], matched_ms_ids: set[str]
) -> MSTradeRecord | None:
    b_date = _norm_date(broker.trade_date)
    b_instr = (broker.instrument or "").upper()
    b_bs = (broker.buy_sell or "").upper()

    for ms in all_ms:
        if _ms_uid(ms) in matched_ms_ids:
            continue
        m_date = _norm_date(ms.trade_date)
        m_instr = (ms.instrument or "").upper()
        m_bs = (ms.buy_sell or "").upper()

        if b_instr and m_instr and b_instr == m_instr and b_date == m_date:
            if not b_bs or not m_bs or b_bs == m_bs:
                return ms
    return None


def _norm_date(d: str | None) -> str:
    if not d:
        return ""
    s = str(d).strip()
    return s.split(" ")[0] if " " in s else s


# ── Comparison Helpers ──────────────────────────────────────────────────────

def _compare(broker: TradeRecord, ms: MSTradeRecord, tolerance: dict) -> dict:
    diffs: dict = {}

    checks = [
        ("quantity", broker.quantity, ms.quantity, tolerance.get("quantity", 0.001)),
        ("price", broker.price, ms.price, tolerance.get("price", 0.01)),
        ("brokerage_amount", broker.brokerage_amount, ms.brokerage_amount, tolerance.get("brokerage", 1.0)),
    ]
    for field, bv, mv, tol in checks:
        if bv is None and mv is None:
            continue
        if bv is None or mv is None:
            diffs[field] = {"broker": bv, "ms": mv, "reason": "missing_value"}
            continue
        if abs(float(bv) - float(mv)) > tol:
            diffs[field] = {"broker": bv, "ms": mv, "diff": abs(float(bv) - float(mv))}

    if broker.currency and ms.currency:
        if broker.currency.upper() != ms.currency.upper():
            diffs["currency"] = {"broker": broker.currency, "ms": ms.currency}

    if broker.buy_sell and ms.buy_sell:
        if broker.buy_sell.upper() != ms.buy_sell.upper():
            diffs["buy_sell"] = {"broker": broker.buy_sell, "ms": ms.buy_sell}

    return diffs


def _confidence_score(broker: TradeRecord, ms: MSTradeRecord) -> int:
    """Score 0-4: 1 point each for matching trade_id, quantity, price, brokerage."""
    score = 0
    if broker.trade_id and ms.trade_id and broker.trade_id.upper() == ms.trade_id.upper():
        score += 1
    if broker.quantity is not None and ms.quantity is not None and abs(broker.quantity - ms.quantity) < 0.001:
        score += 1
    if broker.price is not None and ms.price is not None and abs(broker.price - ms.price) < 0.01:
        score += 1
    if broker.brokerage_amount is not None and ms.brokerage_amount is not None:
        if abs(broker.brokerage_amount - ms.brokerage_amount) < 1.0:
            score += 1
    return score


def _describe_mismatch(diffs: dict) -> str:
    keys = list(diffs.keys())
    if len(keys) == 1:
        return f"MISMATCH_{keys[0].upper()}"
    return "MULTIPLE_ISSUES"
