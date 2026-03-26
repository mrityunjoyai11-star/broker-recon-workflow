"""Excel Parser — pandas-based extraction for broker Excel statements."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pandas as pd

from broker_recon_flow.utils.logger import get_logger

logger = get_logger(__name__)


class ExcelParser:
    """Extract data, tables, and metadata from Excel brokerage statements."""

    def __init__(self, file_path: str | Path):
        self.file_path = Path(file_path)
        self._sheets: dict[str, pd.DataFrame] | None = None
        self._raw_sheets: dict[str, pd.DataFrame] | None = None

    def read_all_sheets(self) -> dict[str, pd.DataFrame]:
        if self._sheets is not None:
            return self._sheets

        self._raw_sheets = pd.read_excel(self.file_path, sheet_name=None, header=None)
        self._sheets = {}

        for sheet_name, df in self._raw_sheets.items():
            header_row = self._detect_header_row(df)
            if header_row is not None:
                df.columns = [
                    str(c).strip() if pd.notna(c) else f"col_{i}"
                    for i, c in enumerate(df.iloc[header_row])
                ]
                df = df.iloc[header_row + 1 :].reset_index(drop=True)
            df = df.dropna(how="all")
            if not df.empty:
                self._sheets[sheet_name] = df
                logger.info("Sheet '%s': %d rows, %d cols", sheet_name, len(df), len(df.columns))

        logger.info("Loaded %d sheets from %s", len(self._sheets), self.file_path.name)
        return self._sheets

    def _detect_header_row(self, df: pd.DataFrame, max_scan: int = 20) -> int | None:
        best_row, best_score = None, 0
        for i in range(min(max_scan, len(df))):
            row = df.iloc[i]
            string_count = sum(1 for v in row if isinstance(v, str) and len(v.strip()) > 0)
            if string_count > best_score and row.notna().sum() >= 3:
                best_score = string_count
                best_row = i
        return best_row

    def extract_metadata(self) -> dict[str, Any]:
        sheets = self.read_all_sheets()
        metadata: dict[str, Any] = {
            "filename": self.file_path.name,
            "sheet_names": list(sheets.keys()),
            "sheet_count": len(sheets),
        }
        if self._raw_sheets:
            for sheet_name, raw_df in self._raw_sheets.items():
                header_text = ""
                for i in range(min(10, len(raw_df))):
                    header_text += " ".join(str(v) for v in raw_df.iloc[i] if pd.notna(v)) + "\n"

                m = re.search(r"Invoice\s*#?\s*:?\s*([A-Z0-9][\w-]+)", header_text, re.IGNORECASE)
                if m:
                    metadata["invoice_id"] = m.group(1)

                m = re.search(r"Date\s*:?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", header_text, re.IGNORECASE)
                if m:
                    metadata["date"] = m.group(1)

                for kw in ["Evolution", "TFS", "ICAP", "Marex", "JP Morgan", "J.P. Morgan", "JPM"]:
                    if kw.upper() in header_text.upper():
                        metadata["broker_hint"] = kw
                        break
        return metadata

    def get_primary_table(self) -> pd.DataFrame | None:
        sheets = self.read_all_sheets()
        if not sheets:
            return None
        trade_keywords = {
            "trade", "date", "quantity", "price", "instrument", "product",
            "brokerage", "commission", "buy", "sell", "volume", "id", "ref",
        }
        best_sheet, best_score = None, 0
        for df in sheets.values():
            cols_lower = {str(c).lower() for c in df.columns}
            score = len(cols_lower & trade_keywords) * 10 + len(df)
            if score > best_score:
                best_score = score
                best_sheet = df
        if best_sheet is not None:
            return best_sheet
        return max(sheets.values(), key=len)

    def detect_broker_keywords(self, keywords: list[str]) -> list[str]:
        if self._raw_sheets is None:
            self.read_all_sheets()
        all_text = ""
        for raw_df in (self._raw_sheets or {}).values():
            for i in range(min(15, len(raw_df))):
                all_text += " ".join(str(v) for v in raw_df.iloc[i] if pd.notna(v)) + " "
        upper = all_text.upper()
        return [kw for kw in keywords if kw.upper() in upper]
