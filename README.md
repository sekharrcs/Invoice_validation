# Agentic Invoice Validation (Local)

Two local Python services:

- **Service 1: Invoice Extraction API** (FastAPI)
- **Service 2: Orchestrator** (Azure Functions Python)

## Folder structure

- `samples/` (PDF samples)
- `services/extraction_api/` (FastAPI extraction)
- `services/orchestrator/` (Azure Functions orchestrator)

## Service 1: Extraction API

### Install + run

```powershell
cd "c:\Users\chandrar\Invoice_validation\services\extraction_api"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8001
```

### Test extraction only

```powershell
curl -X POST http://127.0.0.1:8001/extract `
  -H "Content-Type: application/json" `
  -d '{"file_path":"c:\\Users\\chandrar\\Invoice_validation\\samples\\demo-invoice-20tax-1.pdf","file_type":"PDF"}'
```

## Service 2: Orchestrator (Azure Functions)

### Prereqs

- Python 3.10+
- Azure Functions Core Tools installed (`func`)

### Install + run

```powershell
cd "c:\Users\chandrar\Invoice_validation\services\orchestrator"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
func start
```

The orchestrator will listen locally (typically on `http://localhost:7071`).

## End-to-end test (Orchestrator -> Extraction API)

With both services running:

```powershell
curl -X POST http://localhost:7071/api/orchestrators/process_invoice `
  -H "Content-Type: application/json" `
  -d '{
    "query": "validate invoice",
    "Invoice": {
      "InvoiceNumber": "UNKNOWN",
      "OrderNumber": null,
      "InvoiceDate": "2025-01-01",
      "InvoiceBaseAmount": 0,
      "InvoiceWithTaxAmount": 0,
      "LineItems": []
    },
    "Attachments": [
      {
        "DocumentType": "Invoice",
        "FileName": "demo-invoice-20tax-1.pdf",
        "FileType": "PDF",
        "FileUrl": "c:\\Users\\chandrar\\Invoice_validation\\samples\\demo-invoice-20tax-1.pdf"
      }
    ]
  }'
```
