targetScope = 'resourceGroup'

param location string = resourceGroup().location
param registryName string
param imageTag string
param audience string
param callerId string
param appName string = 'story6-approval-mcp'

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

resource pullIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${appName}-pull'
  location: location
}

resource imagePull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, pullIdentity.id, 'AcrPull')
  scope: registry
  properties: {
    principalId: pullIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}

resource environment 'Microsoft.App/managedEnvironments@2025-01-01' = {
  name: '${appName}-env'
  location: location
  properties: {}
}

resource app 'Microsoft.App/containerApps@2025-01-01' = {
  name: appName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${pullIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: environment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        allowInsecure: false
        targetPort: 8092
        transport: 'http'
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: pullIdentity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'mcp'
          image: '${registry.properties.loginServer}/story6-approval-mcp:${imageTag}'
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            { name: 'STORY6_MCP_URL', value: 'https://${appName}.${environment.properties.defaultDomain}/mcp' }
            { name: 'STORY6_MCP_TENANT_ID', value: tenant().tenantId }
            { name: 'STORY6_MCP_AUDIENCE', value: audience }
            { name: 'STORY6_MCP_CALLER_ID', value: callerId }
          ]
          probes: [
            {
              type: 'Readiness'
              httpGet: { path: '/health', port: 8092 }
              initialDelaySeconds: 5
              periodSeconds: 10
            }
          ]
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
  dependsOn: [imagePull]
}

output mcpUrl string = 'https://${app.properties.configuration.ingress.fqdn}/mcp'
output mcpScope string = 'api://${audience}/.default'
