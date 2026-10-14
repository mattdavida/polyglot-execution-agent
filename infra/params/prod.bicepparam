using '../main.bicep'

// Prod environment — higher SKUs, higher TPM capacity.
// App Service B2 (2 cores, 3.5 GB RAM) | OpenAI 80K TPM
//
// Always deploy with deployAppService=true in prod.

param environment = 'prod'
param location = 'eastus'
param projectName = 'exa'
param chatModelName = 'gpt-4o'
param deployAppService = true
