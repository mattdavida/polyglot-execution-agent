<#
  cleanup.ps1 — Tear down a dev/prod environment completely.

  Usage:
    .\infra\cleanup.ps1 -Environment dev
    .\infra\cleanup.ps1 -Environment prod   (requires extra confirmation)

  What it does:
    1. Deletes the resource group (and everything in it)
    2. Purges the soft-deleted Key Vault so the name is free to reuse
    3. Purges the soft-deleted OpenAI account so the custom subdomain is free
#>

param(
    [Parameter(Mandatory)][ValidateSet('dev', 'prod')]
    [string]$Environment
)

$ProjectName   = 'exa'
$ResourceGroup = "rg-$ProjectName-$Environment"

# Safety gate for prod
if ($Environment -eq 'prod') {
    Write-Host 'WARNING: You are about to delete the PRODUCTION environment.' -ForegroundColor Red
    $confirm = Read-Host "Type 'delete prod' to confirm"
    if ($confirm -ne 'delete prod') {
        Write-Host 'Aborted.' -ForegroundColor Yellow
        exit 0
    }
}

Write-Host ''
Write-Host "This will permanently delete resource group: $ResourceGroup" -ForegroundColor Yellow
Write-Host 'All resources inside it (OpenAI, Key Vault, App Service) will be destroyed.'
$confirm = Read-Host 'Proceed? (y/N)'
if ($confirm -ne 'y' -and $confirm -ne 'Y') {
    Write-Host 'Aborted.' -ForegroundColor Yellow
    exit 0
}

Write-Host ''

# --- Find resource names before deleting the RG ------------------------------
Write-Host 'Finding resources in resource group...' -ForegroundColor Cyan
$kvName      = az keyvault list --resource-group $ResourceGroup --query '[0].name'     -o tsv 2>$null
$kvLocation  = az keyvault list --resource-group $ResourceGroup --query '[0].location' -o tsv 2>$null
$oaiName     = az cognitiveservices account list --resource-group $ResourceGroup --query '[0].name'     -o tsv 2>$null
$oaiLocation = az cognitiveservices account list --resource-group $ResourceGroup --query '[0].location' -o tsv 2>$null

# --- Delete the resource group -----------------------------------------------
Write-Host "Deleting resource group '$ResourceGroup'..." -ForegroundColor Cyan
az group delete --name $ResourceGroup --yes --no-wait
Write-Host 'Resource group deletion initiated (running in background).' -ForegroundColor Green

# --- Wait for RG deletion before purging ------------------------------------
Write-Host 'Waiting for resource group to finish deleting (up to 3 min)...' -ForegroundColor Cyan
$timeout = 180
$elapsed = 0
while ($elapsed -lt $timeout) {
    $exists = az group exists --name $ResourceGroup
    if ($exists -eq 'false') { break }
    Start-Sleep -Seconds 10
    $elapsed += 10
    Write-Host "  ...still deleting ($elapsed s elapsed)"
}

# --- Purge soft-deleted Key Vault so name is free to reuse ------------------
if ($kvName) {
    Write-Host ''
    Write-Host "Purging soft-deleted Key Vault '$kvName'..." -ForegroundColor Cyan
    az keyvault purge --name $kvName --location $kvLocation 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host 'Key Vault purged.' -ForegroundColor Green
    } else {
        Write-Host 'Key Vault purge skipped (may not be in soft-deleted state yet).' -ForegroundColor Yellow
        Write-Host "If redeploy fails on KV name conflict, run:" -ForegroundColor Yellow
        Write-Host "  az keyvault purge --name $kvName --location $kvLocation" -ForegroundColor Cyan
    }
}

# --- Purge soft-deleted OpenAI account so subdomain is free -----------------
if ($oaiName) {
    Write-Host ''
    Write-Host "Purging soft-deleted OpenAI account '$oaiName'..." -ForegroundColor Cyan
    az cognitiveservices account purge --name $oaiName --resource-group $ResourceGroup --location $oaiLocation 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host 'OpenAI account purged.' -ForegroundColor Green
    } else {
        Write-Host 'OpenAI purge skipped (may not be in soft-deleted state yet).' -ForegroundColor Yellow
        Write-Host 'If redeploy fails on subdomain conflict, run:' -ForegroundColor Yellow
        Write-Host "  az cognitiveservices account purge --name $oaiName --resource-group $ResourceGroup --location $oaiLocation" -ForegroundColor Cyan
    }
}

Write-Host ''
Write-Host 'Cleanup complete. You can now redeploy with:' -ForegroundColor Green
Write-Host "  .\infra\deploy.ps1 -Environment $Environment" -ForegroundColor Cyan
