# Infrastructure

Use native **az/azd for deployment**; no Python deployment wrapper. The separate
[grant_local_rbac.py](../scripts/deploy/grant_local_rbac.py) uses Python/Rich for operator
presentation and native `az` commands for user-role inspection and grants.
It is deliberately not a provisioning hook.

## Foundation

[main.bicep](main.bicep) creates one resource group and composes the modules:

- Foundry account/project and shared `gpt-5-mini` deployment
  (`2025-08-07`, GlobalStandard, parameter files now select 10 capacity units).
- ACR Basic with admin credentials disabled, and a Foundry registry connection.
- Standard LRS Storage, one private `artifacts` container, and enforced Network
  Security Perimeter association. NSP explicitly allows IPv4 `0.0.0.0/0`.
  IPv6 is excluded pending verified service/subscription support.
  Storage uses `SecuredByPerimeter`, default-deny firewall, no trusted-service
  bypass, no shared-key auth, and no anonymous Blob access.
- Log Analytics and Application Insights with a Foundry connection. Local
  Application Insights authentication is disabled; eventual agent deployment
  must use Entra-authenticated telemetry and actual agent identity permissions.
- Project-identity model access and ACR pull permissions.
- Optional App Configuration, disabled by default.

No agent image/version is deployed by these templates. Agent-specific Blob,
telemetry, and state roles must target actual agent identities once created.
No capability host, Cosmos, Search, SQL, AKS, or Container Apps is created.

All-public-IP NSP ingress is intentionally broad internet reachability for this
synthetic POC, not private networking or anonymous data access. Prefix acceptance
and real data-plane behavior still require deployed verification.

## Native read-only validation and verbose what-if

Prerequisites: Azure CLI, Bicep, azd 1.36+, PowerShell 7, and manual Azure login.
Keep the selected CLI identities/subscription intentional; scripts never log you in.

The concrete [preview.parameters.json](preview.parameters.json) allows a
read-only preview without initializing local azd environment state:

```powershell
az bicep build --file infra\main.bicep --outfile artifacts\infra-validation\main.json
az deployment sub validate --subscription lz-app1 --name hosted-agents-poc-dev --location northcentralus --template-file infra\main.bicep --parameters '@infra\preview.parameters.json' --query '{state:properties.provisioningState,error:error,diagnostics:properties.diagnostics}'
az deployment sub what-if --subscription lz-app1 --name hosted-agents-poc-dev --location northcentralus --template-file infra\main.bicep --parameters '@infra\preview.parameters.json' --verbose
```

What-if shows exact proposed resource changes. `--verbose` supplies additional
native CLI progress; it is not a substitute for the change list. Do not enable
`--debug` by default or publish unreviewed CLI output that could contain secrets.

The initial native preview reported **17 creates and 2 unsupported role-assignment
changes**. Those two assignments depend on the new project's generated principal
ID and cannot be fully identified before creation. Azure validation reports
`NestedDeploymentShortCircuited` for that access module. Review
[project-access.bicep](modules/project-access.bicep) explicitly; do not describe
the preview as full coverage. There were no reported deletes/modifications.
That preview created no resources. The user's subsequent provisioning attempt
partially created the foundation and failed as described below; full connectivity
remains unproven.

### Recovery from the initial partial deployment

The 2026-10-20 deployment failed on two independent operations:

- Foundry project creation returned `RequestConflict` while the same account's
  model deployment was running. Both depended only on account creation.
  The template now orders account, model, project, registry connection, and
  monitoring connection writes sequentially.
- The NSP access rule containing `::/0` returned
  `This feature is not yet available for given subscription`. The perimeter,
  profile, and Storage account themselves were created successfully. A public
  report in [Azure/terraform-provider-azapi#743](https://github.com/Azure/terraform-provider-azapi/issues/743)
  reproduces this error with IPv6 prefixes; it is supporting evidence, not proof
  of this subscription's exact service limitation. The owner approved IPv4-only
  ingress for now. `::/0` is removed; Enforced mode, `SecuredByPerimeter`,
  default-deny fallback, private containers, and disabled shared-key auth remain.

The old `AllowNetworkSecurityPerimeter` preview flag was NotRegistered, but
[NSP is generally available](https://learn.microsoft.com/azure/private-link/network-security-perimeter-concepts).
Do not assume registering the old preview flag fixes IPv6, or automatically
register features or weaken Storage networking.

Wait for existing operations to reach terminal states, then run
`azd provision --preview` against the **same environment**. Review changes and
run `azd provision` yourself to retry. Keep the existing resource group; do not
delete resources or switch region to work around a partial deployment.
If the IPv4-only access rule still fails, collect its deployment operation and
request Azure support for the service/subscription restriction.
No successful redeployment or deployed IPv4-only rule is claimed yet.

The corrected template passed 28 focused infrastructure tests, Ruff and mypy.
A read-only recovery what-if succeeded with 5 Create, 6 Modify, 6 NoChange, and
2 Unsupported entries; no resource deletions were proposed. The modify deltas
include service-populated/default properties, including the model's default
RAI policy, so review the detailed native preview rather than approving by
counts alone. The two unresolved entries are the project-identity role grants.
Neither this preview nor compilation proves the IPv4-only rule will deploy.

Use **Terminal > Run Task > Infrastructure: Preview changes (Azure CLI)** for this
same native workflow. Build and validation tasks are available separately.

## Initialize azd and provision only after approval

[azure.yaml](../azure.yaml) selects the **Bicep provider**, so no second
Foundry provisioning provider generates overlapping infrastructure. Its selected
environment interpolates [main.parameters.json](main.parameters.json).

```powershell
# Existing project: initialize only local azd environment state, not new source.
azd env new hosted-agents-poc-dev
azd env set AZURE_SUBSCRIPTION_ID (az account show --subscription lz-app1 --query id --output tsv)
azd env set AZURE_LOCATION northcentralus
```

The [preprovision hook](../scripts/deploy/check_provision_inputs.ps1) validates the
environment name, subscription, and region before native azd preview or apply;
it never deploys or grants roles. Budget and cleanup-date inputs are not required
for this POC. Resources still incur charges until stopped or deleted.

```powershell
azd provision --preview
# Review all changes, including the unresolved project-identity role assignments.
# This next command creates resources and can incur charges:
azd provision
```

azd provides its native progress UI; this version has **no `--verbose` flag**.
Use native `az ... --verbose` for detailed what-if/operation inspection rather
than inventing a flag or adding a deployment orchestrator.

```powershell
az deployment sub list --subscription lz-app1 --query '[].{name:name,state:properties.provisioningState}' --output table
# Use the actual deployment name returned above:
az deployment operation sub list --subscription lz-app1 --name '<deployment-name>' --verbose
az deployment group list --subscription lz-app1 --resource-group rg-hosted-agents-poc-dev --output table
az deployment operation group list --subscription lz-app1 --resource-group rg-hosted-agents-poc-dev --name project-access --verbose
```

Native `az deployment sub create --verbose --confirm-with-what-if` is also
available for an explicitly approved direct Bicep deployment. Run the target-input
check first, supply the same approved parameter values, and do not alternate
provisioning drivers without aligning azd outputs/state afterward.

GlobalStandard does not guarantee processing in the resource's region. Prefer
North Central US; East US/West US are candidates only after explicit review of
availability/capacity and parameter changes, never automatic fallbacks.

The agent deploy phase remains separate from Bicep provisioning. Root
`azure.yaml` now declares three container-based agent services sharing the root
build context and locked Python 3.14 dependency set. Use `azd deploy --all` for
agent code after provisioning, then explicit instance-model RBAC and hosted
smoke tasks as described in the [deployment guide](../docs/deployment.md#deploy-and-verify-hosted-agents).
Do not re-provision infrastructure merely to redeploy code. Native hosted-agent
deployment preview is currently unsupported and must not count as validation.

## Separate local-user RBAC (Python + Rich)

After the foundation succeeds, the RBAC and environment-generation helpers
automatically find the newest successful subscription deployment whose
`environmentName` parameter exactly matches `--environment` (default:
`hosted-agents-poc-dev`). The RBAC helper expects resource group `rg-<environment>`.
They display the selected operation name, which normally includes an azd timestamp.
Use `--deployment <exact-name>` to override discovery; an invalid explicit name
fails rather than silently falling back. No-match and ambiguous discovery fail explicitly.

```powershell
uv run --frozen --no-sync --group validation python -m scripts.deploy.grant_local_rbac --subscription lz-app1 --credential-source az
# Explicit mutation after reviewing the role table:
uv run --frozen --no-sync --group validation python -m scripts.deploy.grant_local_rbac --subscription lz-app1 --credential-source az --apply
```

The helper resolves the actual current user, validates POC scopes, displays
existing/inherited/group grants and missing built-in roles, and checks assignment
rights before writing. Native `az` always executes grants. With
`--credential-source azd`, it verifies azd and az are the **same user** through
Graph `/me` before continuing; it never guesses a user from token text.
Use `--expected-principal-id` to pin the intended principal.

Default roles: container-level Storage Blob Data Contributor, account-scoped
Foundry User, workspace Log Analytics Reader, and Application Insights Monitoring
Reader/Metrics Publisher. App Configuration Data Reader is conditional.
`--include-acr-push` opts into image-publishing permissions for this non-ABAC
registry; no subscription Owner/Contributor/User Access Administrator is granted.
Foundry User is the current built-in data role, not a claim of minimal per-operation
custom permissions.

`--apply` verifies a named synthetic Blob upload/download/checksum/delete and
a Log Analytics query using `--auth-mode login`. `--verify-access` runs these
probes without changing roles. Propagation retries are bounded and errors remain
visible. No model inference, image push, or hosted user isolation is simulated.
Monitoring verification uses built-in `az rest` with an explicit Log Analytics
token audience and checks the exact `POCValidation=1` result. No `log-analytics`
CLI extension is required. Dynamic extension installation is disabled for helper
subprocesses, so a missing extension cannot trigger an unattended install/prompt.
Monitoring failures are recorded under their own step instead of only the
generic identity/access failure label.

`--help` requires no Azure login. Rich results and sanitized JSONL evidence go to
ignored `artifacts/<run-id>/`. Exit 0 means the requested checks passed, exit 1
means failure, and exit 2 means a blocked input/prerequisite.

## Validation boundary

Compiled templates, mock role-operation tests, deployment-target tests, and native
Azure what-if validate configuration, not every deployed behavior. Foundation
provisioning, scoped grants, and developer Blob/monitoring probes have since been
exercised separately. Isolated NSP-denial tests and application-owned hosted
artifact access remain pending. See the self-contained
[POC decision and evidence record](../docs/adr/0001-hosted-agents-poc.md#implementation-and-evidence-snapshot).

## Outputs for the local environment generator

The subscription deployment exports both the existing `resources` object
(used by the RBAC helper) and flat, string-valued outputs that azd can store in
its selected environment. Output additions are backward-compatible; existing
names are retained.

| Local environment setting | Source |
|---|---|
| `FOUNDRY_PROJECT_ENDPOINT` | Same-named Bicep output. |
| `AZURE_AI_MODEL_DEPLOYMENT_NAME` | Same-named Bicep output. |
| `APP_CONFIG_ENDPOINT` | Same-named Bicep output; empty when App Configuration is disabled. |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Authenticated resource lookup using output `AZURE_APPLICATION_INSIGHTS_ID`; intentionally not a plain deployment output. |
| `ENVIRONMENT`, agent ports, timeout, log level | Keep local defaults from `.env.example`; do not replace `development` with the azd environment name. |
| `APP_CONFIG_KEY_FILTER`, `APP_CONFIG_LABEL_FILTER` | Keep local defaults/explicit choices; these selectors are not provisioned settings. |
| `AZURE_CLIENT_ID` | Keep blank for local CLI user credentials. Only set for an explicitly chosen user-assigned managed identity; never substitute a project principal/object ID. |

Additional metadata is ready without parsing resource IDs: subscription,
tenant, location, environment/resource-group name, Foundry account/project
IDs and names, registry ID/name/endpoint, Storage account ID/name/Blob
endpoint/container ID/name, Log Analytics resource ID/customer ID, and optional
App Configuration resource ID.

All current output values are identifiers/endpoints, not access tokens, account
keys, or secrets. Resource IDs are lookup handles, not authorization. The future
script must whitelist mappings, preserve local values, and not copy all azd
variables blindly into `.env`. Any telemetry lookup must respect the existing
Entra-only ingestion configuration; a connection string alone does not grant
telemetry-publishing permission.

Agent endpoints/names/versions are not Bicep foundation outputs. After agent
deployment, azd records `AGENT_<SERVICE>_*` values in its local environment.
The project SDK can also retrieve active version metadata and agent-bound
Responses endpoints. Do not copy agent-managed state into infrastructure outputs.
The [Rich generator](../scripts/deploy/generate_env.py) reads successful subscription
deployment outputs through native Azure CLI. Use the Create/Update environment
tasks or `python -m scripts.deploy.generate_env --help`. It fills only the direct
project/model/App Configuration settings; telemetry lookups are not implemented.
It refuses an existing `.env` unless `--update` is explicit and preserves all
unmapped local values. The current foundation/model provisioning succeeded in
the user's retry; no hosted agent deployment or inference success is implied.

### Model parameters

Root Bicep accepts `modelName`, `modelVersion`, `modelDeploymentName`, `modelSku`,
and `modelCapacity`. Parameter files select GPT-5 mini / 2025-08-07 / gpt-5-mini /
GlobalStandard / 10; bare root Bicep retains a minimum fallback capacity of 1.
OpenAI format remains fixed. Edit the concrete model values in
[main.parameters.json](main.parameters.json) for azd and
[preview.parameters.json](preview.parameters.json) for direct az what-if.
The chosen deployment name propagates to outputs and the generated `.env`.
Review availability/quota and what-if before provisioning any changed model.

### SKU and retention parameters

The root parameters also expose `acrSku` (Basic/Standard/Premium),
`storageSku` (Standard_LRS/Standard_ZRS), `logRetentionDays`
(30/60/90/180/365), `logDailyQuotaGb` (1–10 whole GB/day), and
`blobSoftDeleteDays` (1–365). Defaults are Basic, Standard_LRS, 30, 1, and 7.
`modelSku` allows GlobalStandard or DataZoneStandard, not provisioned throughput.
These are intentionally restricted POC options rather than every Azure SKU.

Both parameter files contain the same sizing defaults. The preprovision summary
reads them from the azd parameter file rather than displaying a hard-coded SKU.
Review any changes with native what-if: model availability/capacity and ZRS
regional support are not guaranteed, existing account conversions have service
restrictions, and ACR tier changes affect pricing/features.
Workspace retention may be overridden per table. The daily ingestion cap is
not a total Azure budget and can interrupt observability. Blob soft delete
retains deleted data; it does not schedule deletion of active artifacts.
No security switch, Foundry account SKU, or Log Analytics pricing-plan parameter
was added.

The capacity-1 deployment reported only 1 request/minute and 1,000 tokens/minute.
After user authorization, a capacity-only native `az resource update` increased
the existing model to 10 without reapplying unrelated foundation changes from the
full azd preview. Both parameter files were updated to keep Bicep desired state
aligned. Azure returned Succeeded with 10 requests/minute and 10,000 tokens/minute;
model version, SKU, RAI policy, and upgrade policy were retained. No inference
was invoked during the update/management-plane verification.
