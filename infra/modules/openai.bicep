/*
  Azure OpenAI module — Execution Agent
  ──────────────────────────────────────
  Provisions:
    - Azure OpenAI account (S0, the only available SKU)
    - Chat model deployment (gpt-5.4 / GlobalStandard)

  No embedding deployment — this project has no vector store.
  Capacity is in thousands of tokens per minute (TPM).
  Dev: lower capacity to control costs.
  Prod: higher capacity for throughput.
*/

param name string
param location string
param chatDeploymentName string

@allowed(['dev', 'prod'])
param environment string

var chatCapacity = environment == 'prod' ? 80 : 30

// ── Azure OpenAI Account ───────────────────────────────────────────────────────

resource openAIAccount 'Microsoft.CognitiveServices/accounts@2026-03-15-preview' = {
  name: name
  location: location
  kind: 'OpenAI'
  sku: {
    name: 'S0'
  }
  properties: {
    customSubDomainName: name
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: false
  }
}

// ── Chat Model Deployment (gpt-5.4 GlobalStandard) ────────────────────────────

resource chatDeployment 'Microsoft.CognitiveServices/accounts/deployments@2026-03-15-preview' = {
  parent: openAIAccount
  name: chatDeploymentName
  sku: {
    name: 'GlobalStandard'
    capacity: chatCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-5.4'
      version: '2026-03-05'
    }
    versionUpgradeOption: 'OnceCurrentVersionExpired'
  }
}

// ── Outputs ───────────────────────────────────────────────────────────────────

@description('Azure OpenAI endpoint — paste into AZURE_OPENAI_ENDPOINT')
output endpoint string = openAIAccount.properties.endpoint

// API key is intentionally NOT output here — fetched post-deploy by deploy.ps1 via:
//   az cognitiveservices account keys list --name <account> --resource-group <rg> --query key1 -o tsv

output accountName string = openAIAccount.name
output accountId string = openAIAccount.id
