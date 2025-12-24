# Agentic Invoice Validation

Two local Python services:

- **Service 1: Invoice Extraction API** (FastAPI)
- **Service 2: Orchestrator** (Azure Functions Python)

## Folder structure

- `samples/` (PDF samples)
- `services/extraction_api/` (FastAPI extraction)
- `services/orchestrator/` (Azure Functions orchestrator)

## Service 1: Extraction API (FastAPI)

### Install + run

```powershell
cd "./services/extraction_api"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8001
```

### Test extraction only

```powershell
curl -X POST http://127.0.0.1:8001/extract `
  -H "Content-Type: application/json" `
  -d '{"file_path":"./samples/demo-invoice-20tax-1.pdf","file_type":"PDF"}'
```

## Service 2: Orchestrator (Azure Functions)

### Prereqs

- Python 3.10+
- Azure Functions Core Tools installed (`func`)

### Install + run

```powershell
cd "./services/orchestrator"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
func start
```

The orchestrator will listen locally (typically on `http://localhost:7071`).

### (Optional) Use Azure AI Foundry Agent for Validation

The orchestrator can call your Foundry Agent to generate the `Validation` block. It will still call the local Extraction API for `Extraction`.

Set environment variables (PowerShell examples):

```powershell
# Required
$env:FOUNDRY_PROJECT_ENDPOINT = "https://<resource>.services.ai.azure.com/api/projects/<project_name>"
$env:FOUNDRY_AGENT_NAME = "invoice-validator-agent-v2"
$env:FOUNDRY_AGENT_VERSION = "1"

# Optional
$env:FOUNDRY_API_VERSION = "2025-11-15-preview"

# Auth (local): set a bearer token, or rely on DefaultAzureCredential if your dev environment is configured.
$env:AZURE_AI_AUTH_TOKEN = "<paste access token>"
```

If `USE_FOUNDRY_AGENT=true`, the orchestrator will fail the request if the agent call fails.
If you only set `FOUNDRY_PROJECT_ENDPOINT` (without `USE_FOUNDRY_AGENT=true`), it will fall back to local validation on agent failure.

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
      "InvoiceLineItems": []
    },
    "Attachments": [
      {
        "DocumentType": "Invoice",
        "FileName": "demo-invoice-20tax-1.pdf",
        "FileType": "PDF",
        "FileUrl": "./samples/demo-invoice-20tax-1.pdf"
      }
    ]
  }'
```

## Azure deployment (recommended)

Deploy the services separately:

- **Orchestrator**: Azure Function App (Python)
- **Extraction API**: Azure App Service / Container Apps (FastAPI)

For Foundry calls from Azure, prefer **Managed Identity**:

- Enable **System Assigned Managed Identity** on the Function App
- Grant that identity access to your Foundry project/resource (RBAC)
- Configure Function App settings:
  - `FOUNDRY_PROJECT_ENDPOINT`
  - `FOUNDRY_AGENT_NAME`
  - `FOUNDRY_AGENT_VERSION`
  - `FOUNDRY_API_VERSION` (optional)
  - `USE_FOUNDRY_AGENT=true`
  - `EXTRACTION_API_URL` (point to your deployed extraction API)

With Managed Identity, you do **not** need to set `AZURE_AI_AUTH_TOKEN` in Azure.

## Deploy from GitHub (CLI + GitHub Actions)

This repo includes GitHub Actions workflows that deploy automatically on pushes to the `azure_integration` branch:

- `.github/workflows/deploy-extraction-api.yml`
- `.github/workflows/deploy-orchestrator-function.yml`

### 1) Prereqs

- Azure CLI installed (`az`)
- Azure Functions Core Tools installed (`func`)
- Logged in:

```powershell
az login
az account show
```

### 2) Create Azure resources (CLI)

Pick a resource group (you can reuse your existing one that hosts Foundry).

```powershell
$rg = "<your-resource-group>"
$loc = "eastus"

# App Service (Extraction API)
$plan = "plan-invoice-validation"
$extractionApp = "invoice-extraction-api-<unique>"

az appservice plan create --name $plan --resource-group $rg --location $loc --is-linux --sku B1
az webapp create --name $extractionApp --resource-group $rg --plan $plan --runtime "PYTHON|3.11"
az webapp config appsettings set --name $extractionApp --resource-group $rg --settings SCM_DO_BUILD_DURING_DEPLOYMENT=1
az webapp config set --name $extractionApp --resource-group $rg --startup-file "python -m uvicorn app.main:app --host 0.0.0.0 --port 8000"

# Function App (Orchestrator)
$storage = "invvalstor<unique>"   # lowercase, globally unique
$funcApp = "invoice-orchestrator-<unique>"

az storage account create --name $storage --resource-group $rg --location $loc --sku Standard_LRS
az functionapp create --resource-group $rg --consumption-plan-location $loc --runtime python --runtime-version 3.11 --functions-version 4 --name $funcApp --storage-account $storage
az functionapp config appsettings set --name $funcApp --resource-group $rg --settings SCM_DO_BUILD_DURING_DEPLOYMENT=1
```

### 3) Configure Function App settings

```powershell
$extractionUrl = "https://$extractionApp.azurewebsites.net"

az functionapp config appsettings set --name $funcApp --resource-group $rg --settings \
  EXTRACTION_API_URL=$extractionUrl \
  FOUNDRY_PROJECT_ENDPOINT="https://<resource>.services.ai.azure.com/api/projects/<project_name>" \
  FOUNDRY_AGENT_NAME="invoice-validator-agent-v2" \
  FOUNDRY_AGENT_VERSION="1" \
  FOUNDRY_API_VERSION="2025-11-15-preview" \
  USE_FOUNDRY_AGENT=true
```

### 4) Enable Managed Identity (recommended for Azure)

```powershell
az functionapp identity assign --name $funcApp --resource-group $rg
```

Then grant that identity permission to call your Foundry project/resource (RBAC in Azure Portal → Foundry/Azure AI resource → Access control (IAM)).

### 5) Wire GitHub Actions to your Azure apps

In your GitHub repo:

1) Settings → Secrets and variables → Actions
2) Add **Repository variables**:
   - `EXTRACTION_APP_SERVICE_NAME` = your `$extractionApp`
   - `ORCHESTRATOR_FUNCTION_APP_NAME` = your `$funcApp`
3) Add **Repository secrets** (publish profiles):

```powershell
# Extraction API publish profile
az webapp deployment list-publishing-profiles --name $extractionApp --resource-group $rg --xml > extraction.publishsettings

# Orchestrator Function publish profile
az functionapp deployment list-publishing-profiles --name $funcApp --resource-group $rg --xml > orchestrator.publishsettings
```

Copy the full contents into GitHub Secrets:
- `EXTRACTION_APP_PUBLISH_PROFILE`
- `ORCHESTRATOR_FUNC_PUBLISH_PROFILE`

After that, any push to `azure_integration` will deploy automatically.
