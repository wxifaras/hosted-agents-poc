# azd loads its selected environment before invoking this hook.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

foreach ($name in @('AZURE_ENV_NAME', 'AZURE_SUBSCRIPTION_ID', 'AZURE_LOCATION')) {
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) {
        throw "Missing $name. Set it with azd env set before provision (including azd provision --preview). For an early read-only preview, use az deployment sub what-if with infra\preview.parameters.json."
    }
}
if ($env:AZURE_ENV_NAME -notmatch '^[a-z][a-z0-9-]{0,39}$') {
    throw 'AZURE_ENV_NAME must be lowercase letters, numbers, and hyphens, starting with a letter (max 40).'
}
if ($env:AZURE_LOCATION -notin @('northcentralus', 'eastus', 'westus')) {
    throw 'Select northcentralus, eastus, or westus explicitly; no automatic fallback is permitted.'
}
Write-Host 'Provisioning target (review the native what-if before apply):'
$modelParameters = (Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot '..\..\infra\main.parameters.json') |
    ConvertFrom-Json).parameters
[pscustomobject]@{
    Subscription = $env:AZURE_SUBSCRIPTION_ID
    Environment = $env:AZURE_ENV_NAME
    ResourceGroup = "rg-$($env:AZURE_ENV_NAME)"
    Location = $env:AZURE_LOCATION
    Model = "$($modelParameters.modelName.value) / $($modelParameters.modelVersion.value) / $($modelParameters.modelSku.value)"
    ModelDeployment = $modelParameters.modelDeploymentName.value
    ModelCapacity = $modelParameters.modelCapacity.value
    RegistrySKU = $modelParameters.acrSku.value
    StorageSKU = $modelParameters.storageSku.value
    LogRetentionDays = $modelParameters.logRetentionDays.value
    LogDailyQuotaGB = $modelParameters.logDailyQuotaGb.value
    BlobSoftDeleteDays = $modelParameters.blobSoftDeleteDays.value
    StorageIngress = 'Enforced NSP; all public IPs; Entra/RBAC required'
} | Format-List | Out-Host
