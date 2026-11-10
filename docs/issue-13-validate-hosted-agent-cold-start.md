# Story 13 - Hosted-agent cold starts and memory snapshots

**Detailed HTML report with task walkthroughs, graphs, and recorded evidence:**
[View with HTML Preview](https://htmlpreview.github.io/?https://raw.githubusercontent.com/wxifaras/hosted-agents-poc/refs/heads/feature/story13-cold-start/docs/reports/story-13/cold-start-validation/2026-11-09/index.html).
The repository snapshot is at
`docs/reports/story-13/cold-start-validation/2026-11-09/index.html`.
It reuses the retained campaign results; no hosted validation is rerun to
generate it. Project/session hashes, response IDs, registry URLs, image digests,
and absolute machine paths are excluded from this publication. Like Story 1,
the new GitHub preview requires the snapshot and this link to be committed,
pushed to `feature/story13-cold-start`; no merge to `main` is required. The local HTML works immediately.

## Project Context

- Status: hosted comparison complete; all 63 planned checks passed; graphs and scenario tasks validated.
- Profile: lightweight, one plan/runbook; no architecture diagrams or new infrastructure.
- Scope owner: the POC owner; readers: contributors and the customer.
- Source: [issue #13](https://github.com/wxifaras/hosted-agents-poc/issues/13).
- Implementation: [isolated experiment](../experiments/story13/).

## Outcomes And Success

| ID | Outcome | Success measure |
| --- | --- | --- |
| OBJ-1301 | Evidence-backed native hosted startup comparison | Ten confirmed cold starts per condition, matched controls, and honest measured change |

Measure whether native Foundry memory snapshots reduce startup time
for a minimal Python Responses hosted agent, and report the actual change without
promising a reduction. Use ten confirmed cold starts per condition, matched warm
controls, and an idle/resume guardrail. No artificial initialization or sleeps
are added to the agent to manufacture an improvement.

## Scope

The approved workload has **no model, MAF, tools, database, or external storage**.
It isolates platform startup, not the customer's complete workload. The broader
issue's peer-agent latency, proactive warm-up, product roadmap, and cross-user
authorization investigations are deferred. Synthetic cross-session markers test
sandbox separation; they do not certify cross-user authorization.

Existing Supervisor, Research, Data Analysis, dependencies, and deployment
configuration must remain unchanged. No deployment, resource provisioning,
permission grants, production routing changes, or hosted requests run as part of
local implementation validation.

## Stakeholders

- STK-1301: POC owner approves scope, hosted budget, and customer interpretation.
- STK-1302: contributors implement and verify; the operator selects existing
  infrastructure and verifies permissions. The customer reads measured results.

## Requirements

| ID | Requirement | Source | Acceptance |
| --- | --- | --- | --- |
| FR-1301 | The probe MUST use native snapshot lifecycle hooks and a no-model Responses handler. | OBJ-1301 | AC-1301 |
| FR-1302 | The runner MUST compare immutable versions under matched project, SDK, image, compute, and configuration controls. | OBJ-1301 | AC-1302 |
| NFR-1301 | Cold-start conclusions MUST have ten observed cold starts per condition and MUST expose failed or missing measurements. | OBJ-1301 | AC-1303 |
| FR-1303 | The experiment MUST validate same-owner reuse, state reset, and synthetic cross-session separation. | OBJ-1301 | AC-1304 |
| FR-1304 | The report MUST graph and analyze measured startup results without invented data. | OBJ-1301 | AC-1305 |
| NFR-1302 | Implementation MUST preserve earlier stories and MUST NOT invoke hosted agents without explicit approval. | OBJ-1301, CON-1301 | AC-1306 |

## Acceptance Criteria

| ID | Requirement links | Observable result | Task | Verification |
| --- | --- | --- | --- | --- |
| AC-1301 | FR-1301 | Local readiness and two actual HTTP Responses requests succeed; synthetic markers persist warm; IDs differ per request. | TASK-1301 | TEST-1301: [probe tests](../experiments/story13/tests/test_probe.py) |
| AC-1302 | FR-1302 | Reject inactive versions, wrong snapshot flags, non-digest images, and mismatched controls; A/B use one image digest. | TASK-1302 | TEST-1302: [measurement](../experiments/story13/tests/test_measure.py) and [report tests](../experiments/story13/tests/test_report.py) |
| AC-1303 | NFR-1301 | Native cold/warm/resume classification is mandatory for passing measurements; report median, range, counts, unavailable values, and every failure; no p95 claim. | TASK-1302 | TEST-1303: transport tests and later hosted JSONL evidence |
| AC-1304 | FR-1303 | New sessions have no previous marker; warm calls retain their own; snapshot resume loses memory/files and has a fresh instance ID; ordinary resume preserves the file. | TASK-1302 | TEST-1304: state tests and later native cold/warm/resume evidence |
| AC-1305 | FR-1304 | Customer-friendly HTML includes two simple comparison graphs, a summary/conclusion, three detailed SVG plots, and sanitized JSON; missing measurements are not zero. | TASK-1303 | TEST-1305: SVG XML, arithmetic, input validation, and escaping tests |
| AC-1306 | NFR-1302 | Separate manifest/lock/environment; preview needs no auth/network/files; hosted invocation requires `--apply`; output never overwrites evidence. | TASK-1304 | TEST-1306: CLI preview, report overwrite, editor wiring, and targeted root tests |

## Traceability

The requirement and acceptance tables link OBJ-1301 to every FR/NFR, AC, TASK,
and TEST. Local evidence establishes code/protocol behavior only; hosted JSONL
is required for startup claims. The Delivery Plan links these to MS-1301 through
MS-1303. Native documentation and inspected root package pins are the current
planning evidence, not measurements of deployed platform performance.

| ID | Acceptance | Evidence | Scope |
| --- | --- | --- | --- |
| TEST-1301 | AC-1301 | Probe unit tests and actual local HTTP readiness/responses | Local lifecycle and protocol correctness |
| TEST-1302 | AC-1302 | Version preflight, transport, configuration, and image comparison tests | Matched immutable deployment controls |
| TEST-1303 | AC-1303 | Transport tests plus later hosted JSONL cold/warm/resume records | Native classification and real timing, after hosted approval |
| TEST-1304 | AC-1304 | Synthetic marker/identity unit tests plus later hosted state observations | Same-owner reuse and session separation, not cross-user authorization |
| TEST-1305 | AC-1305 | Report CLI, SVG XML, arithmetic, missing/failure handling, and escaping tests | Accurate, reusable analysis graphs |
| TEST-1306 | AC-1306 | Preview, no-overwrite, VS Code configuration, and root infrastructure regression tests | Approval gates and preservation of existing behavior |

## Native feature and SDK findings

The existing root project pins Responses `2.2.0b1` and resolves Core `2.1.0`.
The isolated experiment pins Core **2.3.0**, Responses **2.4.0**, and Projects
**2.10.0**. Its own lockfile captures all transitive dependencies. Do not upgrade
the root project's packages for this story.

- Core 2.3.0 provides public before-snapshot, after-restore, and readiness hooks.
  Upgrade it together with Responses 2.4.0: older Responses versions reference
  an internal constant removed by Core.
- Responses 2.4.0 includes protocol-scoped task activation, original-subject
  handling, and user-partitioned local storage/replay changes. Background
  recovery and steering remain disabled in this probe.
- Core 2.3.1b1 is a newer preview with an A2A tracing fix, not a documented
  startup optimization. It is not required for this experiment.
- The SDK's generated `CreateResponse` export is not recognized as a static
  type alias by mypy. The handler accepts `Mapping[str, object]`, matching the
  dictionary request without casts or private SDK imports.
- Native azd snapshot guidance requires azd >= 1.37.0 and the documented
  `azure.ai.agents` extension 1.0.0-beta.23. Check actual versions before any
  deployment; the root project's older declared minimum is not sufficient.

**Critical behavior:** a memory snapshot is the initialized version baseline,
not a checkpoint of a user's ongoing session. Snapshot-enabled idle/resume
currently does not preserve process changes or local files, including `$HOME`.
Do not enable it on the existing file-dependent Data Analysis story.

## Experiment matrix and measurement method

| Condition | Image | Native snapshot flag | Purpose |
| --- | --- | --- | --- |
| A / `baseline` | Baseline target, digest pinned | false | Establish startup baseline |
| B / `snapshot` | Exact same digest as A | true | Isolate snapshot benefit |
| C / `optimized` | Optimized target, new digest | true | Measure incremental build-time bytecode compilation |

Both image targets install dependencies at build time, use a small multistage
runtime, and disable user-site loading. C adds precompiled dependency/stdlib
bytecode on a best-effort basis, and strict application compilation. Thus C
does not claim improvements already present in A/B.

Use the same Foundry project/region, agent name, CPU (0.5), memory (1 GiB),
configuration, input shape, SDK versions, and sequential concurrency. Each
cold POST creates a new native session implicitly; the isolated endpoint is
pinned to one immutable version, and returned session metadata is verified.
Version creation and asynchronous snapshot preparation are outside invocation
timings; record preparation separately if operationally relevant.

Default comparison budget **per condition**: 10 new sessions, 10 first POSTs, 10 warm
POSTs, and 1 resumed POST: at most **21 invocations**, plus session management
and at most three diagnostic GETs per invocation. Across three conditions:
30 new sessions and at most 63 invocations. There are no model tokens.
Keep the machine awake during idle waits. Live runs use 210 seconds to allow
native idle suspension beyond the 120-second threshold.

The configured idle timeout is 120 seconds. Sleeping is not proof of suspension:
only a native `resume` diagnostic passes the resume check. An unexpected `warm`,
`cold`, or absent classification is a recorded failure, not silently relabeled
or retried. Adjust the idle wait in a separately identified run if needed.

Include `x-ms-debug-latency-enabled: true`. Capture native platform preprocessing,
infrastructure setup, container readiness, handling/response headers, and
first/last-byte timings when available. Diagnostic GETs retrieve original
request timings; their duration is not added to client POST timings.

Client observations measure response headers, first body chunk, and full
response completion. Implicit session creation is inside the cold POST's timed
journey. A response-begin header
is **not** time to first useful body content; first body chunk is **not** model
time-to-first-token. Complete JSON delivery is the useful response for this
non-streaming no-model workload. The cold **client journey** graph includes
session creation and response completion in one POST; warm/resume reuse the
returned session ID. Do not pre-create sessions for cold measurements: live
validation showed `create_session` warms the container before the first POST.
That moves startup cost; it does not eliminate it. The earlier explicit-create
diagnostic files retain their separate session-creation timings.

Persist each attempt immediately to JSONL. Save only allowlisted native headers,
response correlation IDs, version/image controls, timestamps, hashed session/
project bindings, numeric timings, and synthetic comparison outcomes. Do not
save credentials, caller identity, arbitrary headers, or the synthetic markers.

### Graphs and analysis

The [report generator](../experiments/story13/report.py) includes a summary and
conclusion, key result cards, and two simple zero-based bar charts:
median first-response time and median application-preparation time. Both charts
have downloadable SVG versions. It also writes `summary.md` for easy reuse.
Before the first graph, **What we tested** explains the isolated hosted architecture,
matched snapshot-capable SDK versions, native snapshots, and build-time optimizations.

An expandable technical appendix retains:

1. **Client startup distributions:** cold session creation plus response
   completion, and warm/resume POST completion, per condition.
2. **Container-readiness distributions:** cold and resumed calls per condition.
3. **Native cold-start components:** platform, infrastructure, readiness, and
   request handling, shown separately rather than misleading stacked medians.

The detailed plots retain actual passing observations, median, minimum/maximum,
and sample counts. The simple bars show medians only. Failed/misclassified
attempts are excluded from distributions but remain in the attempt table;
the main report discloses initial diagnostic failures. Missing data is unavailable, not zero.
Analysis reports percentage median change versus A, including regressions.
Fewer than ten measured cold readiness values is insufficient readiness evidence.
An incomplete/failed run cannot be presented as accepted simply because HTML
generation succeeded. Small samples do not establish p95 or significance.

## Local implementation and validation

From the repository root:

```powershell
Set-Location experiments\story13
uv sync --frozen --link-mode copy
uv run --frozen --no-sync ruff check .
uv run --frozen --no-sync ruff format --check .
uv run --frozen --no-sync mypy
uv run --frozen --no-sync pytest -q
```

`--link-mode copy` avoids OneDrive hardlink failures. Local tests include an
actual temporary HTTP server; its readiness/responses are not hosted latency
measurements. The server is stopped and synthetic files stay in test temp storage.

For manual local testing, run `uv run --frozen --no-sync python main.py`.
Port 8093 defaults to loopback. For interactive VS Code testing, select
**Story 13 cold-start + Foundry Agent Inspector** and press **F5**, after running
the **Setup: Sync Story 13 dependencies** task. Inspector uses 8093; debugger
uses 5683. Stop the Story 13 task when finished.

### VS Code scenario validation tasks

Use **Terminal > Run Task**. The [workspace tasks](../.vscode/tasks.json) follow
the other stories' explicit validation workflow and use Story 13's own virtual
environment. Run **Setup: Sync Story 13 dependencies** first.

| Task | Scenario and expected evidence | Acceptance |
| --- | --- | --- |
| Agents: Story 13 - 0. Inspect endpoint routing | Read-only native routing/protocol inspection of the isolated agent | AC-1302 |
| Agents: Story 13 - 1. Baseline cold/warm/resume | Version 1 by default; snapshots disabled; ten cold, ten warm, one resumed request; ordinary resumed file survives | AC-1302, AC-1303, AC-1304 |
| Agents: Story 13 - 2. Snapshots cold/warm/resume | Version 2 by default; same image as baseline; restored baseline is clean; warm state survives; idle/resume resets local state | AC-1302, AC-1303, AC-1304 |
| Agents: Story 13 - 3. Optimized snapshots cold/warm/resume | Version 3 by default; snapshot restore plus optimized image; same state/latency checks | AC-1302, AC-1303, AC-1304 |
| Agents: Story 13 - 4. Generate startup analysis graphs | Offline customer-friendly HTML, two simple charts, detailed plots, short summary/conclusion, failures, and sanitized JSON | AC-1305 |
| Quality: Run all Story 13 local checks | Sequential lint, formatting, types, unit/transport tests, and a temporary real local HTTP host; no Azure | AC-1301, AC-1306 |

Each measurement task asks for **verify / preview / run**:

- **verify (default):** metadata GETs only; verify the active version, snapshot
  flag, digest-pinned image/compute, and exact endpoint routing. No session,
  invocation, or evidence-file creation.
- **preview:** show the invocation budget without resolving an environment,
  authenticating, writing files, or calling Azure.
- **run:** explicitly execute the approved paid scenario. No POST retries,
  implicit deployment, routing changes, or permission grants.

The tasks resolve endpoint aliases from the selected **isolated** azd environment
(default `story13-cold-start`); they do not embed subscription/project URLs.
Before each scenario, set the tested `agentVersion` in the experiment's
[native endpoint configuration](../experiments/story13/azure.yaml), then run
**Agents: Story 13 - Apply configured endpoint routing**. This explicit action
patches only the existing Story 13 endpoint, with no build or new version.
Inspect routing, then verify/run the matching scenario. A mismatched route
blocks the runner rather than invoking a different version.

Use the **same new run-folder name** for all three scenarios and graph generation,
for example `tasks-run-01`. They write `baseline.jsonl`, `snapshot.jsonl`, and
`optimized.jsonl` under that folder in `artifacts/story-13/`. Choose a fresh
report-folder name to regenerate graphs without overwriting existing output.
The three hosted scenarios deliberately have no automatic aggregate: execute
them sequentially with the correct native version routing, not concurrently.
Finish with the endpoint pinned to the optimized version.

The original live campaign also has failed calibration/capture files with
different filenames. Include them via report CLI `--diagnostic-input` when
publishing that campaign; they must not be hidden or pooled into valid samples.

### Reproduce the repository HTML snapshot

The [offline publisher](../experiments/story13/publish.py) follows Story 1's
repository-report pattern. It validates the original controls before redaction,
embeds every graph and download so HTML Preview needs no relative assets, and
includes the full issue document, acceptance coverage, reviewed task steps,
all recorded timings, and diagnostic failures. The downloadable
`sanitized-evidence.json` uses report-local session/image aliases, not raw bindings.
Unreviewed failure text blocks publication until reviewed. The Markdown renderer
is a development-only dependency and is not installed into the agent image.
Eight dedicated publication regressions passed, bringing the isolated experiment
suite to 45 tests. These are offline code checks, not extra hosted invocations.

```powershell
# From experiments\story13. Offline; no authentication, deployment, or hosted requests.
uv sync --frozen --link-mode copy
uv run --frozen --no-sync python publish.py --input ..\..\artifacts\story-13\baseline-v1-implicit.jsonl ..\..\artifacts\story-13\snapshot-v2-implicit.jsonl ..\..\artifacts\story-13\optimized-v3-implicit.jsonl --diagnostic-input ..\..\artifacts\story-13\baseline-v1.jsonl ..\..\artifacts\story-13\baseline-v1-fixed.jsonl --issue-source ..\..\docs\issue-13-validate-hosted-agent-cold-start.md --output ..\..\docs\reports\story-13\cold-start-validation\2026-11-09
```

The command refuses an existing output directory. To regenerate, choose a new
dated snapshot directory and update the preview link. Original inputs remain
local/ignored; the repository HTML and sanitized download are readable without them.
Report generation is not a new acceptance verdict or a source-commit binding.

Input is a 32-character lowercase hexadecimal synthetic marker, for example
`aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`. Manual runs write one synthetic file
`.story13-synthetic-marker` under your local home; remove only that file after
testing. Never bake it into an image or snapshot.

## Hosted execution - approved bounded run

The POC owner approved reuse of `hosted-agents-poc-dev` in North Central US and
its existing registry, two remote builds, three isolated Story 13 versions,
30 sessions, and at most 63 no-model invocations. This approval does not include
new infrastructure, permission grants, broader retries, or automatic cleanup.

Execution update: both images built successfully; versions 1 (baseline),
2 (same-image snapshots), and 3 (optimized snapshots) are active. An initial
command-approval denial was cleared by explicit user approval. The stable azd
extension resolves `FOUNDRY_PROJECT_ENDPOINT`; configure that alias alongside
`AZURE_AI_PROJECT_ENDPOINT` in the experiment's environment.

The initial ten capture attempts exposed a client parser incompatibility: native
no-tool replies omit `tools` and `tool_choice`. A subsequent diagnostic exposed
prewarming by explicit session creation. Both are fixed and covered by tests.
The user approved replacement measurements and a revised total ceiling of
**41 sessions / 74 invocations**, including those eleven diagnostic attempts.
The original JSONL files are preserved and must be disclosed with
`--diagnostic-input`, not counted as valid cold-start samples.

1. Select the existing Foundry project and registry explicitly; verify access
   and native tooling. No new infrastructure or broad grants are part of this plan.
2. Build/push the `baseline` Docker target for `linux/amd64`, locally or with
   an approved ACR remote build. Record its immutable image digest.
3. In this experiment's own azd environment, configure the existing project
   endpoint/ARM ID and `STORY13_IMAGE` as a digest-pinned image reference.
   The [.env.example](../experiments/story13/.env.example) is a reference, not
   an automatically loaded configuration file. Do not copy secret-bearing envs.
4. Set `enableMemorySnapshot: false` and deploy A.
   Wait until the resulting immutable version is `active`; record the version.
5. Change only that native YAML flag to literal `true`, enable the shell switch
   `AZD_ALPHA_ENABLE_AGENTS_MEMORY_SNAPSHOT=true`, and deploy B with A's exact
   image digest. Wait for `active`. A successful create command is not readiness.
6. Build/push the `optimized` target; change only `STORY13_IMAGE` to its digest,
   keep snapshots enabled, and deploy C. Again wait for `active`.
7. Before each condition, set `agentEndpoint.versionSelector.versionSelectionRules`
   to `FixedRatio`, the actual `agentVersion`, and `trafficPercentage: 100`.
   Run native `azd ai agent endpoint update story13-probe --force --no-prompt`
   with the snapshot feature switch enabled. This updates only the isolated
   Story 13 endpoint, creates no new version, and leaves original agents alone.
   The runner rejects `@latest`/split routing and verifies the returned session
   belongs to the selected version. After measurements, leave the endpoint on C.
8. Preview each budget, then execute only the separately approved commands.
   Replace all example values with the selected project and actual versions.

```powershell
# From experiments\story13. Preview: no authentication or Azure requests.
uv run --frozen --no-sync python measure.py --project-endpoint "https://<account>.services.ai.azure.com/api/projects/<project>" --version 1 --condition baseline --idle-seconds 210 --output ..\..\artifacts\story-13\baseline.jsonl

# After approving this specific hosted budget, add --apply.
# Repeat for condition snapshot and optimized with their actual explicit versions.
# Do not use @latest or reuse the same output file.

uv run --frozen --no-sync python report.py --input ..\..\artifacts\story-13\baseline.jsonl ..\..\artifacts\story-13\snapshot.jsonl ..\..\artifacts\story-13\optimized.jsonl --output ..\..\artifacts\story-13\report
```

Open the generated `index.html`; each SVG and sanitized evidence JSON can be
downloaded without a web server, JavaScript, or external assets. Report exit 0
means rendered, not validated. Measurement exit 0 means all recorded checks
passed; exit 1 means observed failures; exit 2 means blocked/invalid invocation.
Report exit 2 means invalid evidence or an I/O failure.

The runner itself does not deploy, grant access, alter routes, force-stop sessions, or
delete resources. Its sessions are left to native idle expiry. Confirm cost and
retention expectations; after the experiment, review and remove only Story 13
versions/sessions/images as a separately approved cleanup. If adopted later,
rollback means routing to a known snapshot-disabled version, not modifying an
immutable version.

## Observed Hosted Results

The completed campaign used versions 1, 2, and 3 sequentially, with ten native
`cold` observations, ten `warm` observations, and one confirmed `resume` per
condition. All **63 planned checks passed**. Native headers, full client timings,
version bindings, and synthetic state checks are in the saved evidence.

| Condition | Median cold useful response | Median cold container readiness | Cold samples |
| --- | --- | --- | --- |
| Baseline / snapshots off | 6.390 s | 4,264 ms | 10 |
| Same image / snapshots on | 1.461 s | 135 ms | 10 |
| Optimized image / snapshots on | 1.361 s | 122.5 ms | 10 |

Same-image snapshots reduced the observed median useful-response time by
**77.1%** and container-readiness time by **96.8%**. The optimized image's total
reductions versus baseline were 78.7% and 97.1%, respectively. Its additional
useful-response improvement versus snapshots alone was about 100 ms / 6.8%;
this small sequential sample does not establish a statistically significant
incremental bytecode benefit.

Warm medians were 488 / 486 / 472 ms, respectively. The single resumed requests
took 6.750 / 3.979 / 2.435 s. Resume is a guardrail observation, not a population
latency estimate. Snapshot cold ranges were 1.264-4.947 s and optimized ranges
were 1.171-5.308 s: snapshots do not eliminate infrastructure or network variance.
No p95, SLA, or customer-workload latency promise is implied.

The report is local generated output at
`artifacts/story-13/report-live-20261109/index.html`, alongside three downloadable
SVGs and sanitized `evidence.json`. It was opened in the integrated browser;
all three SVGs loaded without horizontal page overflow. Generated evidence is
ignored by Git and is not included in a fresh clone.

The customer-friendly edition is at
`artifacts/story-13/report-customer-20261109/index.html`, with a downloadable
`summary.md`, two simple comparison SVGs, and the original detailed plots and
evidence. The original technical report remains unchanged. The new presentation
was checked at desktop (1280 px) and mobile (390 px) widths without horizontal
page overflow.

Comparison inputs under `artifacts/story-13/` are:

- `baseline-v1-implicit.jsonl`
- `snapshot-v2-implicit.jsonl`
- `optimized-v3-implicit.jsonl`

The report also discloses `baseline-v1.jsonl` and `baseline-v1-fixed.jsonl` via
`--diagnostic-input`. These contain **11 failed capture/methodology attempts**,
not valid cold samples: ten exposed the generic OpenAI parser's required
tool-field mismatch, and one exposed explicit-session prewarming. They are
retained and labeled separately, not silently discarded or plotted as successes.
Total execution stayed within the revised **41-session / 74-invocation** ceiling,
with two remote builds and three deployed versions. There were no POST retries,
new infrastructure, permission grants, or automatic cleanup.

The isolated endpoint is left pinned to version 3. Existing POC agents and
root dependencies remain unchanged. The native CLI still labels snapshot
enablement as a preview feature. Recommend snapshots first for eligible,
externally persisted/stateless workloads; validate real model/tool calls and
state requirements separately before adopting them for customer workloads.

The campaign's original local verification covered 37 experiment tests and 50 targeted editor/infrastructure
regressions passed, alongside Ruff, formatting, strict typing, and the plan
scanners. The new read-only task mode was also checked against live version 3
metadata and created no invocation or evidence file.

## Risks And Assumptions

| ID | Risk or assumption | Owner and response |
| --- | --- | --- |
| RISK-1301 | Snapshots lose local state across idle/resume | POC owner: keep this agent synthetic and isolated; do not enable existing file-dependent stories |
| RISK-1302 | A minimal agent has little reusable initialization or platform provisioning dominates | POC owner: accept zero/negative benefit as evidence; do not add fake work |
| RISK-1303 | Best-effort diagnostics are missing or idle timing is variable | Contributor: preserve unknowns/failures; extend a new approved run rather than infer a cold start |
| RISK-1304 | Image/deployment/tooling availability prevents hosted execution | Operator: verify versions, registry pulls, region availability, and permissions before spending the run budget |
| ASM-1301 | The existing Foundry project supports snapshot-enabled hosted versions | Operator: validate at hosted approval; failure blocks the experiment, not a reason to provision silently |
| CON-1301 | No promised reduction, cross-user certification, or customer-workload extrapolation | POC owner: enforce in the customer results narrative |

## Delivery Plan

| ID | Outcome | Requirement Links | Depends On | Owner | Status |
| --- | --- | --- | --- | --- | --- |
| TASK-1301 | Isolated agent and locked SDKs | FR-1301, AC-1301 | None | Contributor | Done |
| TASK-1302 | Native version-bound measurement and state probes | FR-1302, NFR-1301, FR-1303, AC-1302, AC-1303, AC-1304 | TASK-1301 | Contributor | Done |
| TASK-1303 | Measured HTML/SVG analysis | FR-1304, AC-1305 | TASK-1302 | Contributor | Done |
| TASK-1304 | Runbook and final local verification | NFR-1302, AC-1306 | TASK-1301, TASK-1302, TASK-1303 | Contributor | Done |

- **MS-1301 / local readiness:** TASK-1301 through TASK-1304 have passing local
  tests/lint/types and the plan is saved. No cloud performance conclusion yet.
- **MS-1302 / hosted evidence:** after explicit approval and active versions,
  TEST-1303/TEST-1304 record matched cold/warm/resume evidence for all conditions.
- **MS-1303 / customer review:** AC-1301 through AC-1306 have evidence or explicit
  limitations; graphs and the actual measured reduction/regression are reviewed.

No delivery date or reduction threshold is promised. MS-1302 depends on hosted
approval and MS-1301; MS-1303 depends on MS-1302. TASK-1302 depends on TASK-1301;
TASK-1303 depends on TASK-1302; final TASK-1304 validation covers all three.

## Approval

The POC owner approved implementation and result-analysis graphs, then explicitly
approved the existing project/registry and the bounded hosted budget above.
Changes to other stories, extra infrastructure/grants, broader retries, cleanup,
and customer performance claims remain outside that approval. Review the actual
results and limitations before customer use.

## Sources

- [Enable memory snapshots](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/use-hosted-agent-memory-snapshots?tabs=azd&pivots=python)
- [Reduce startup latency](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/optimize-hosted-agent-latency?tabs=python)
- [Diagnose invocation latency](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/debug-hosted-agent#diagnose-invocation-latency)
- [Native azd service configuration](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/azure-yaml-reference)
- [Core changelog](https://raw.githubusercontent.com/Azure/azure-sdk-for-python/refs/heads/main/sdk/agentserver/azure-ai-agentserver-core/CHANGELOG.md)
- [Responses changelog](https://raw.githubusercontent.com/Azure/azure-sdk-for-python/refs/heads/main/sdk/agentserver/azure-ai-agentserver-responses/CHANGELOG.md)
