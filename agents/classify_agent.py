"""Agent 2 — Broker Template Classifier.

4-tier classification:
  1. Exact keyword match in document content
  2. Filename keyword match
  3. TemplateCache DB lookup (auto-learned mappings)
  4. LLM fallback
"""

from __future__ import annotations

from broker_recon_flow.config import get_agent_config, get_broker_configs
from broker_recon_flow.parsers.pdf_parser import PDFParser
from broker_recon_flow.parsers.excel_parser import ExcelParser
from broker_recon_flow.parsers.template_parser import list_available_templates
from broker_recon_flow.schemas.canonical_trade import ClassificationResult
from broker_recon_flow.services.llm_service import invoke_llm_json
from broker_recon_flow.utils.logger import get_logger

logger = get_logger(__name__)

CLASSIFY_SYSTEM_PROMPT = """You are a financial document classification specialist.
Given text from a brokerage statement, identify which broker it belongs to.

Known templates: evolution, tfs, icap, marex, jpmorgan
For unknown brokers set template_type to null — the system will use fuzzy/LLM extraction.

Return ONLY a JSON object:
{
  "template_type": "template name or null",
  "broker_name": "full broker name",
  "confidence": 0.0-1.0,
  "detected_keywords": ["keywords found"],
  "reasoning": "brief explanation"
}"""


def run_classification(
    pdf_path: str | None = None,
    excel_path: str | None = None,
    broker_hint: str | None = None,
    db_session=None,
    flow_type: str = "receivable",
) -> ClassificationResult:
    logger.info("Classifying: PDF=%s, Excel=%s, hint=%s, flow=%s", pdf_path, excel_path, broker_hint, flow_type)

    cfg = get_agent_config("classifier")
    broker_configs = get_broker_configs()
    available_templates = list_available_templates(flow_type=flow_type)

    # Tier 1: broker hint direct match
    if broker_hint:
        result = _match_broker_hint(broker_hint, broker_configs, available_templates)
        if result and result.confidence >= cfg.get("confidence_threshold", 0.80):
            logger.info("Classified by hint: %s (%.2f)", result.template_type, result.confidence)
            return result

    # Build flat keyword → broker_config map
    all_keywords: dict[str, dict] = {}
    for bc in broker_configs:
        for kw in bc.get("keywords", []):
            all_keywords[kw] = bc

    detected_keywords: list[str] = []
    result: ClassificationResult | None = None

    if pdf_path:
        pdf_parser = PDFParser(pdf_path)
        detected_keywords.extend(pdf_parser.detect_broker_keywords(list(all_keywords.keys())))
        # Also check filename
        pdf_name = str(pdf_path).upper()
        for kw in all_keywords:
            if kw.upper() in pdf_name and kw not in detected_keywords:
                detected_keywords.append(kw)

    if excel_path:
        excel_parser = ExcelParser(excel_path)
        detected_keywords.extend(excel_parser.detect_broker_keywords(list(all_keywords.keys())))
        excel_name = str(excel_path).upper()
        for kw in all_keywords:
            if kw.upper() in excel_name and kw not in detected_keywords:
                detected_keywords.append(kw)

    # Tier 2: rule-based keyword classification
    if detected_keywords:
        result = _keywords_to_classification(detected_keywords, broker_configs, available_templates)
        if result.confidence >= cfg.get("confidence_threshold", 0.80):
            logger.info("Rule-based classification: %s (%.2f)", result.template_type, result.confidence)
            return result

    # Tier 3: TemplateCache DB lookup
    broker_for_cache = (
        (result.broker_name_detected if result else None)
        or broker_hint
    )
    if db_session and broker_for_cache:
        cached = _check_template_cache(db_session, broker_for_cache, flow_type=flow_type)
        if cached:
            logger.info("TemplateCache hit for broker: %s", broker_for_cache)
            return ClassificationResult(
                template_type=None,
                parser_strategy="cached_template",
                confidence=0.90,
                detected_keywords=detected_keywords,
                broker_name_detected=broker_for_cache,
                method="cached_template",
            )

    # Tier 4: LLM fallback
    if cfg.get("use_llm_fallback", True):
        logger.info("Using LLM classification fallback")
        return _llm_classify(pdf_path, excel_path, available_templates, detected_keywords)

    return ClassificationResult(
        template_type=None,
        confidence=0.0,
        detected_keywords=detected_keywords,
        broker_name_detected=broker_hint,
        method="rule_based",
    )


def _match_broker_hint(hint: str, broker_configs: list, available_templates: list) -> ClassificationResult | None:
    hint_upper = hint.upper()
    for bc in broker_configs:
        for kw in bc.get("keywords", []):
            if kw.upper() in hint_upper or hint_upper in kw.upper():
                tmpl = bc.get("template")
                has_tmpl = tmpl in available_templates
                return ClassificationResult(
                    template_type=tmpl if has_tmpl else None,
                    parser_strategy="template" if has_tmpl else "llm_assisted",
                    confidence=0.90,
                    detected_keywords=[kw],
                    broker_name_detected=bc["name"],
                    method="rule_based",
                )
    return None


def _keywords_to_classification(
    keywords: list[str], broker_configs: list, available_templates: list
) -> ClassificationResult:
    scores: dict[str | None, tuple[int, str]] = {}
    for bc in broker_configs:
        tmpl = bc.get("template")
        bc_kw = {k.upper() for k in bc.get("keywords", [])}
        score = sum(1 for kw in keywords if kw.upper() in bc_kw)
        if score > 0:
            scores[tmpl] = (score, bc["name"])

    if scores:
        best_tmpl = max(scores, key=lambda k: scores[k][0])
        best_score, best_name = scores[best_tmpl]
        confidence = min(0.5 + (best_score / max(sum(s for s, _ in scores.values()), 1)) * 0.5, 1.0)
        has_tmpl = best_tmpl in available_templates
        return ClassificationResult(
            template_type=best_tmpl if has_tmpl else None,
            parser_strategy="template" if has_tmpl else "llm_assisted",
            confidence=confidence,
            detected_keywords=keywords,
            broker_name_detected=best_name,
            method="rule_based",
        )
    return ClassificationResult(template_type=None, confidence=0.0, detected_keywords=keywords, method="rule_based")


def _check_template_cache(db_session, broker_name: str, flow_type: str = "receivable") -> dict | None:
    """Check TemplateCache DB for a HITL-approved mapping for this broker + flow type."""
    try:
        from broker_recon_flow.db.models import TemplateCache
        row = (
            db_session.query(TemplateCache)
            .filter(
                TemplateCache.broker_name.ilike(f"%{broker_name}%"),
                TemplateCache.hitl_approved == True,  # noqa: E712
                TemplateCache.flow_type == flow_type,
            )
            .order_by(TemplateCache.use_count.desc())
            .first()
        )
        if row:
            return row.column_mapping
    except Exception as exc:
        logger.warning("TemplateCache lookup failed: %s", exc)
    return None


def _llm_classify(
    pdf_path: str | None,
    excel_path: str | None,
    available_templates: list,
    detected_keywords: list,
) -> ClassificationResult:
    context = f"Available templates: {available_templates}\n\n"
    if pdf_path:
        pdf_parser = PDFParser(pdf_path)
        context += f"PDF Text:\n{pdf_parser.extract_full_text()[:]}\n\n"
    if excel_path:
        excel_parser = ExcelParser(excel_path)
        for name, df in excel_parser.read_all_sheets().items():
            context += f"Sheet '{name}': {list(df.columns)}\n{df.head(10).to_string()}\n"
            break

    result = invoke_llm_json(CLASSIFY_SYSTEM_PROMPT, context)
    if result.get("parse_error"):
        return ClassificationResult(template_type=None, confidence=0.0, method="llm")

    tmpl = result.get("template_type")
    if tmpl and tmpl not in available_templates:
        tmpl = None
    return ClassificationResult(
        template_type=tmpl,
        parser_strategy="template" if tmpl else "llm_assisted",
        confidence=float(result.get("confidence", 0.0)),
        detected_keywords=detected_keywords + result.get("detected_keywords", []),
        broker_name_detected=result.get("broker_name"),
        method="llm",
    )
