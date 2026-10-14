<#
.SYNOPSIS
    Deploy the Execution Agent infrastructure to Azure.

.DESCRIPTION
    Creates the resource group if it does not exist, runs a Bicep what-if preview,
    then deploys on confirmation. Prints all values needed for .env at the end.

    Default deploy (dev, OpenAI + Key Vault only — no App Service):
        .\infra\deploy.ps1

    Deploy with App Service (full cloud stack):
        .\infra\deploy.ps1 -DeployAppService

    Deploy prod:
        .\infra\deploy.ps1 -Environment prod -DeployAppService

.PARAMETER Environment
    Target environment: 'dev' or 'prod'. Default: dev.

.PARAMETER DeployAppService
    Include App Service resources (backend + frontend). Off by default for
    local dev where you just need the Azure OpenAI resource.

.PARAMETER SkipWhatIf
    Skip the what-if preview and deploy immediately.

.EXAMPLE
    # Preview what will be created (no changes)
    .\infra\deploy.ps1

    # Deploy dev — OpenAI + Key Vault only
    .\infra\deploy.ps1 -SkipWhatIf

    # Deploy dev — full stack
    .\infra\deploy.ps1 -DeployAppService -SkipWhatIf

    # Deploy prod
    .\infra\deploy.ps1 -Environment prod -DeployAppService -SkipWhatIf
#>

param(
    [ValidateSet('dev', 'prod')]
    [string]$Environment = 'dev',

    [switch]$DeployAppService,

    [switch]$SkipWhatIf
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# --- Config ------------------------------------------------------------------
$ProjectName    = 'exa'
$ResourceGroup  = "rg-$ProjectName-$Environment"
$Location       = 'eastus'
$DeploymentName = "$ProjectName-$Environment-$(Get-Date -Format 'yyyyMMdd-HHmm')"
$TemplateFile   = Join-Path $PSScriptRoot 'main.bicep'
$ParamsFile     = Join-Path $PSScriptRoot "params\$Environment.bicepparam"

# --- Pre-flight checks -------------------------------------------------------
Write-Host ''
Write-Host '=== Execution Agent — Bicep Deploy ===' -ForegroundColor Cyan
Write-Host "Environment  : $Environment"
Write-Host "Resource Grp : $ResourceGroup"
Write-Host "Location     : $Location"
Write-Host "Template     : $TemplateFile"
Write-Host "Params       : $ParamsFile"
Write-Host "App Service  : $($DeployAppService.IsPresent)"
Write-Host ''

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw 'Azure CLI not found. Install from https://aka.ms/installazurecliwindows and re-run.'
}

$accountJson = az account show 2>$null
if (-not $accountJson) {
    Write-Host 'Not logged in to Azure. Running az login...' -ForegroundColor Yellow
    az login | Out-Null
}
$account = az account show | ConvertFrom-Json
Write-Host "Logged in as  : $($account.user.name)" -ForegroundColor Green
Write-Host "Subscription  : $($account.name) ($($account.id))"
Write-Host ''

# --- Create resource group if needed -----------------------------------------
$rgExists = az group exists --name $ResourceGroup
if ($rgExists -eq 'false') {
    Write-Host "Creating resource group '$ResourceGroup' in '$Location'..." -ForegroundColor Yellow
    az group create --name $ResourceGroup --location $Location | Out-Null
    Write-Host 'Resource group created.' -ForegroundColor Green
} else {
    Write-Host "Resource group '$ResourceGroup' already exists." -ForegroundColor Green
}
Write-Host ''

# --- Build deployment arguments ----------------------------------------------
$deployArgs = @(
    '--resource-group', $ResourceGroup,
    '--template-file', $TemplateFile,
    '--parameters', $ParamsFile
)

if ($DeployAppService) {
    $deployArgs += '--parameters', 'deployAppService=true'
}

# --- What-if preview ---------------------------------------------------------
if (-not $SkipWhatIf) {
    Write-Host 'Running what-if preview (no changes made yet)...' -ForegroundColor Cyan
    az deployment group what-if @deployArgs
    Write-Host ''
    $confirm = Read-Host 'Proceed with deployment? (y/N)'
    if ($confirm -ne 'y' -and $confirm -ne 'Y') {
        Write-Host 'Deployment cancelled.' -ForegroundColor Yellow
        exit 0
    }
    Write-Host ''
}

# --- Deploy ------------------------------------------------------------------
Write-Host 'Deploying... (OpenAI provisioning takes 3-5 minutes)' -ForegroundColor Cyan
$resultJson = az deployment group create `
    @deployArgs `
    --name $DeploymentName `
    --output json

if ($LASTEXITCODE -ne 0) {
    throw 'Deployment failed. Check the Azure portal Activity Log for details.'
}

$result  = $resultJson | ConvertFrom-Json
$outputs = $result.properties.outputs

Write-Host ''
Write-Host 'Deployment succeeded!' -ForegroundColor Green

# --- Fetch the OpenAI API key (not returned in Bicep outputs for security) ---
$openaiAccountName = az resource list `
    --resource-group $ResourceGroup `
    --resource-type 'Microsoft.CognitiveServices/accounts' `
    --query '[0].name' `
    --output tsv

$openaiKey = az cognitiveservices account keys list `
    --name $openaiAccountName `
    --resource-group $ResourceGroup `
    --query 'key1' `
    --output tsv

# --- Extract outputs ---------------------------------------------------------
$openaiEndpoint  = $outputs.openaiEndpoint.value
$chatDeployment  = $outputs.chatDeploymentName.value
$backendUrl      = $outputs.backendUrl.value
$frontendUrl     = $outputs.frontendUrl.value
$keyVaultUri     = $outputs.keyVaultUri.value

# --- Print .env values -------------------------------------------------------
$envBlock = @"

============================================================
 Copy the block below into your .env file
============================================================

AZURE_OPENAI_API_KEY=$openaiKey
AZURE_OPENAI_ENDPOINT=$openaiEndpoint
AZURE_OPENAI_API_VERSION=2024-02-01
AZURE_OPENAI_CHAT_DEPLOYMENT=$chatDeployment

CHECKPOINTS_DB=./checkpoints.db

API_PORT=3001
ALLOWED_ORIGINS=http://localhost:3000

# Key Vault URI - use for secret references in prod
# KEY_VAULT_URI=$keyVaultUri

============================================================

Backend URL  : $backendUrl
Frontend URL : $frontendUrl

Next steps:
  1. Paste the block above into your .env file
  2. .venv\Scripts\Activate.ps1
  3. pip install -r requirements.txt
  4. uvicorn backend.main:app --reload --port 3001
"@

Write-Host $envBlock -ForegroundColor Yellow
