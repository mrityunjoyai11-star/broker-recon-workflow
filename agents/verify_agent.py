"""Agent 1 — Document Verification Agent.

Verifies that an uploaded PDF and Excel belong to the same brokerage invoice.
Rule-based first; LLM fallback for ambiguous cases.
"""

from __future__ import annotations

from broker_recon_flow.config import get_agent_config, get_broker_configs
from broker_recon_flow.parsers.pdf_parser import PDFParser
from broker_recon_flow.parsers.excel_parser import ExcelParser
from broker_recon_flow.schemas.canonical_trade import VerificationResult
from broker_recon_flow.services.llm_service import invoke_llm_json
from broker_recon_flow.utils.logger import get_logger

logger = get_logger(__name__)

VERIFY_SYSTEM_PROMPT = """You are a financial document verification specialist.
Given metadata and content from a PDF brokerage statement and an Excel brokerage statement,
determine if they belong to the SAME brokerage invoice/trade set.

Compare: broker name, invoice number, date/period, currency, approximate totals, counterparty.

Return ONLY a JSON object:
{
  "doc_match": true/false,
  "confidence": 0.0-1.0,
  "broker_detected": "broker name or null",
  "invoice_id": "invoice id or null",
  "mismatches": ["list of specific mismatches"],
  "message": "brief explanation"
}"""


def run_verification(pdf_path: str, excel_path: str) -> VerificationResult:
    logger.info("Starting verification: PDF=%s, Excel=%s", pdf_path, excel_path)

    cfg = get_agent_config("verification")
    broker_configs = get_broker_configs()
    all_keywords = [kw for bc in broker_configs for kw in bc.get("keywords", [])]

    pdf_parser = PDFParser(pdf_path)
    excel_parser = ExcelParser(excel_path)

    pdf_meta = pdf_parser.extract_metadata()
    excel_meta = excel_parser.extract_metadata()
    pdf_meta["detected_keywords"] = pdf_parser.detect_broker_keywords(all_keywords)
    excel_meta["detected_keywords"] = excel_parser.detect_broker_keywords(all_keywords)

    result = _rule_based_verify(pdf_meta, excel_meta, broker_configs)
    if result.confidence >= cfg.get("confidence_threshold", 0.75):
        logger.info("Rule-based verification: match=%s, confidence=%.2f", result.doc_match, result.confidence)
        return result

    logger.info("Inconclusive rule-based, using LLM fallback")
    return _llm_verify(pdf_meta, excel_meta, pdf_parser, excel_parser)


def _rule_based_verify(pdf_meta: dict, excel_meta: dict, broker_configs: list) -> VerificationResult:
    mismatches: list[str] = []
    confidence = 0.0
    broker_detected = None

    pdf_kw = set(pdf_meta.get("detected_keywords", []))
    excel_kw = set(excel_meta.get("detected_keywords", []))
    common_kw = pdf_kw & excel_kw
    any_kw = pdf_kw | excel_kw

    if common_kw:
        confidence += 0.3
        for bc in broker_configs:
            if common_kw & set(bc.get("keywords", [])):
                broker_detected = bc["name"]
                break
    elif any_kw:
        confidence += 0.1
        for bc in broker_configs:
            if any_kw & set(bc.get("keywords", [])):
                broker_detected = bc["name"]
                confidence += 0.1
                break

    if broker_detected is None:
        pdf_name = pdf_meta.get("filename", "").upper()
        excel_name = excel_meta.get("filename", "").upper()
        for bc in broker_configs:
            for kw in bc.get("keywords", []):
                if kw.upper() in pdf_name or kw.upper() in excel_name:
                    broker_detected = bc["name"]
                    confidence += 0.15
                    break
            if broker_detected:
                break

    pdf_inv = pdf_meta.get("invoice_id")
    excel_inv = excel_meta.get("invoice_id")
    if pdf_inv and excel_inv:
        if pdf_inv.strip().upper() == excel_inv.strip().upper():
            confidence += 0.4
        else:
            mismatches.append(f"Invoice ID mismatch: PDF={pdf_inv}, Excel={excel_inv}")
    elif pdf_inv or excel_inv:
        confidence += 0.1

    pdf_date = pdf_meta.get("date")
    excel_date = excel_meta.get("date")
    if pdf_date and excel_date:
        if pdf_date == excel_date:
            confidence += 0.2
        else:
            mismatches.append(f"Date mismatch: PDF={pdf_date}, Excel={excel_date}")
    elif pdf_date or excel_date:
        confidence += 0.05

    doc_match = confidence >= 0.5 and not mismatches
    return VerificationResult(
        broker_detected=broker_detected,
        invoice_id=pdf_inv or excel_inv,
        doc_match=doc_match,
        confidence=min(confidence, 1.0),
        pdf_metadata=pdf_meta,
        excel_metadata=excel_meta,
        mismatches=mismatches,
        message="Rule-based verification " + ("passed" if doc_match else f"failed – {len(mismatches)} issues"),
    )


def _llm_verify(
    pdf_meta: dict, excel_meta: dict, pdf_parser: PDFParser, excel_parser: ExcelParser
) -> VerificationResult:
    pdf_text = pdf_parser.extract_full_text()[:5000]
    excel_preview = ""
    for name, df in excel_parser.read_all_sheets().items():
        excel_preview += f"\nSheet: {name}\nColumns: {list(df.columns)}\n{df.head(10).to_string()}\n"
        if len(excel_preview) > 4000:
            break

    user_prompt = (
        f"PDF Metadata: {pdf_meta}\n\nPDF Text:\n{pdf_text}\n\n"
        f"Excel Metadata: {excel_meta}\n\nExcel Preview:\n{excel_preview}\n\n"
        "Are these from the same brokerage invoice?"
    )
    llm_result = invoke_llm_json(VERIFY_SYSTEM_PROMPT, user_prompt)
    if llm_result.get("parse_error"):
        return VerificationResult(
            doc_match=False, confidence=0.0,
            pdf_metadata=pdf_meta, excel_metadata=excel_meta,
            message="LLM verification failed",
        )
    return VerificationResult(
        broker_detected=llm_result.get("broker_detected"),
        invoice_id=llm_result.get("invoice_id"),
        doc_match=llm_result.get("doc_match", False),
        confidence=float(llm_result.get("confidence", 0.0)),
        pdf_metadata=pdf_meta,
        excel_metadata=excel_meta,
        mismatches=llm_result.get("mismatches", []),
        message=llm_result.get("message", "LLM verification"),
    )
