targetScope = 'resourceGroup'

param location string
param environmentName string
param tags object
param enableAppConfiguration bool
param modelCapacity int
param modelName string
param modelVersion string
param modelDeploymentName string
param modelSku string
param acrSku string
param storageSku string
param logRetentionDays int
param logDailyQuotaGb int
param blobSoftDeleteDays int

var suffix = uniqueString(subscription().id, resourceGroup().id)

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: 'acrpoc${suffix}'
  location: location
  tags: tags
  sku: {
    name: acrSku
  }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

module monitoring 'monitoring.bicep' = {
  name: 'monitoring'
  params: {
    location: location
    suffix: suffix
    tags: tags
    logRetentionDays: logRetentionDays
    logDailyQuotaGb: logDailyQuotaGb
  }
}

module storage 'storage.bicep' = {
  name: 'artifact-storage'
  params: {
    location: location
    suffix: suffix
    tags: tags
    workspaceId: resourceId('Microsoft.OperationalInsights/workspaces', 'log-poc-${suffix}')
    storageSku: storageSku
    blobSoftDeleteDays: blobSoftDeleteDays
  }
  dependsOn: [
    monitoring
  ]
}

module foundry 'foundry.bicep' = {
  name: 'foundry'
  params: {
    location: location
    suffix: suffix
    environmentName: environmentName
    tags: tags
    modelCapacity: modelCapacity
    modelName: modelName
    modelVersion: modelVersion
    modelDeploymentName: modelDeploymentName
    modelSku: modelSku
    registryId: registry.id
    registryEndpoint: '${registry.name}.azurecr.io'
    insightsName: 'appi-poc-${suffix}'
    insightsId: resourceId('Microsoft.Insights/components', 'appi-poc-${suffix}')
  }
  dependsOn: [
    monitoring
  ]
}

module projectAccess 'project-access.bicep' = {
  name: 'project-access'
  params: {
    accountName: 'aif-poc-${suffix}'
    registryName: registry.name
    insightsName: monitoring.outputs.insightsName
    principalId: foundry.outputs.projectPrincipalId
  }
}

resource appConfig 'Microsoft.AppConfiguration/configurationStores@2024-05-01' = if (enableAppConfiguration) {
  name: 'appcs-poc-${suffix}'
  location: location
  tags: tags
  sku: {
    name: 'free'
  }
  properties: {
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

output resources object = {
  subscriptionId: subscription().subscriptionId
  tenantId: tenant().tenantId
  resourceGroup: resourceGroup().name
  location: location
  foundryAccountId: foundry.outputs.accountId
  foundryAccountName: 'aif-poc-${suffix}'
  foundryProjectId: foundry.outputs.projectId
  foundryProjectEndpoint: foundry.outputs.projectEndpoint
  modelDeploymentName: modelDeploymentName
  registryId: registry.id
  registryName: registry.name
  registryEndpoint: registry.properties.loginServer
  storageAccountId: storage.outputs.accountId
  storageAccountName: 'stpoc${suffix}'
  blobEndpoint: storage.outputs.blobEndpoint
  artifactContainerId: storage.outputs.containerId
  artifactContainerName: 'artifacts'
  perimeterId: storage.outputs.perimeterId
  perimeterAssociationId: storage.outputs.associationId
  workspaceId: monitoring.outputs.workspaceId
  workspaceCustomerId: monitoring.outputs.workspaceCustomerId
  applicationInsightsId: monitoring.outputs.insightsId
  appConfigurationId: enableAppConfiguration ? appConfig!.id : ''
  appConfigurationEndpoint: enableAppConfiguration ? appConfig!.properties.endpoint : ''
}
