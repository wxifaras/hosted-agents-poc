param location string
param suffix string
param tags object
param workspaceId string
param storageSku string
param blobSoftDeleteDays int

resource account 'Microsoft.Storage/storageAccounts@2025-06-01' = {
  name: 'stpoc${suffix}'
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: storageSku
  }
  properties: {
    publicNetworkAccess: 'SecuredByPerimeter'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'None'
      ipRules: []
      virtualNetworkRules: []
    }
    encryption: {
      keySource: 'Microsoft.Storage'
      services: {
        blob: {
          enabled: true
        }
      }
    }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2025-06-01' = {
  parent: account
  name: 'default'
  properties: {
    deleteRetentionPolicy: {
      enabled: true
      days: blobSoftDeleteDays
    }
  }
}

resource artifacts 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-06-01' = {
  parent: blobService
  name: 'artifacts'
  properties: {
    publicAccess: 'None'
  }
}

resource perimeter 'Microsoft.Network/networkSecurityPerimeters@2025-09-01' = {
  name: 'nsp-poc-${suffix}'
  location: location
  tags: tags
  properties: {}
}

resource profile 'Microsoft.Network/networkSecurityPerimeters/profiles@2025-09-01' = {
  parent: perimeter
  name: 'artifact-access'
  properties: {}
}

resource ingress 'Microsoft.Network/networkSecurityPerimeters/profiles/accessRules@2025-09-01' = {
  parent: profile
  name: 'poc-all-public-ips'
  properties: {
    direction: 'Inbound'
    addressPrefixes: [
      '0.0.0.0/0'
    ]
  }
}

resource association 'Microsoft.Network/networkSecurityPerimeters/resourceAssociations@2025-09-01' = {
  parent: perimeter
  name: 'artifact-storage'
  properties: {
    accessMode: 'Enforced'
    privateLinkResource: {
      id: account.id
    }
    profile: {
      id: profile.id
    }
  }
  dependsOn: [
    ingress
  ]
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: perimeter
  name: 'perimeter-logs'
  properties: {
    workspaceId: workspaceId
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
  }
}

output accountId string = account.id
output blobEndpoint string = account.properties.primaryEndpoints.blob
output containerId string = artifacts.id
output perimeterId string = perimeter.id
output associationId string = association.id
