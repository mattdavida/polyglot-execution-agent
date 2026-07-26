using '../main.bicep'

// Dev environment — cheap SKUs, destroy and recreate freely.
// App Service B1 (1 core, 1.75 GB RAM) | OpenAI 30K TPM
//
// deployAppService defaults to false — OpenAI + Key Vault only for local dev.
// Flip to true when you're ready to deploy the full stack to Azure.

param environment = 'dev'
param location = 'eastus'
param projectName = 'exa'
param chatModelName = 'gpt-4o'
