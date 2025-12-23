from __future__ import annotations

import os
import json
from datetime import datetime
from typing import Any, Literal, Optional

import azure.functions as func
import requests
from pydantic import BaseModel, ValidationError

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


class InvoiceLineItemInput(BaseModel):
    LineItemNo: Optional[str] = None
    Product: Optional[str] = None
    Quantity: Any = None
    UnitPrice: Any = None
    Amount: Any = None


class InvoiceInput(BaseModel):
    OrderNumber: Optional[str] = None
    InvoiceNumber: Optional[str] = None
    InvoiceDate: Optional[str] = None
    InvoiceBaseAmount: Any = None
    InvoiceWithTaxAmount: Any = None
    InvoiceLineItems: list[InvoiceLineItemInput] = []


class ValidationFieldResult(BaseModel):
    status: Literal["MATCH", "MISMATCH", "MISSING_IN_EXTRACTION"]
    expected: Any = None
    actual: Any = None


class ValidationResult(BaseModel):
    is_valid: bool
    field_analysis: dict[str, ValidationFieldResult]
    line_items_analysis: list[dict[str, Any]]
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


def _maybe_number(val: Any) -> Any:
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        s = val.strip()
        if s == "":
            return None
        try:
            return float(s.replace(",", ""))
        except ValueError:
            return val
    return val


def _maybe_iso_date(val: Any) -> Any:
    if not isinstance(val, str):
        return val
    s = val.strip()
    if s == "":
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return val


def _normalize(val: Any) -> Any:
    if val is None:
        return None
    if isinstance(val, str) and val.strip() == "":
        return None
    v = _maybe_number(val)
    v = _maybe_iso_date(v)
    return v


def _val_status(expected: Any, extracted: Any) -> Literal["MATCH", "MISMATCH", "MISSING_IN_EXTRACTION"]:
    expected_n = _normalize(expected)
    extracted_n = _normalize(extracted)

    # If caller didn't provide an expected value, don't treat it as invalid.
    if expected_n is None:
        return "MATCH"

    if extracted_n is None:
        return "MISSING_IN_EXTRACTION"

    if isinstance(expected_n, (int, float)) or isinstance(extracted_n, (int, float)):
        return "MATCH" if _num_equal(expected_n, extracted_n) else "MISMATCH"

    return "MATCH" if str(expected_n).strip() == str(extracted_n).strip() else "MISMATCH"


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


def _get_foundry_bearer_token() -> str:
    token = os.getenv("AZURE_AI_AUTH_TOKEN") or os.getenv("FOUNDRY_AUTH_TOKEN")
    if token:
        return token

    try:
        from azure.identity import DefaultAzureCredential  # type: ignore

        return DefaultAzureCredential().get_token("https://ai.azure.com/.default").token
    except Exception as exc:
        raise RuntimeError(
            "Missing Foundry auth token. Set AZURE_AI_AUTH_TOKEN (or FOUNDRY_AUTH_TOKEN), "
            "or configure DefaultAzureCredential (Managed Identity in Azure, or developer login locally)."
        ) from exc


def _pick_first_json_object(text: str) -> Any:
    """Extract the first JSON object/array from a string."""
    s = text.strip()
    if not s:
        raise ValueError("Empty response")

    # Fast path
    if s[0] in "{[":
        return json.loads(s)

    # Heuristic: find first '{' or '[' and last matching '}' or ']'
    start_candidates = [i for i in (s.find("{"), s.find("[")) if i != -1]
    if not start_candidates:
        raise ValueError("No JSON found in response")
    start = min(start_candidates)

    end_obj = s.rfind("}")
    end_arr = s.rfind("]")
    end = max(end_obj, end_arr)
    if end <= start:
        raise ValueError("No complete JSON found in response")

    return json.loads(s[start : end + 1])


def _build_mismatch_summary(validation: ValidationResult) -> str:
    mismatches: list[str] = []

    for k, v in (validation.field_analysis or {}).items():
        if v.status != "MATCH":
            if _normalize(v.expected) is not None:
                mismatches.append(k)

    for item in (validation.line_items_analysis or []):
        try:
            status = str(item.get("status") or "")
            ln = item.get("line_number")
        except AttributeError:
            continue
        if status != "MATCH":
            mismatches.append(f"InvoiceLineItems[{ln}]")

    uniq = sorted(set(mismatches))
    if not uniq:
        return "The invoice is valid. All header fields and line items match the expected values."
    return f"Mismatched fields: {', '.join(uniq)}"


def _normalize_foundry_validation_shape(parsed: Any) -> Any:
    """Best-effort coercions so Foundry output fits our schema reliably."""
    if not isinstance(parsed, dict):
        return parsed

    out = dict(parsed)
    lia = out.get("line_items_analysis")
    if isinstance(lia, dict) or lia is None:
        out["line_items_analysis"] = []
    return out


def _ensure_required_field_keys(validation: ValidationResult, expected_invoice: dict[str, Any], extraction: dict[str, Any]) -> None:
    required_keys = [
        "InvoiceNumber",
        "OrderNumber",
        "InvoiceDate",
        "InvoiceBaseAmount",
        "InvoiceWithTaxAmount",
    ]

    # Ensure field_analysis exists
    if validation.field_analysis is None:
        validation.field_analysis = {}

    for key in required_keys:
        if key in validation.field_analysis:
            continue
        expected_val = expected_invoice.get(key)
        actual_val = extraction.get(key)
        status = _val_status(expected_val, actual_val)
        validation.field_analysis[key] = ValidationFieldResult(status=status, expected=expected_val, actual=actual_val)


def _finalize_foundry_validation(validation: ValidationResult, expected_invoice: dict[str, Any], extraction: dict[str, Any]) -> ValidationResult:
    # Ensure required header keys exist
    _ensure_required_field_keys(validation, expected_invoice, extraction)

    # Enforce exact required summary wording
    summary = _build_mismatch_summary(validation)
    validation.summary = summary
    validation.is_valid = summary.startswith("The invoice is valid")
    return validation


def _call_foundry_agent_validate(
    *,
    query: str,
    extraction: dict[str, Any],
    expected_invoice: dict[str, Any],
) -> ValidationResult:
    project_endpoint = (os.getenv("FOUNDRY_PROJECT_ENDPOINT") or "").rstrip("/")
    if not project_endpoint:
        raise RuntimeError("FOUNDRY_PROJECT_ENDPOINT not set")

    api_version = os.getenv("FOUNDRY_API_VERSION", "2025-11-15-preview")
    agent_name = os.getenv("FOUNDRY_AGENT_NAME")
    agent_version = (os.getenv("FOUNDRY_AGENT_VERSION") or "latest").strip()
    if not agent_name:
        raise RuntimeError("Set FOUNDRY_AGENT_NAME")

    token = _get_foundry_bearer_token()
    url = f"{project_endpoint}/openai/responses?api-version={api_version}"

    instructions = (
        "You are an invoice validation assistant. Compare `expected_invoice` with `extraction`. "
        "Return ONLY a JSON object matching this schema exactly (no markdown, no extra keys):\n"
        "{\n"
        '  "is_valid": boolean,\n'
        '  "field_analysis": { "InvoiceNumber": {"status": "MATCH|MISMATCH|MISSING_IN_EXTRACTION", "expected": any, "actual": any}, ... },\n'
        '  "line_items_analysis": [ {"line_number": number, "status": "MATCH|MISMATCH|MISSING_IN_EXTRACTION", "field_analysis": {"Description": {"status": "MATCH|MISMATCH|MISSING_IN_EXTRACTION", "expected": any, "actual": any}, "Quantity": {...}, "UnitPrice": {...}, "Amount": {...}} } ],\n'
        '  "summary": string\n'
        "}\n"
        "Rules: If an expected field is null/empty, treat it as MATCH. For numbers, allow small rounding differences. "
        "For dates, normalize to YYYY-MM-DD if possible. "
        "ALWAYS return line_items_analysis as a JSON array (use [] if none). "
        "If is_valid is true, summary MUST be exactly: 'The invoice is valid. All header fields and line items match the expected values.'. "
        "If is_valid is false, summary MUST be exactly: 'Mismatched fields: <comma-separated list>'."
    )

    user_input = {
        "query": query,
        "expected_invoice": expected_invoice,
        "extraction": extraction,
    }

    base_payload: dict[str, Any] = {
        "input": f"{instructions}\n\nDATA:\n{json.dumps(user_input, ensure_ascii=False)}",
        "agent": {"type": "agent_reference", "name": agent_name, "version": agent_version},
    }

    # Attempt to force JSON output when supported.
    prefer_json = os.getenv("FOUNDRY_RESPONSE_FORMAT", "json_object").strip().lower()
    if prefer_json in {"json_object", "none"}:
        if prefer_json == "json_object":
            base_payload["response_format"] = {"type": "json_object"}

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    last_err: Exception | None = None
    for payload in (
        base_payload,
        # Fallback: remove response_format if server rejects it
        {k: v for k, v in base_payload.items() if k != "response_format"},
    ):
        if payload is None:
            continue
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=90)
            if resp.status_code >= 400:
                raise RuntimeError(f"Foundry call failed ({resp.status_code}): {resp.text}")

            data = resp.json()

            # Try to locate assistant text content.
            text_parts: list[str] = []
            if isinstance(data, dict):
                output = data.get("output")
                if isinstance(output, list):
                    for item in output:
                        if not isinstance(item, dict):
                            continue
                        content = item.get("content")
                        if isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") in {"output_text", "text"}:
                                    t = c.get("text")
                                    if isinstance(t, str):
                                        text_parts.append(t)
                        elif isinstance(content, str):
                            text_parts.append(content)
                elif isinstance(output, str):
                    text_parts.append(output)

            raw_text = "\n".join([t for t in text_parts if t.strip()])
            if not raw_text:
                # Some responses APIs return a top-level 'output_text'
                raw_text = str(data.get("output_text") or "").strip()
            if not raw_text:
                raise ValueError("No assistant text found in Foundry response")

            parsed = _normalize_foundry_validation_shape(_pick_first_json_object(raw_text))
            validated = ValidationResult.model_validate(parsed)
            return _finalize_foundry_validation(validated, expected_invoice, extraction)
        except Exception as exc:
            last_err = exc
            continue

    raise RuntimeError(f"Unable to parse Foundry agent validation response: {last_err}")


def _parse_invoice_input(raw: dict[str, Any]) -> InvoiceInput:
    """Accept either the drafted schema or the internal schema.

    Drafted input uses InvoiceLineItems. If caller sends LineItems already,
    we map it into InvoiceLineItems shape.
    """
    if "InvoiceLineItems" in raw:
        return InvoiceInput.model_validate(raw)

    # Back-compat: allow LineItems with extraction-style keys.
    mapped_items = []
    for li in (raw.get("LineItems") or []):
        mapped_items.append(
            {
                "LineItemNo": str(li.get("LineItemNo") or ""),
                "Product": li.get("Description") or li.get("Product"),
                "Quantity": li.get("Quantity"),
                "UnitPrice": li.get("UnitPrice"),
                "Amount": li.get("Amount"),
            }
        )

    merged = dict(raw)
    merged["InvoiceLineItems"] = mapped_items
    return InvoiceInput.model_validate(merged)


def _compare(extracted: dict[str, Any], expected_invoice: dict[str, Any]) -> ValidationResult:
    inv = _parse_invoice_input(expected_invoice)

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

    expected_dict = inv.model_dump()

    for expected_key, extracted_key in field_map.items():
        expected_val = expected_dict.get(expected_key)
        extracted_val = extracted.get(extracted_key)

        status = _val_status(expected_val, extracted_val)
        field_analysis[expected_key] = ValidationFieldResult(
            status=status,
            expected=expected_val,
            actual=extracted_val,
        )
        if status != "MATCH":
            # Only count as mismatch when an expected value was provided.
            if _normalize(expected_val) is not None:
                mismatches.append(expected_key)

    # Line items: positional comparison, but output matches drafted schema.
    expected_items = inv.InvoiceLineItems
    extracted_items = extracted.get("LineItems") or []

    line_items_analysis: list[dict[str, Any]] = []
    max_len = max(len(expected_items), len(extracted_items))

    for i in range(max_len):
        exp = expected_items[i] if i < len(expected_items) else None
        ext = extracted_items[i] if i < len(extracted_items) else None

        line_number = i + 1
        if exp is not None and exp.LineItemNo and str(exp.LineItemNo).strip().isdigit():
            line_number = int(str(exp.LineItemNo).strip())

        if ext is None:
            line_items_analysis.append(
                {
                    "line_number": line_number,
                    "status": "MISSING_IN_EXTRACTION",
                    "field_analysis": {
                        "Description": {"status": "MISSING_IN_EXTRACTION", "expected": (exp.Product if exp else None), "actual": None},
                        "Quantity": {"status": "MISSING_IN_EXTRACTION", "expected": (exp.Quantity if exp else None), "actual": None},
                        "UnitPrice": {"status": "MISSING_IN_EXTRACTION", "expected": (exp.UnitPrice if exp else None), "actual": None},
                        "Amount": {"status": "MISSING_IN_EXTRACTION", "expected": (exp.Amount if exp else None), "actual": None},
                    },
                }
            )
            if exp is not None:
                mismatches.append(f"InvoiceLineItems[{line_number}]")
            continue

        if exp is None:
            # Ignore extra extracted items for now.
            continue

        expected_desc = exp.Product
        expected_qty = exp.Quantity
        expected_unit = exp.UnitPrice
        expected_amt = exp.Amount

        per_field = {
            "Description": {
                "status": _val_status(expected_desc, ext.get("Description")),
                "expected": expected_desc,
                "actual": ext.get("Description"),
            },
            "Quantity": {
                "status": _val_status(expected_qty, ext.get("Quantity")),
                "expected": expected_qty,
                "actual": ext.get("Quantity"),
            },
            "UnitPrice": {
                "status": _val_status(expected_unit, ext.get("UnitPrice")),
                "expected": expected_unit,
                "actual": ext.get("UnitPrice"),
            },
            "Amount": {
                "status": _val_status(expected_amt, ext.get("Amount")),
                "expected": expected_amt,
                "actual": ext.get("Amount"),
            },
        }

        item_ok = all(v["status"] == "MATCH" for v in per_field.values())
        if not item_ok:
            # Only count as mismatch if any expected value was provided.
            if any(_normalize(x) is not None for x in [expected_desc, expected_qty, expected_unit, expected_amt]):
                mismatches.append(f"InvoiceLineItems[{line_number}]")

        line_items_analysis.append(
            {
                "line_number": line_number,
                "status": "MATCH" if item_ok else "MISMATCH",
                "field_analysis": per_field,
            }
        )

    is_valid = len(sorted(set(mismatches))) == 0
    if is_valid:
        summary = "The invoice is valid. All header fields and line items match the expected values."
    else:
        summary = f"Mismatched fields: {', '.join(sorted(set(mismatches)))}"

    return ValidationResult(
        is_valid=is_valid,
        field_analysis=field_analysis,
        line_items_analysis=line_items_analysis,
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

    use_foundry = (os.getenv("USE_FOUNDRY_AGENT") or "").strip().lower() in {"1", "true", "yes", "on"}
    if use_foundry or os.getenv("FOUNDRY_PROJECT_ENDPOINT"):
        try:
            validation = _call_foundry_agent_validate(
                query=orchestrator_req.query,
                extraction=extraction,
                expected_invoice=orchestrator_req.Invoice,
            )
        except Exception as exc:
            # Fall back to local compare unless explicitly forced.
            if use_foundry:
                return func.HttpResponse(f"Foundry agent validation failed: {exc}", status_code=502)
            validation = _compare(extraction, orchestrator_req.Invoice)
    else:
        validation = _compare(extraction, orchestrator_req.Invoice)

    out = OrchestratorResponse(Extraction=extraction, Validation=validation)
    return func.HttpResponse(out.model_dump_json(), status_code=200, mimetype="application/json")
