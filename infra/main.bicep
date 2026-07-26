/*
  Execution Agent — Azure Infrastructure
  ========================================
  Top-level deployment. Orchestrates all modules and outputs the
  values needed to populate .env.

  Deploy (dev — OpenAI only, no App Service):
    az group create --name rg-exa-dev --location eastus
    az deployment group create \
      --resource-group rg-exa-dev \
      --template-file infra/main.bicep \
      --parameters infra/params/dev.bicepparam

  Deploy (dev — full stack including App Service):
    az deployment group create \
      --resource-group rg-exa-dev \
      --template-file infra/main.bicep \
      --parameters infra/params/dev.bicepparam \
      --parameters deployAppService=true

  After deploy, copy outputs into .env:
    az deployment group show \
      --resource-group rg-exa-dev \
      --name main \
      --query properties.outputs

  Prefer using deploy.ps1 which handles all of the above automatically.
*/

@description('Environment name — used to select SKUs and name resources.')
@allowed(['dev', 'prod'])
param environment string

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Short name used in all resource names. Keep to 8 chars max.')
@maxLength(8)
param projectName string = 'exa'

@description('Azure OpenAI chat model deployment name.')
param chatModelName string = 'gpt-4o'

@description('Deploy App Service resources. Set to false for local-only dev (default). Flip to true for cloud deploy.')
param deployAppService bool = false

// ── Name tokens ───────────────────────────────────────────────────────────────
// uniqueString produces a deterministic 13-char hash from the resource group id.
// This ensures globally unique names without manual coordination.
var suffix = uniqueString(resourceGroup().id)
var shortSuffix = take(suffix, 6)

var names = {
  openai: 'oai-${projectName}-${environment}-${shortSuffix}'
  appServicePlan: 'asp-${projectName}-${environment}'
  backendApp: 'app-${projectName}-api-${environment}-${shortSuffix}'
  frontendApp: 'app-${projectName}-web-${environment}-${shortSuffix}'
  keyVault: 'kv-${projectName}-${environment}-${shortSuffix}'
}

// ── Modules ───────────────────────────────────────────────────────────────────

module openai 'modules/openai.bicep' = {
  name: 'openai-deploy'
  params: {
    name: names.openai
    location: location
    chatDeploymentName: chatModelName
    environment: environment
  }
}

module keyVault 'modules/keyvault.bicep' = {
  name: 'keyvault-deploy'
  params: {
    name: names.keyVault
    location: location
    environment: environment
  }
}

module appService 'modules/app-service.bicep' = if (deployAppService) {
  name: 'appservice-deploy'
  params: {
    planName: names.appServicePlan
    backendAppName: names.backendApp
    frontendAppName: names.frontendApp
    location: location
    environment: environment
    openaiEndpoint: openai.outputs.endpoint
    openaiChatDeployment: chatModelName
    keyVaultName: names.keyVault
  }
}

// ── Outputs — copy these into .env ────────────────────────────────────────────

@description('Paste into AZURE_OPENAI_ENDPOINT in .env')
output openaiEndpoint string = openai.outputs.endpoint

@description('Paste into AZURE_OPENAI_CHAT_DEPLOYMENT in .env')
output chatDeploymentName string = chatModelName

@description('Backend App Service URL (empty if deployAppService is false)')
output backendUrl string = deployAppService ? appService.outputs.backendUrl : 'localhost:3001 (run locally)'

@description('Frontend App Service URL (empty if deployAppService is false)')
output frontendUrl string = deployAppService ? appService.outputs.frontendUrl : 'localhost:3000 (run locally)'

@description('Key Vault URI — use for secret references in prod')
output keyVaultUri string = keyVault.outputs.uri
