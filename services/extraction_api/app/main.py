from __future__ import annotations

import os
import re
from datetime import date
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from pypdf import PdfReader

app = FastAPI(title="Invoice Extraction API", version="0.1.0")


class ExtractRequest(BaseModel):
    file_path: str = Field(..., description="Local path to PDF")
    file_type: str = Field(..., description="Only 'PDF' is supported for now")


class LineItem(BaseModel):
    Description: str
    Quantity: float
    UnitPrice: float
    Amount: float


class ExtractionResponse(BaseModel):
    InvoiceNumber: str
    OrderNumber: Optional[str] = None
    InvoiceDate: str
    InvoiceBaseAmount: float
    InvoiceWithTaxAmount: float
    LineItems: list[LineItem]


def _read_pdf_text(file_path: str) -> str:
    if not os.path.exists(file_path):
        raise FileNotFoundError(file_path)
    reader = PdfReader(file_path)
    parts: list[str] = []
    for page in reader.pages:
        txt = page.extract_text() or ""
        parts.append(txt)
    return "\n".join(parts).strip()


def _first_match(pattern: str, text: str, *, flags: int = re.IGNORECASE) -> Optional[str]:
    m = re.search(pattern, text, flags)
    if not m:
        return None
    return m.group(1).strip()


def _to_number(val: Optional[str]) -> Optional[float]:
    if val is None:
        return None
    cleaned = val.replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _mock_openai_extract_to_json(text: str) -> dict[str, Any]:
    """Placeholder for an LLM call.

    This is intentionally deterministic and lightweight: it uses a few regexes to
    produce a structured JSON output even without any cloud dependency.
    """

    invoice_number = (
        _first_match(r"Invoice\s*(?:#|No\.?|Number)\s*[:\-]?\s*([A-Z0-9\-_/]+)", text)
        or _first_match(r"\bINV[\-_/ ]?(\d{3,})\b", text)
        or "UNKNOWN"
    )

    order_number = (
        _first_match(r"Order\s*(?:#|No\.?|Number)\s*[:\-]?\s*([A-Z0-9\-_/]+)", text)
        or _first_match(r"PO\s*(?:#|No\.?|Number)\s*[:\-]?\s*([A-Z0-9\-_/]+)", text)
    )

    invoice_date = (
        _first_match(
            r"Invoice\s*Date\s*[:\-]?\s*(\d{4}[-/]\d{2}[-/]\d{2}|\d{2}[-/]\d{2}[-/]\d{4})",
            text,
        )
        or _first_match(r"\b(\d{4}[-/]\d{2}[-/]\d{2})\b", text)
    )
    if invoice_date is None:
        invoice_date = date.today().isoformat()

    base_amount = _to_number(
        _first_match(r"(?:Subtotal|Base\s*Amount)\s*[:\-]?\s*\$?([0-9,]+(?:\.[0-9]{1,2})?)", text)
    )

    total_amount = _to_number(
        _first_match(r"(?:Total\s*Due|Total)\s*[:\-]?\s*\$?([0-9,]+(?:\.[0-9]{1,2})?)", text)
    )

    if base_amount is None or total_amount is None:
        numbers = [float(x.replace(",", "")) for x in re.findall(r"\b\d{1,3}(?:,\d{3})*(?:\.\d{2})\b", text)]
        if numbers:
            if total_amount is None:
                total_amount = max(numbers)
            if base_amount is None:
                base_amount = min(numbers) if len(numbers) > 1 else max(numbers)

    base_amount = float(base_amount or 0.0)
    total_amount = float(total_amount or 0.0)

    line_items: list[dict[str, Any]] = []

    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue

        m = re.search(
            r"^(?P<desc>.+?)\s{1,}(?P<qty>\d+(?:\.\d+)?)\s{1,}(?P<unit>\d+(?:\.\d+)?)\s{1,}(?P<amt>\d+(?:\.\d+)?)$",
            line,
        )
        if not m:
            continue

        desc = m.group("desc").strip()
        qty = float(m.group("qty"))
        unit = float(m.group("unit"))
        amt = float(m.group("amt"))

        if len(desc) < 3:
            continue

        line_items.append(
            {
                "Description": desc,
                "Quantity": qty,
                "UnitPrice": unit,
                "Amount": amt,
            }
        )

    if not line_items:
        line_items = [
            {
                "Description": "UNKNOWN",
                "Quantity": 1.0,
                "UnitPrice": base_amount,
                "Amount": base_amount,
            }
        ]

    return {
        "InvoiceNumber": invoice_number,
        "OrderNumber": order_number,
        "InvoiceDate": invoice_date,
        "InvoiceBaseAmount": base_amount,
        "InvoiceWithTaxAmount": total_amount,
        "LineItems": line_items,
    }


@app.post("/extract", response_model=ExtractionResponse)
def extract(req: ExtractRequest) -> ExtractionResponse:
    if req.file_type.upper() != "PDF":
        raise HTTPException(status_code=400, detail="Only file_type='PDF' is supported")

    try:
        text = _read_pdf_text(req.file_path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"File not found: {req.file_path}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to read PDF: {exc}")

    extraction = _mock_openai_extract_to_json(text)
    return ExtractionResponse.model_validate(extraction)
