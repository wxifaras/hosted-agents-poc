param location string
param suffix string
param environmentName string
param tags object
param modelCapacity int
param modelName string
param modelVersion string
param modelDeploymentName string
param modelSku string
param registryId string
param registryEndpoint string
param insightsId string
param insightsName string

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: 'aif-poc-${suffix}'
  location: location
  tags: tags
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    customSubDomainName: 'aif-poc-${suffix}'
    allowProjectManagement: true
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

resource model 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: account
  name: modelDeploymentName
  sku: {
    name: modelSku
    capacity: modelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: modelName
      version: modelVersion
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: account
  name: environmentName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: environmentName
    description: 'Synthetic-data Hosted Agents POC'
  }
  // Cognitive Services can reject concurrent child writes on the same account.
  dependsOn: [
    model
  ]
}

resource acrConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-04-01-preview' = {
  parent: project
  name: 'container-registry'
  properties: {
    category: 'ContainerRegistry'
    target: registryEndpoint
    authType: 'AAD'
    isSharedToAll: true
    metadata: {
      ApiType: 'Azure'
      ResourceId: registryId
    }
  }
}

resource insights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: insightsName
}

resource insightsConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-04-01-preview' = {
  parent: project
  name: 'application-insights'
  properties: {
    category: 'AppInsights'
    target: insightsId
    authType: 'ApiKey'
    isSharedToAll: true
    credentials: {
      key: insights.properties.ConnectionString
    }
    metadata: {
      ApiType: 'Azure'
      ResourceId: insightsId
    }
  }
  dependsOn: [
    acrConnection
  ]
}

output accountId string = account.id
output accountName string = account.name
output projectId string = project.id
output projectPrincipalId string = project.identity.principalId
output projectEndpoint string = 'https://${account.name}.services.ai.azure.com/api/projects/${project.name}'
