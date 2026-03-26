"""PDF Parser — pdfplumber-based extraction for broker PDF statements."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pdfplumber
import pandas as pd

from broker_recon_flow.utils.logger import get_logger

logger = get_logger(__name__)


class PDFParser:
    """Extract text, tables, and metadata from PDF brokerage statements."""

    def __init__(self, file_path: str | Path):
        self.file_path = Path(file_path)
        self._text: str | None = None
        self._pages_text: list[str] | None = None
        self._tables: list[pd.DataFrame] | None = None

    def extract_full_text(self) -> str:
        if self._text is not None:
            return self._text
        texts = []
        with pdfplumber.open(self.file_path) as pdf:
            for page in pdf.pages:
                texts.append(page.extract_text() or "")
        self._pages_text = texts
        self._text = "\n\n".join(texts)
        logger.info("Extracted text from %s: %d chars, %d pages", self.file_path.name, len(self._text), len(texts))
        return self._text

    def extract_pages_text(self) -> list[str]:
        if self._pages_text is None:
            self.extract_full_text()
        return self._pages_text

    def extract_tables(self) -> list[pd.DataFrame]:
        if self._tables is not None:
            return self._tables
        tables = []
        with pdfplumber.open(self.file_path) as pdf:
            for i, page in enumerate(pdf.pages):
                for j, table in enumerate(page.extract_tables() or []):
                    if table and len(table) > 1:
                        headers = [str(h).strip() if h else f"col_{k}" for k, h in enumerate(table[0])]
                        df = pd.DataFrame(table[1:], columns=headers)
                        df = df.dropna(how="all")
                        if not df.empty:
                            df.attrs["source_page"] = i + 1
                            df.attrs["table_index"] = j
                            tables.append(df)
        self._tables = tables
        logger.info("Extracted %d tables from %s", len(tables), self.file_path.name)
        return self._tables

    def extract_metadata(self, patterns: dict[str, list[str]] | None = None) -> dict[str, Any]:
        text = self.extract_full_text()
        metadata: dict[str, Any] = {
            "filename": self.file_path.name,
            "text_length": len(text),
        }
        with pdfplumber.open(self.file_path) as pdf:
            metadata["page_count"] = len(pdf.pages)

        if patterns:
            for field, pattern_list in patterns.items():
                for pat in pattern_list:
                    m = re.search(pat, text, re.IGNORECASE)
                    if m:
                        metadata[field] = m.group(1) if m.lastindex else m.group(0)
                        break

        if "invoice_id" not in metadata:
            for pat in [
                r"Invoice\s*#?\s*:?\s*([A-Z0-9][\w-]+)",
                r"Inv\s*No\s*:?\s*([A-Z0-9][\w-]+)",
                r"Reference\s*:?\s*([A-Z0-9][\w-]+)",
            ]:
                m = re.search(pat, text, re.IGNORECASE)
                if m:
                    metadata["invoice_id"] = m.group(1)
                    break

        if "date" not in metadata:
            m = re.search(r"Date\s*:?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", text, re.IGNORECASE)
            if m:
                metadata["date"] = m.group(1)

        return metadata

    def detect_broker_keywords(self, keywords: list[str]) -> list[str]:
        text = self.extract_full_text().upper()
        return [kw for kw in keywords if kw.upper() in text]

    def get_text_around_tables(self, context_lines: int = 5) -> list[dict]:
        pages_text = self.extract_pages_text()
        contexts = []
        for table in self.extract_tables():
            page_idx = table.attrs.get("source_page", 1) - 1
            if page_idx < len(pages_text):
                lines = pages_text[page_idx].split("\n")
                contexts.append({
                    "page": page_idx + 1,
                    "header_area": "\n".join(lines[:context_lines]),
                    "columns": list(table.columns),
                    "sample_rows": table.head(3).to_dict(orient="records"),
                })
        return contexts
