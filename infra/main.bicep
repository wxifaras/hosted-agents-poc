targetScope = 'subscription'

@minLength(1)
@maxLength(40)
param environmentName string = 'hosted-agents-poc-dev'

@allowed([
  'northcentralus'
  'eastus'
  'westus'
])
param location string = 'northcentralus'

param enableAppConfiguration bool = false

@minLength(1)
@description('OpenAI-format Foundry catalog model. Verify version and regional availability before changing.')
param modelName string = 'gpt-5-mini'

@minLength(1)
param modelVersion string = '2025-08-07'

@minLength(1)
@description('Azure deployment resource name used by the application, independent of the catalog model name.')
param modelDeploymentName string = 'gpt-5-mini'

@minValue(1)
@maxValue(10)
@description('Model deployment capacity units, not a spending limit or a fixed TPM assumption.')
param modelCapacity int = 1

@allowed([
  'GlobalStandard'
  'DataZoneStandard'
])
@description('Pay-as-you-go deployment type. Verify model/version/region support and quota before changing.')
param modelSku string = 'GlobalStandard'

@allowed([
  'Basic'
  'Standard'
  'Premium'
])
param acrSku string = 'Basic'

@allowed([
  'Standard_LRS'
  'Standard_ZRS'
])
@description('Standard StorageV2 redundancy. Existing-account conversions and regional support require review.')
param storageSku string = 'Standard_LRS'

@allowed([
  30
  60
  90
  180
  365
])
@description('Workspace default retention; individual tables can have their own retention policy.')
param logRetentionDays int = 30

@minValue(1)
@maxValue(10)
@description('Whole GB/day ingestion cap for this POC, not a total Azure spend cap. Reaching it can interrupt logs.')
param logDailyQuotaGb int = 1

@minValue(1)
@maxValue(365)
param blobSoftDeleteDays int = 7

var tags = {
  environment: environmentName
  purpose: 'hosted-agents-synthetic-poc'
  'azd-env-name': environmentName
}

resource group 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${environmentName}'
  location: location
  tags: tags
}

module foundation 'modules/foundation.bicep' = {
  name: '${environmentName}-foundation'
  scope: group
  params: {
    location: location
    environmentName: environmentName
    tags: tags
    enableAppConfiguration: enableAppConfiguration
    modelCapacity: modelCapacity
    modelName: modelName
    modelVersion: modelVersion
    modelDeploymentName: modelDeploymentName
    modelSku: modelSku
    acrSku: acrSku
    storageSku: storageSku
    logRetentionDays: logRetentionDays
    logDailyQuotaGb: logDailyQuotaGb
    blobSoftDeleteDays: blobSoftDeleteDays
  }
}

output resources object = foundation.outputs.resources
output AZURE_SUBSCRIPTION_ID string = subscription().subscriptionId
output AZURE_ENV_NAME string = environmentName
output AZURE_LOCATION string = location
output AZURE_RESOURCE_GROUP string = group.name
output AZURE_TENANT_ID string = tenant().tenantId
output AZURE_AI_ACCOUNT_ID string = foundation.outputs.resources.foundryAccountId
output AZURE_AI_ACCOUNT_NAME string = foundation.outputs.resources.foundryAccountName
output AZURE_AI_PROJECT_ID string = foundation.outputs.resources.foundryProjectId
output AZURE_AI_PROJECT_NAME string = environmentName
output AZURE_AI_PROJECT_ENDPOINT string = foundation.outputs.resources.foundryProjectEndpoint
output FOUNDRY_PROJECT_ENDPOINT string = foundation.outputs.resources.foundryProjectEndpoint
output AZURE_AI_MODEL_DEPLOYMENT_NAME string = foundation.outputs.resources.modelDeploymentName
output AZURE_CONTAINER_REGISTRY_NAME string = foundation.outputs.resources.registryName
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = foundation.outputs.resources.registryEndpoint
output AZURE_CONTAINER_REGISTRY_RESOURCE_ID string = foundation.outputs.resources.registryId
output AZURE_STORAGE_ACCOUNT_NAME string = foundation.outputs.resources.storageAccountName
output AZURE_STORAGE_ACCOUNT_ID string = foundation.outputs.resources.storageAccountId
output AZURE_STORAGE_BLOB_ENDPOINT string = foundation.outputs.resources.blobEndpoint
output AZURE_STORAGE_CONTAINER_NAME string = foundation.outputs.resources.artifactContainerName
output AZURE_STORAGE_CONTAINER_ID string = foundation.outputs.resources.artifactContainerId
output AZURE_LOG_ANALYTICS_WORKSPACE_ID string = foundation.outputs.resources.workspaceId
output AZURE_LOG_ANALYTICS_CUSTOMER_ID string = foundation.outputs.resources.workspaceCustomerId
output AZURE_APPLICATION_INSIGHTS_ID string = foundation.outputs.resources.applicationInsightsId
output AZURE_APP_CONFIGURATION_ID string = foundation.outputs.resources.appConfigurationId
output APP_CONFIG_ENDPOINT string = foundation.outputs.resources.appConfigurationEndpoint
