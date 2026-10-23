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
  It also ensures Data Analysis has Monitoring Metrics Publisher and the project
  managed identity has Monitoring Reader on the connected Application Insights.
  Ordinary smoke setup adds no Blob or impersonation roles. The
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

The [detailed task guide](tasks-detailed.md) covers every task, grouped by purpose,
with test use cases, execution flows, prerequisites, and evidence boundaries.
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

### Identity variants and isolation experiment

#### Prepare isolated native-user authentication

Run **Access: Create isolated user auth profiles** to enter comma-separated full
sign-in names, such as `user1@tenant.onmicrosoft.com,user2@tenant.onmicrosoft.com`.
The selected azd environment supplies `AZURE_TENANT_ID`. The task also asks for
RBAC: `consumer` (default) or `none` (authentication only). Microsoft sign-in opens
for each account; enter passwords, password resets, and MFA only in Microsoft's
window, never in task inputs, command arguments, or this repository.

CLI preview, followed by explicit sign-in:

```powershell
uv run --frozen --no-sync --group validation python -m scripts.common.auth_profiles --users "user1@tenant.onmicrosoft.com,user2@tenant.onmicrosoft.com" --environment hosted-agents-poc-dev
# Add --rbac consumer to preview the project-scoped invocation grants too.
# After reviewing the plan, repeat the command with --apply.
```

Use `--tenant <tenant-guid>` to override environment discovery. Without `--apply`,
the helper only records a plan; it does not create profile caches or sign in.
It never copies the developer cache or changes the parent process environment.
The CLI defaults to `--rbac none`; the task's explicit selection controls RBAC.

With `--rbac consumer`, run from your existing operator/admin CLI context, not
from a test-user profile. Before any user sign-in, the helper validates the
environment's project/subscription/tenant, resolves every requested account to a
distinct enabled Member user, and validates the built-in invoke-only Consumer
role. It plans all users before writing anything. Only `--apply` creates missing
project-scoped assignments, using stable assignment names and existing role
helpers. Unconditional inherited/group Consumer grants are reused; conditional
Consumer grants require review instead of being widened. Assignment write
permission is required only when grants are missing.

No Foundry User, Owner, impersonation, Blob, model, or subscription-wide grants
are created. Other existing roles are not removed or reduced; this is not a full
least-privilege audit. Each assignment is read back before user sign-in begins.
Readback is not effective data-plane access proof, and RBAC propagation can take
time. If later grants or authentication fail, earlier grants/profiles remain;
there is no automatic rollback, login fallback, or retry.
Each tenant/user gets a distinct hashed directory below
`$env:LOCALAPPDATA\HostedAgentsPoc\Auth`. The helper refuses paths overlapping the
repository or active CLI cache, and rejects existing cache entries or completed
logins for another user/tenant. Earlier successful profiles are retained on failure.

The helper prints a PowerShell `AZURE_CONFIG_DIR` assignment for each verified
profile. Set it in a separate terminal before using that identity. The task
cannot change the environment of an already open terminal. Browser/Windows SSO
can offer an existing account, so explicitly select the requested UPN; directory
names alone are not identity proof. To recover a wrongly populated profile, sign
out manually only under that profile's printed `AZURE_CONFIG_DIR`, then retry.
The helper never automatically signs out, clears caches, or disables MFA/CA.

Profile caches contain authentication material: keep them local and do not share,
copy, or commit them. Only CLI account metadata is captured by the helper;
passwords/tokens are not collected or added to run evidence.

**Authentication is not authorization or isolation proof.** Native-user
invocation normally needs the least-privilege Foundry Agent Consumer role at the
approved project/agent scope, not an impersonation grant. This task optionally
grants only project-scoped Consumer access and invokes no agents. Do not use the
synthetic-label matrix below as a
substitute for a genuine two-Entra-user test. Future native-user tests must use
the selected CLI credential explicitly, without a developer-credential fallback.

#### Test genuine native Entra users

After profile setup and an approved Consumer grant, run **Agents: Test native
Entra user isolation**, entering exactly two full UPNs. The CLI equivalent is:

```powershell
uv run --frozen --no-sync --group validation python -m scripts.run_scenario.native_user_isolation --users "user1@tenant.onmicrosoft.com,user2@tenant.onmicrosoft.com" --environment hosted-agents-poc-dev --run --no-color --verbose
```

Without `--run`, only prerequisites are checked and hosted tests remain BLOCKED.
The existing operator identity reads Research's active version/routing/120-second
idle metadata; it never authenticates the actual test requests. Each native
credential uses a separate child-process `AZURE_CONFIG_DIR`, verifies CLI and
Graph `/me` identities, and obtains only the Foundry token from that profile.
Tokens stay in process memory. There is no login, developer-credential fallback,
acting-user header, role change, or deployment.

The test uses Research's existing opt-in diagnostic handler and separate
one-hour State Store markers. In each direction it verifies both callers'
positive context, owner state/history continuity, other-user state denial,
history denial with and without an explicit owner session, owner continuation
after those attempts, and unchanged owner state. Generic history `404/not_found`
also requires positive owner response readback. Unexpected access remains FAIL
even if no marker is returned; unrelated failures are not isolation passes.
Actual outgoing requests are checked for absence of delegated-user, forwarded
call-context, and client-forged platform-user headers.

This is direct native-user isolation, not hosted Supervisor-to-specialist
delegation, filesystem/artifact authorization, model-quality, or recovery proof.
No normal workflow mappings are created or reset. No automatic retries are made.

The VS Code task asks **Verbose** or **Not verbose**, defaulting to Verbose.
These select `--verbose` and `--no-verbose`, respectively.
In verbose mode, Rich REQUEST/RESPONSE panels show the
captured diagnostic message, acting user alias, response/session bindings, safe
HTTP URI/status/correlation headers, elapsed invocation time, and validated
diagnostic reply. Rejected calls show only safe error classification; a 403/404
is not called a passing isolation check until the assertion evaluates it.
These replies are deterministic diagnostic JSON, not model-generated answers.
Elapsed time includes authentication/transport, not just server processing.

For shorter CLI output, omit `--verbose`. Sanitized exchange events are still
saved in `events.jsonl` with `kind: request` or `kind: response`, alongside the
assertion events. `results.jsonl` remains assertion-only, so verbosity does not
change pass/fail counts or exit codes. Tokens, cookies, opaque call IDs, raw
service-error messages, and unapproved headers are excluded. Owner-resource GET
readback remains represented by its confirmation in the history-denial assertion.

#### Run the delegated-label diagnostics

After explicit approval, use **Agents: Test hosted identity variants and isolation**:

```powershell
uv run --frozen --no-sync --group validation python -m scripts.run_scenario.identity_matrix --environment hosted-agents-poc-dev --no-color
```

This requires `POC_ENABLE_IDENTITY_TESTS=true` on Supervisor and Research. The
setting defaults to false; the POC manifest explicitly enables it for the
owner-authorized experiment. Disable it and redeploy those services when this
diagnostic surface is no longer needed. It never relaxes `POC_WORKFLOW` checks.

The fixed diagnostic variants do not mix mechanisms: original call context only,
or explicit synthetic application-user delegation only. The caller-comparison
header is diagnostic, never authorization. User labels are generated server-side
per verified Supervisor caller/run; arbitrary acting-user or store overrides
are rejected. No additional permission is granted by this task.

The diagnostic expected-user value follows the selected mechanism: original
caller for call-context forwarding, selected synthetic A/B label for delegated
forwarding. Context results label this source explicitly. Equality is an
observation, not an authorization decision or proof of an enforced boundary.

The Research diagnostic requires hosted call/session context but intentionally
does not require the visible user-ID field, so service authorization can actually
be observed. Small markers use a separate user-isolated
`hosted-agents-poc-identity-experiments-v1` State Store, with a verified one-hour
item lifetime. Reads do not initialize absent stores/items. Markers expire;
normal workflow mappings are never opened, reset, or deleted.

State and platform-history checks are reported separately from context presence.
The A-A-B-A test requires A's initial continuation to succeed, B's access to
produce a specific visibility rejection, and A to continue again afterward;
unrelated failures are not passes. The history
handler returns deterministic synthetic text, not model output. Conversation/
response records and hosting still incur service usage.

The Rich report and saved evidence include a separate
`delegated-user: A continues after B attempt` assertion. Its observations show
whether A saw the history marker, reused the original child session, and created
a new response. This uses A's original client and the response preceding B's
attempt, not any response returned to B. An A-continuation PASS never overrides
a B-isolation FAIL. For nested runs, deploy the Supervisor containing this
assertion before testing; an older version's successful owner GET is not evidence
of post-B continuation.

The operator also reports `Matrix completeness`. Every required core assertion
must appear once; empty/partial matrices, duplicate names, or a genuine-user PASS
that contradicts the protocol's untested flag fail validation. Original failed
and blocked checks remain in the report. A completeness PASS means the expected
assertions were collected, not that they passed. Old Supervisor versions missing
final-A continuation cannot satisfy this evidence requirement.

Two delegated labels are not two genuine Entra users. The latter tests are
explicitly BLOCKED until another authenticated principal is available, making
the overall scenario nonzero even if all delegated-user checks pass. This is
diagnostic evidence, not completion of the full specialist workflow.

For the developer-to-Research control, without a Supervisor hop:

```powershell
uv run --frozen --no-sync --group validation python -m scripts.run_scenario.identity_matrix --environment hosted-agents-poc-dev --direct-control --no-color
```

This uses the current Azure CLI principal and separate synthetic delegated
labels. It starts directly with Responses/`previous_response_id` and runs the
same strengthened A-A-B-A sequence; no fake platform call/session context is created.
The current operator must independently have the custom impersonation action:
Supervisor's assignment does not grant it to the developer. A `403` before
Research executes blocks the control and is not an isolation pass. The script
does not grant roles, refresh authentication, or retry denied requests.

For the nested response-chain control (no explicit conversation creation):

```powershell
uv run --frozen --no-sync --group validation python -m scripts.run_scenario.identity_matrix --environment hosted-agents-poc-dev --response-chains --no-color
```

This requires the Supervisor version that supports `compare-chains`. It separates
conversation-object binding from hosted-caller identity handling. A missing
conversation ID is not a failed requirement when a response chain was explicitly
chosen; verified call/session/user checks and actual history assertions still apply.

For the separately authorized history-only negative test:

```powershell
uv run --frozen --no-sync --group validation python -m scripts.run_scenario.identity_matrix --environment hosted-agents-poc-dev --history-only --no-color --verbose
```

This selects `compare-history-only` on Supervisor and requires a version that
implements it. B sends A's `previous_response_id` without explicitly sending
`agent_session_id`. A's initial and final continuations retain the original
session binding. This changes only B's explicit sandbox binding relative to the
nested response-chain control; it does not turn the labels into genuine users.
`--history-only` and `--response-chains` are mutually exclusive.

The B-history result records `explicit_session_requested: false`; completeness
fails if that observation is absent or contradicts the selected history-only mode.
Boolean-only egress checks also compare the serialized previous-response/session
fields with the requested bindings on successful and HTTP-rejected requests.
They inspect application HTTP request objects, not downstream gateway changes,
and never return the actual IDs or request bodies.

Generic `404/not_found` is only accepted as a masked history denial when the
request targets the Responses endpoint and A successfully retrieves the exact
completed response after B's rejected continuation. A generic 404 by itself,
an unrelated permission failure, or a transport outage is not an isolation pass.
The [ADR](adr/0001-hosted-agents-poc.md#planned-delegation-tests) records the
passing direct delegated-label control and the still-failing nested controls.
Temporary developer grants used for those approved controls were removed; the
command does not recreate them, and a later run may require fresh approval.

Supervisor's current diagnostic also compares final application HTTP request
targets/identity headers and bearer principal/client claims against its actual
instance metadata. Results contain booleans only; unknown/unreadable credential
metadata is not reported as a match. This is inspection of our own request,
not token signature verification or a substitute for isolation tests.
It cannot prove that an intermediary leaves the request unchanged after the
application transport boundary. No tokens, raw claims, or opaque call IDs are
printed or persisted by the audit.

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
