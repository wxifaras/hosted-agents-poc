# Deployment reference

Use the [Quick start](../README.md#quick-start---run-in-this-order) for the
recommended sequence. This guide provides CLI alternatives and deployment
caveats. Run commands from the repository root in PowerShell.

## Prerequisites

Install uv, Azure CLI with Bicep, azd 1.36+, and PowerShell 7. Sign in with the
same user/tenant in both CLIs:

```powershell
az login
azd auth login
uv sync --frozen --group validation --inexact
```

## Select the environment

```powershell
azd env new hosted-agents-poc-dev
azd env set AZURE_SUBSCRIPTION_ID (az account show --query id --output tsv)
azd env set AZURE_LOCATION northcentralus
```

For an existing environment, use `azd env select hosted-agents-poc-dev` instead
of `azd env new`. The templates allow `northcentralus`, `eastus`, and `westus`;
verify model/Hosted Agent availability and quota before choosing a region.

## Preview and provision

```powershell
azd provision --preview
```

Review Foundry/model, ACR, private Blob storage with enforced NSP, monitoring,
and project-identity permissions. New-identity role assignments may be
unevaluated; review [project-access.bicep](../infra/modules/project-access.bicep)
and the [infrastructure guide](../infra/README.md).

Only after reviewing the preview, explicitly provision:

```powershell
azd provision
```

This creates resources and incurs charges. Inspect failed deployment operations
before continuing; do not silently change regions or treat provisioning as
proof of NSP/data access.

## Grant local-user access

The helper discovers the latest successful subscription deployment matching
the requested environment and displays its timestamped name. Preview first:

```powershell
$subscription = azd env get-value AZURE_SUBSCRIPTION_ID
uv run --frozen --no-sync --group validation python -m scripts.deploy.grant_local_rbac --subscription $subscription --credential-source azd
```

Review the scoped roles, then explicitly apply:

```powershell
uv run --frozen --no-sync --group validation python -m scripts.deploy.grant_local_rbac --subscription $subscription --credential-source azd --apply
```

The helper checks that az/azd represent the same user. Apply also checks
synthetic Blob write/read/checksum/delete and a monitoring query. Investigate
failed checks before proceeding. RBAC is not a provisioning hook.

## Generate local configuration

```powershell
uv run --frozen --no-sync --group validation python -m scripts.deploy.generate_env --subscription $subscription
uv run --frozen --no-sync --group validation python -m scripts.common.validate_environment --mode configured
```

The generator creates `.env` from `.env.example` when absent and fills mapped
project/model and optional App Configuration values from successful Bicep
outputs. Use `--update` for an existing file; local ports, identity, telemetry,
comments, and other values are preserved. It neither provisions resources nor
invokes the model.

Both deployment-discovery helpers default to `hosted-agents-poc-dev`. Supply
`--environment <name>` for another environment or `--deployment <exact-name>`
to override discovery. Environment names are not timestamped deployment
operation names; invalid explicit names fail without fallback.

Follow the [local host instructions](#local-hosts-and-debugging)
or use F5. All local hosts read the root `.env`; process environment takes
precedence. Configured preflight validates settings, not live inference.

## Deploy and verify hosted agents

Use **Agents: Deploy and smoke test all hosted agents** in VS Code, or:

```powershell
azd deploy --all --environment hosted-agents-poc-dev
uv run --frozen --no-sync --group validation python -m scripts.deploy.grant_agent_rbac --environment hosted-agents-poc-dev --apply
uv run --frozen --no-sync --group validation python -m scripts.run_scenario.hosted_smoke --environment hosted-agents-poc-dev
```

- Remote ACR builds require no local Docker. The shared
  [Dockerfile](../src/backend/Dockerfile) uses Python 3.14.8 and the locked
  dependencies through the required package feed. Each service uses Responses
  2.0.0 and 0.5 CPU / 1 GiB per session.
- The [.dockerignore](../.dockerignore) excludes local credentials, `.env`,
  datasets, generated evidence, and virtual environments from the build context.
- Each deployment creates an immutable agent version, even with unchanged code.
- Agent-instance model grants are separate from local-user RBAC. The helper
  resolves actual instance principals and applies scoped Foundry User roles.
  Ordinary smoke setup adds no Blob, impersonation, or telemetry roles. The
  optional Supervisor impersonation grant is separate and does not establish
  verified caller propagation. RBAC propagation may delay requests.
- Hosted smoke uses the Azure CLI user and actual agent-bound Responses
  endpoints, not localhost. It checks active latest-version routing and sends
  one exact-marker request per agent, without automatic request retries.
  Generated evidence remains local and git-ignored.
- Independent hosted smoke verifies packaging, startup, model access, and
  Responses execution, not delegation, isolation, persistence, or recovery.
  End-to-end cloud trace export remains pending.
- Hosted-agent `azd deploy --preview` is currently unsupported/skipped; a zero
  exit code is not deployment proof. Inspect named-service failures explicitly.
- Foundry injects reserved `FOUNDRY_*` and `PORT` variables. Do not add them to
  service `env`; the platform rejects these overrides.

Full stateful delegation is not yet validated. The current application guard
rejects a missing nested user ID before State Store access, although the
documented State Store isolation contract resolves callers from the opaque
call ID. This is not a proven platform authorization failure. Read the
[ADR's authorization finding](adr/0001-hosted-agents-poc.md#authorization-contract-and-proposed-alignment)
before running stateful scenarios. Automatic workflow recovery is not implemented.

## Local hosts and debugging

Copy `.env.example` once if no `.env` exists, then set the real project endpoint
and model deployment. Do not overwrite an existing configuration:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# Set FOUNDRY_PROJECT_ENDPOINT and AZURE_AI_MODEL_DEPLOYMENT_NAME in .env.
uv run --frozen --no-sync --group validation python -m scripts.common.validate_environment --mode configured
uv run --frozen --no-sync python -m app.agents.supervisor.main
```

In separate terminals:

```powershell
uv run --frozen --no-sync python -m app.agents.research.main
uv run --frozen --no-sync python -m app.agents.data_analysis.main
```

Ports are 8088/8089/8090. Development binds loopback; hosted binds all interfaces.
Platform `PORT` overrides the role's default. Local F5 configurations force
loopback and distinct ports, overriding an inherited platform port.

```powershell
uv run --frozen --no-sync --group validation python -m scripts.run_scenario.trigger_agent --agent supervisor --no-color
uv run --frozen --no-sync --group validation python -m scripts.run_scenario.run_scenarios --scenario smoke --mode local --no-color
```

These requests use local HTTP but real configured model inference, which can
incur charges. They do not test hosted identity or persistence. Scripts support
credential-free `--help`; no script signs you in automatically.

### VS Code task and debugger reference

The [task table at the end of the README](../README.md#vs-code-tasks)
is the quick reference for the current task names and purposes.

Local checks never deploy, grant roles, or inject failures. Run setup once and
sync after dependency changes. Validation tasks use `uv run --no-sync`.
Hosted preview is currently skipped/unsupported, not a verification step.

For normal debugging, select **Supervisor host**, **Research host**, or
**Data Analysis host**, then press **F5**. The root `.venv` interpreter launches
the actual module, supports startup breakpoints, and owns process termination.
No extra listener or automatic dependency install is required.

**Supervisor + Foundry Agent Inspector** is the optional Toolkit path. Its task
starts debugpy on loopback 5679, waits for the listening message on 8088, opens
the in-editor Inspector, and attaches. Do not add `--wait-for-client`: attach
depends on HTTP readiness, so that would deadlock startup.

Stopping an attach debugger can leave the task-owned server running. Use
**Terminal > Terminate Task > Debug: Start Supervisor for Inspector** if needed.
Do not terminate unrelated tasks/processes or start another Supervisor on 8088.
Automated readiness tests do not establish interactive F5/Inspector UI behavior.

## Workflow and context diagnostics

Plain chat is not orchestration. Explicit `POC_WORKFLOW` JSON commands use
`create`, `inspect`, and `continue`; IDs in the request are labels, not ownership
credentials. The intended workflow creates a Supervisor conversation and distinct
specialist conversations, persists their references, and tests follow-up history.

**Agents: Test hosted workflow state and delegation** runs:

```powershell
uv run --frozen --no-sync --group validation python -m scripts.run_scenario.hosted_workflow --environment hosted-agents-poc-dev
```

This can incur model/hosting usage. It is not currently a passing end-to-end
demonstration. **Agents: Deploy and test hosted delegation** additionally builds
new versions; do not run it merely to inspect an existing failure.

Use **Agents: Diagnose hosted caller context** for the bounded comparison:

```powershell
uv run --frozen --no-sync --group validation python -m scripts.run_scenario.context_probe --environment hosted-agents-poc-dev --no-color
```

It compares direct Research with one Supervisor-to-Research probe. It reports
only runtime/call/user/session/conversation presence and an expected-caller
comparison. It requests no model inference or application State Store writes;
Foundry still creates conversation/history records and can incur hosting usage.

Probe collection can succeed while the final comparison exits with failure:
that comparison currently requires every presence flag and an exact user match.
It is not a State Store authorization test. Current observations and the
implementation gap are in the [ADR](adr/0001-hosted-agents-poc.md#implementation-and-evidence-snapshot).
Do not infer a platform limitation or change permissions from one missing flag.

An interrupted/failed workflow can remain `progress: running`. That means an
unfinished turn was persisted, not that successful work is still executing.
Inspection is allowed; automatic replay/recovery is disabled. Missing mappings
never authorize creating replacements.

## Session idle policy

All three services declare this in [azure.yaml](../azure.yaml):

```yaml
sessionConfiguration:
  idleTimeoutSeconds: 120
```

Redeploy affected services to change it. The value is a hosted **idle policy**,
not an HTTP/model timeout, maximum turn duration, or guaranteed exact eviction
time. It does not affect local F5 hosts or non-expiring registry items.
Actual filesystem idle/resume continuity remains a separate hosted experiment.

Validation reads raw deployed metadata and requires exactly
`definition.session_configuration.idle_timeout_seconds == 120`. The pinned
project SDK omits this newer field from typed `as_dict()` output, so the public
raw-response hook is used. Configured policy is not lifecycle evidence.

## Configuration and sizing

Settings are shared through [settings.py](../src/backend/app/core/settings.py),
[app_config_source.py](../src/backend/app/core/app_config_source.py), and
[logger.py](../src/backend/app/core/logger.py).

Precedence: constructor overrides, process environment, optional Azure App
Configuration, local dotenv, file secrets, defaults. Root dotenv resolution is
independent of the terminal directory; `POC_ENV_FILE` selects another file.
Bootstrap selectors use environment over dotenv without mutating the environment.
Configured remote-source failures are errors, not silent defaults. Restart
processes to refresh cached settings.

No App Configuration endpoint means no remote configuration access. The local
preflight never loads Azure settings. Install optional libraries only when needed:

```powershell
uv sync --frozen --group validation --extra appconfig --extra telemetry
```

Installing extras does not enable integrations or provision anything. Telemetry
content capture stays disabled. End-to-end cloud trace export still needs proof;
monitoring resources or optional packages alone do not establish it.

### Model selection

Both [main.parameters.json](../infra/main.parameters.json) and
[preview.parameters.json](../infra/preview.parameters.json) select:

```json
"modelName": { "value": "gpt-5-mini" },
"modelVersion": { "value": "2025-08-07" },
"modelDeploymentName": { "value": "gpt-5-mini" }
```

Keep both files aligned when using both preview paths. OpenAI format and
GlobalStandard are the baseline. Verify region, model/version, SKU, and quota
before changing them; review provisioning preview, then explicitly provision.
This is not an `.env`-only switch. Incremental deployment can leave an old model
deployment behind after renaming. Refresh mapped `.env` settings with `--update`.

| Parameter | Baseline | Supported POC values |
|---|---|---|
| `modelSku` | `GlobalStandard` | `GlobalStandard`, `DataZoneStandard` |
| `modelCapacity` | 10 in parameter files; bare Bicep fallback 1 | 1-10 units |
| `acrSku` | `Basic` | `Basic`, `Standard`, `Premium` |
| `storageSku` | `Standard_LRS` | `Standard_LRS`, `Standard_ZRS` |
| `logRetentionDays` | 30 | 30, 60, 90, 180, 365 |
| `logDailyQuotaGb` | 1 | 1-10 whole GB/day |
| `blobSoftDeleteDays` | 7 | 1-365 days |

Availability, price, quota, and in-place conversion restrictions apply.
The log cap can interrupt diagnostics and is not a total spending cap.
Soft-delete retention does not delete active artifacts. NSP enforcement,
disabled anonymous/shared-key Blob access, Foundry S0, and Log Analytics
PerGB2018 remain fixed. See the [infrastructure guide](../infra/README.md).

### Model throttling and readable results

All hosts share the selected model's limits. This deployment reported
1 request/minute and 1,000 tokens/minute at capacity 1, then 10 requests/minute
and 10,000 tokens/minute after the approved change to capacity 10. That is an
observed deployment result, not a universal capacity-unit conversion.
Burst windows and token estimates can still throttle requests. Respect retry
guidance; do not repeatedly submit failed prompts or increase capacity without review.

Smoke output shows the sanitized sent message and actual visible answer in
separate per-agent tables, between numbered START/END sections, with a final
outcome summary. Default prompts require the exact `HOSTED-AGENTS-POC-OK` marker.
Use the single-agent script's `--message` for a different prompt.

HTTP diagnostics use the actual operator-to-agent request URI, method, status,
and allowlisted headers, not the downstream model URL. Only the safe `api-version`
query is exposed separately. Auth, cookies, API keys, opaque call IDs, URL userinfo,
and hidden reasoning are excluded even with `--verbose`. An unavailable status
is not fabricated. No extra authorization is supplied by the diagnostic display.

`encrypted_content` is opaque model reasoning/continuation state, not encryption
of the visible answer by this application. It is omitted from console/evidence
without changing the SDK wire payload or decrypting hidden reasoning. Nonnegative
integer usage counters and exact boolean context-presence flags remain readable;
secret/token values remain redacted.

## Validation and evidence

After setup:

```powershell
uv run --frozen --no-sync pytest -q tests\unit
uv run --frozen --no-sync ruff check src\backend\app scripts tests
uv run --frozen --no-sync ruff format --check src\backend\app scripts tests
uv run --frozen --no-sync mypy
```

Unit tests can start actual SDK hosts with placeholder model settings, check
`/readiness` without inference, and stop only their own subprocesses. Mock state
tests do not prove Foundry isolation or restart behavior. Interactive VS Code UI
and cloud traces need separate checks.

Rich scripts save sanitized `manifest.json`, `events.jsonl`, and `results.jsonl`
under ignored `artifacts/<run-id>/`. Published findings should state the date,
mode, behavior, and limitations without requiring those private files.
Exit 0 means all required assertions passed, 1 means failure/required skip, and
2 means invalid input or blocked prerequisites. A negative test passes only when
the expected rejection is observed. Use `--verbose`, `--no-color`, and
`--output-dir` as needed.

Scripts are grouped by responsibility:

- `scripts.common`: environment validation, safe HTTP details, Rich reporting,
  and exact-environment deployment discovery.
- `scripts.deploy`: environment generation, explicit developer/agent RBAC,
  and the target-input hook. Provisioning itself remains native az/azd.
- `scripts.run_scenario`: local/hosted smoke, workflow validation, and the
  presence-only context probe.

Use module commands such as `python -m scripts.deploy.grant_local_rbac --help`.
The [Bicep output contract](../infra/README.md#outputs-for-the-local-environment-generator)
defines the generator's mapped fields. No script fallback changes the selected
environment, caller, or test mode to manufacture a pass.

## Tear down

Wait for active operations to finish and export anything you need. Confirm the
selected target:

```powershell
azd env select hosted-agents-poc-dev
azd env get-value AZURE_SUBSCRIPTION_ID
azd env get-value AZURE_RESOURCE_GROUP
```

**Destructive: this skips deletion/purge confirmations.** Use only when you intend
to remove this entire disposable POC:

```powershell
azd down --environment hosted-agents-poc-dev --force --purge
```

Expect loss of the project/model, Blob artifacts, registry images, and monitoring
data, including supported soft-deleted recovery copies. `--force` skips deletion
confirmation; `--purge` skips confirmation for permanent purge where supported.
Retention/purge protection and service timing can still prevent immediate name
reuse. Inspect a failed deletion; never purge unrelated resources.

Local source and `.env` are not deleted; endpoints in that file become stale.
The custom impersonation role was created explicitly outside Bicep. Project
deletion removes its scoped assignment, but the custom definition may require
separate administrator cleanup. Teardown never runs from setup, F5, or hooks.
