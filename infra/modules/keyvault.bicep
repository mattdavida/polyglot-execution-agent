/*
  Azure Key Vault module — Execution Agent
  ─────────────────────────────────────────
  Provisions a Key Vault for secret storage in production.

  In prod, App Service app settings reference Key Vault secrets using the
  @Microsoft.KeyVault(SecretUri=...) syntax — secrets never appear in plain
  text in the portal or in deployment outputs.

  In dev, .env is sufficient — Key Vault is still provisioned so you can
  start storing secrets there early without a prod config change later.

  Access model: RBAC (recommended over legacy access policies).
  The App Service managed identity is granted "Key Vault Secrets User" role
  in the app-service module after both resources exist.
*/

param name string
param location string

@allowed(['dev', 'prod'])
param environment string

// ── Key Vault ─────────────────────────────────────────────────────────────────

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: name
  location: location
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: environment == 'prod' ? 90 : 7
    enabledForDeployment: false
    enabledForTemplateDeployment: true
    publicNetworkAccess: 'Enabled'
  }
}

// ── Secret placeholders ───────────────────────────────────────────────────────
// Populate real values post-deploy via:
//   az keyvault secret set --vault-name <name> --name AZURE-OPENAI-API-KEY --value <key>

resource secretOpenAIKey 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'AZURE-OPENAI-API-KEY'
  properties: {
    value: 'REPLACE-AFTER-DEPLOY'
    contentType: 'text/plain'
    attributes: { enabled: true }
  }
}

// ── Outputs ───────────────────────────────────────────────────────────────────

output vaultName string = keyVault.name
output uri string = keyVault.properties.vaultUri
output id string = keyVault.id
