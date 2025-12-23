from __future__ import annotations

import os
from typing import Any, Literal, Optional

import azure.functions as func
import requests
from pydantic import BaseModel, Field, ValidationError

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


# ---------- Request/Response Models ----------


class Attachment(BaseModel):
    DocumentType: str
    FileName: str
    FileType: Literal["PDF"]
    FileUrl: str  # local file path


class OrchestratorRequest(BaseModel):
    query: str
    Invoice: dict[str, Any]
    Attachments: list[Attachment]


class ValidationFieldResult(BaseModel):
    status: Literal["MATCH", "MISMATCH", "MISSING_IN_EXTRACTION"]
    expected: Any = None
    extracted: Any = None


class ValidationResult(BaseModel):
    is_valid: bool
    field_analysis: dict[str, ValidationFieldResult]
    line_items_analysis: dict[str, Any]
    summary: str


class OrchestratorResponse(BaseModel):
    Extraction: dict[str, Any]
    Validation: ValidationResult


# ---------- Helpers ----------


def _num_equal(a: Any, b: Any, tol: float = 0.01) -> bool:
    try:
        af = float(a)
        bf = float(b)
    except (TypeError, ValueError):
        return False
    return abs(af - bf) <= tol


def _val_status(expected: Any, extracted: Any) -> Literal["MATCH", "MISMATCH", "MISSING_IN_EXTRACTION"]:
    if extracted is None:
        return "MISSING_IN_EXTRACTION"

    if isinstance(expected, (int, float)) or isinstance(extracted, (int, float)):
        return "MATCH" if _num_equal(expected, extracted) else "MISMATCH"

    return "MATCH" if str(expected).strip() == str(extracted).strip() else "MISMATCH"


def _pick_invoice_attachment(atts: list[Attachment]) -> Attachment:
    for a in atts:
        if a.DocumentType.lower() == "invoice":
            return a
    return atts[0]


def _call_extraction_api(file_path: str) -> dict[str, Any]:
    base_url = os.getenv("EXTRACTION_API_URL", "http://localhost:8001").rstrip("/")
    url = f"{base_url}/extract"
    resp = requests.post(url, json={"file_path": file_path, "file_type": "PDF"}, timeout=60)
    resp.raise_for_status()
    return resp.json()


def _compare(extracted: dict[str, Any], expected_invoice: dict[str, Any]) -> ValidationResult:
    # Map expected keys (from input) to extraction keys.
    field_map = {
        "InvoiceNumber": "InvoiceNumber",
        "OrderNumber": "OrderNumber",
        "InvoiceDate": "InvoiceDate",
        "InvoiceBaseAmount": "InvoiceBaseAmount",
        "InvoiceWithTaxAmount": "InvoiceWithTaxAmount",
    }

    field_analysis: dict[str, ValidationFieldResult] = {}
    mismatches: list[str] = []

    for expected_key, extracted_key in field_map.items():
        expected_val = expected_invoice.get(expected_key)
        extracted_val = extracted.get(extracted_key)

        status = _val_status(expected_val, extracted_val)
        field_analysis[expected_key] = ValidationFieldResult(
            status=status,
            expected=expected_val,
            extracted=extracted_val,
        )
        if status != "MATCH":
            mismatches.append(expected_key)

    # Line items: minimal positional comparison.
    expected_items = expected_invoice.get("LineItems") or []
    extracted_items = extracted.get("LineItems") or []

    line_item_results: list[dict[str, Any]] = []
    max_len = max(len(expected_items), len(extracted_items))

    for i in range(max_len):
        exp = expected_items[i] if i < len(expected_items) else None
        ext = extracted_items[i] if i < len(extracted_items) else None

        if ext is None:
            line_item_results.append({"index": i, "status": "MISSING_IN_EXTRACTION", "expected": exp, "extracted": None})
            mismatches.append(f"LineItems[{i}]")
            continue
        if exp is None:
            # Extra extracted items are not treated as invalid for minimal system.
            line_item_results.append({"index": i, "status": "EXTRA_IN_EXTRACTION", "expected": None, "extracted": ext})
            continue

        per_field = {}
        for k in ["Description", "Quantity", "UnitPrice", "Amount"]:
            per_field[k] = {
                "status": _val_status(exp.get(k), ext.get(k)),
                "expected": exp.get(k),
                "extracted": ext.get(k),
            }
        item_ok = all(v["status"] == "MATCH" for v in per_field.values())
        if not item_ok:
            mismatches.append(f"LineItems[{i}]")

        line_item_results.append(
            {
                "index": i,
                "status": "MATCH" if item_ok else "MISMATCH",
                "fields": per_field,
            }
        )

    is_valid = len([m for m in mismatches if not m.startswith("LineItems") or "EXTRA" not in m]) == 0
    summary = "Valid" if is_valid else f"Mismatched fields: {', '.join(sorted(set(mismatches)))}"

    return ValidationResult(
        is_valid=is_valid,
        field_analysis=field_analysis,
        line_items_analysis={
            "expected_count": len(expected_items),
            "extracted_count": len(extracted_items),
            "items": line_item_results,
        },
        summary=summary,
    )


# ---------- Function Endpoint ----------


@app.route(route="orchestrators/process_invoice", methods=["POST"])  # becomes /api/orchestrators/process_invoice
def process_invoice(req: func.HttpRequest) -> func.HttpResponse:
    try:
        payload = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON", status_code=400)

    try:
        orchestrator_req = OrchestratorRequest.model_validate(payload)
    except ValidationError as ve:
        return func.HttpResponse(ve.json(), status_code=400, mimetype="application/json")

    if not orchestrator_req.Attachments:
        return func.HttpResponse("Attachments must be non-empty", status_code=400)

    att = _pick_invoice_attachment(orchestrator_req.Attachments)
    try:
        extraction = _call_extraction_api(att.FileUrl)
    except requests.RequestException as exc:
        return func.HttpResponse(f"Failed to call extraction API: {exc}", status_code=502)

    validation = _compare(extraction, orchestrator_req.Invoice)
    out = OrchestratorResponse(Extraction=extraction, Validation=validation)
    return func.HttpResponse(out.model_dump_json(), status_code=200, mimetype="application/json")
