# Implementation plan: Hosted Data Analysis capacity

- **Story:** [#1 - Validate Hosted Agent Memory and Data Analysis Capacity](https://github.com/wxifaras/hosted-agents-poc/issues/1)
- **Plan date:** 2026-10-22
- **Status:** Step 1 is implemented and locally tested. Its live deployment was attempted on 2026-10-22 but hosted activation failed with an image-pull HTTP 403; managed interpreter execution remains unverified. Steps 2-5 remain planned. See [ADR execution evidence](../adr/0001-hosted-agents-poc.md#story-1-step-1-execution-evidence-2026-10-22).
- **Task interpretation:** One runnable VS Code Task per acceptance criterion, selected through **Terminal > Run Task**.

## Goal and scope

Evaluate the existing Data Analysis Hosted Agent using Microsoft-managed Code
Interpreter, with reproducible synthetic-data demonstrations and readable
Markdown results. Keep Hosted Agent container resources, hosted sessions,
Code Interpreter sandbox resources/sessions, conversations, and durable storage
separate in configuration, measurements, and conclusions.

- Use only authorized POC resources and synthetic data.
- Run bounded, sequential functional cases. No load, concurrency, stress,
  multi-user scale testing, intentional OOM, or process-kill experiments.
- Record tested behavior, documented capabilities, architectural assessments,
  and unverified questions separately. Untested does not mean unsupported.
- Coordinate representative output patterns with #4, conversation/state
  association with #3, and approval-related suspension with #6.
- Process-loss recovery (#7) and long-term memory (#8) are separate work, not
  completion prerequisites. This story covers ordinary sandbox recreation and
  explicit rehydration, not transparent process-loss recovery.

## Repository baseline and implementation decisions

| Area | Current evidence | Planned treatment |
|---|---|---|
| Agent runtime | [runtime.py](../../src/backend/app/runtime.py) creates a `FoundryChatClient` without tools; Data Analysis instructions explicitly prohibit claiming execution. | Attach managed Code Interpreter through the supported Foundry toolbox integration for Data Analysis only. Update instructions only when execution is wired. |
| Deployment | [azure.yaml](../../azure.yaml) has three independent hosted services. Data Analysis declares 0.5 CPU, 1 GiB, and a 120-second hosted-session idle timeout. | Reuse the foundation and deploy the selected Data Analysis service. These settings are not Code Interpreter limits. |
| Existing architectural decision | [ADR-0001](../adr/0001-hosted-agents-poc.md) proposes developer-controlled, in-container Python first. | Record the newer story's managed Code Interpreter baseline as a scoped decision change before implementation; do not silently substitute local Python or a customer-managed session pool. |
| Transport and artifacts | [hosting.py](../../src/backend/app/hosting.py) returns text; [delegation.py](../../src/backend/app/services/delegation.py) transports string answers; [trigger_agent.py](../../scripts/run_scenario/trigger_agent.py) extracts text rather than downloadable outputs. | Preserve tool-result/file metadata and provide an authenticated artifact-download path. Do not treat model-generated file names or sandbox paths as downloadable artifacts. |
| Supervisor delegation | [README](../../README.md) and the ADR record a nested caller-context guard blocking hosted delegation. | Use direct Data Analysis-to-user demonstrations first, which satisfy the story's "supervisor or user" output path. Supervisor delivery remains explicitly blocked until its authorization contract is validated; do not weaken guards for this story. |
| Durable state | [foundry_state.py](../../src/backend/app/services/foundry_state.py) and [workflows.py](../../src/backend/app/services/workflows.py) provide isolated mappings, strict session bindings, and a 200 KB application document guard. | Reuse small typed metadata/checkpoints, not binary files or Python heap serialization. Add explicit rehydration semantics without weakening existing continuation checks. |
| Reporting | [reporting.py](../../scripts/common/reporting.py) already persists sanitized manifests/events/results and renders Rich output. | Extend this existing reporter with an opt-in Markdown report/rendering path; preserve existing JSONL, redaction, and exit-code behavior. |
| Editor tasks | [tasks.json](../../.vscode/tasks.json) uses explicit `uv` process tasks and separates paid actions from setup and local checks. | Add exactly five story demonstration tasks using the existing `Agents:` category and frozen/no-sync invocation pattern. |

### Important platform findings to carry into implementation

These are **documentation findings**, not observations from this POC.

- Microsoft documents a managed Code Interpreter sandbox backed by Azure
  Container Apps dynamic sessions with a Hyper-V isolation boundary.
- The current hosted-agent example uses `FoundryChatClient` plus
  `FoundryToolbox`. Verify compatibility against the repository's locked SDKs
  before changing dependencies; do not assume a sample targets the same versions.
- The toolbox-specific documentation states that **user isolation is not
  supported and users in the same project share the same container context**.
  Do not replace this specific warning with generic claims about separate
  sessions per conversation. Treat it as a major suitability constraint and
  use a controlled synthetic-only POC scope.
- The toolbox-specific REST instructions require resource-level file upload
  with the documented project GUID/header and warn about ownership verification
  failures with project-scoped uploads. The Python example on the same page uses
  project-scoped upload. Resolve this documentation/API discrepancy with a tiny
  authorized upload probe and record the working endpoint, headers, and SDK/API
  versions without persisting credentials.
- The documentation describes a one-hour active lifetime, 30-minute idle
  timeout, and no outbound network access for managed Code Interpreter.
  Verify applicability to the selected toolbox configuration. Do not copy
  configuration options from customer-managed ACA session pools and imply that
  Foundry exposes them.

Source: [Code Interpreter for Foundry agents][code-interpreter], especially the
hosted-agent, toolbox REST, and sandboxed execution sections, consulted 2026-10-22.

## Five-task demonstration map

Step 1's Task and report are implemented. Steps 2-5 below are **proposed**, not
currently available tasks. Task implementation is not the same as acceptance:
Step 1's live acceptance remains blocked by the recorded hosted image-pull failure.

| Step / criterion | Proposed VS Code Task | Main Markdown result | Execution prerequisites |
|---|---|---|---|
| 1. Deploy managed Code Interpreter | `Agents: Story 1 - Deploy and verify managed interpreter` | `01-deployment.md` | Approved environment, tooling/auth/access, supported integration, explicit deployment approval |
| 2. Analyze and deliver outputs | `Agents: Story 1 - Demonstrate analysis and outputs` | `02-analysis-and-outputs.md` | Verified Step 1 deployment and tiny upload/download path |
| 3. Evaluate increasing datasets | `Agents: Story 1 - Evaluate dataset capacity` | `03-capacity.md` | Step 2 correctness checks and operator-approved bounded dataset set |
| 4. Assess sandbox boundaries | `Agents: Story 1 - Assess sandbox boundaries` | `04-sandbox-boundaries.md` | Documentation available; live probes additionally require Step 1 |
| 5. Assess continuity and storage | `Agents: Story 1 - Assess state continuity` | `05-state-continuity.md` | Step 1 deployment and Step 2 artifact/checkpoint path; Step 4 findings inform interpretation |

Implement shared reporting while building Step 1, then Steps 2 and 3. Step 4
documentation research can proceed independently. Implement Step 5 after the
artifact path exists. These are logical prerequisites, **not automatic task
dependencies that redeploy or rerun paid tests**.

## Shared Task and Markdown evidence contract

Implement one scenario entry point, proposed as
`scripts.run_scenario.data_analysis_capacity`, with a `--step` selector for the
five cases. Keep the scenario orchestration separate from runtime tool handling;
reuse current deployment discovery, authentication, HTTP diagnostics, and
reporting helpers rather than introducing another framework.

### Task behavior

- Use `type: process`, `command: uv`, and arguments starting with
  `run --frozen --no-sync --group validation python -m`.
- Reuse the existing environment input. Add a shared evidence-root input for
  the operator's demonstration ID and a bounded dataset-profile selection.
  Each invocation gets a unique run directory; reruns never overwrite evidence.
- Use a dedicated, always-visible terminal panel. Render the same sanitized
  Markdown through Rich for terminal readability and persist the Markdown
  source for VS Code preview.
- Print the report path at completion and after a blocked/failed attempt.
  The operator opens that file and selects **Open Preview** (`Ctrl+Shift+V`)
  to see tables, artifact links, and embedded plots. No new extension is required.
- Keep task execution explicit: no `runOn: folderOpen`, background service,
  implicit login, automatic role grants, or deployment hidden in Steps 2-5.
- Step 1 first shows the selected environment/service and intended mutations.
  Require an explicit deployment/apply choice before creating toolbox versions
  or agent versions. Offer verification-only mode for an existing version.
  RBAC changes remain separate, reviewed access tasks.
- For required prerequisites, validate the selected evidence manifest against
  environment, deployed agent/toolbox version, and dataset profile. Stale or
  mismatched evidence is `BLOCKED`, not silently reused.
- Long waits and network operations have bounded timeouts. No automatic paid
  inference retries. Report partial results and cleanup status on failure or
  cancellation; preserve incrementally written evidence if forcibly terminated.

### Evidence layout

Under the existing ignored artifacts directory, use:

```text
artifacts\story-1\<demonstration-id>\
  index.md
  <unique-step-run-id>\
    manifest.json
    events.jsonl
    results.jsonl
    <step-report-name>.md
    inputs\
    outputs\
```

The index links every step report and its current acceptance status. Each
report includes:

1. Criterion, task label, UTC timestamp, run ID, and execution mode.
2. Sanitized configuration, agent/image/toolbox/model versions, region,
   CLI/extensions, Python and relevant SDK/API versions.
3. Reproduction inputs and steps, including synthetic generator seed, schema,
   actual row/byte counts, and input/output SHA-256 checksums.
4. Expected-versus-observed Markdown table, plus concise model summary and
   sanitized tool execution evidence. Do not publish hidden reasoning, tokens,
   call-context headers, credentials, or signed URL query strings.
5. Downloadable local copies of authorized output files and a relative Markdown
   image link for each validated plot.
6. Capability ownership, evidence basis, acceptance verdict, limitations,
   follow-ups, and cleanup outcome.

Use separate fields rather than collapsing evidence quality into one green status:

| Field | Values / rule |
|---|---|
| Check status | Existing `PASS`, `FAIL`, `BLOCKED`, `SKIP` |
| Evidence basis | `Tested`, `Documented`, `Architectural assessment`, `Unverified` |
| Acceptance verdict | `Met`, `Partially met`, `Not met`, `Not evaluated` |
| Capability ownership | `Foundry-managed`, `Agent Framework / SDK-managed`, `Custom implementation (Dev Team)`, `Not supported` |

Assign ownership only when established; otherwise leave it unclassified and
record the question. Use `Not supported` only for the specific, evidenced
capability/configuration, never for missing access or an unrun test.

Preserve reporter exit codes: `0` when all executed checks pass, `1` for
failure/skip, `2` for blocked prerequisites (failure takes precedence). A passed
documentation-assessment check does not prove live deployment or execution.
Documentation-only runs can complete an assessment but must expose untested
live checks and must not mark a live demonstration `Met`.

---

## Step 1 - Deploy and verify managed Code Interpreter

**Acceptance criterion:** Deploy a Foundry Hosted Data Analysis Agent using
Microsoft-managed Code Interpreter, backed by Azure Container Apps dynamic sessions.

**VS Code Task:** `Agents: Story 1 - Deploy and verify managed interpreter`

### Implementation

1. Confirm the target azd environment, Data Analysis service, region/model/tool
   availability, caller identity, required permissions, and spending approval.
   Capture installed versions and read back actual deployment configuration.
2. Reconcile the ADR with the story's managed-sandbox baseline. Keep the
   Data Analysis service `kind: hosted`; do not convert it to a prompt agent.
3. Verify the pinned framework/SDK integration for the documented Code
   Interpreter toolbox. Configure its endpoint through typed settings and the
   selected service environment only. Create or reuse an approved toolbox
   version through supported tooling, not dynamically on every request.
4. Wire the supported `FoundryToolbox` lifecycle and credentials into Data
   Analysis composition. Preserve Supervisor/Research behavior and existing
   fail-closed caller/state checks. Add no local `exec`/subprocess fallback.
5. Resolve upload ownership with one tiny synthetic CSV. Keep resource-level
   versus project-level file APIs explicit and verified. Record identifiers
   sufficient for reproduction without exposing authorization material.
6. After approval, deploy the changed service using the repository's azd flow.
   Reuse the foundation; do not reprovision or grant broad roles automatically.
   Read back active version/routing and run a small arithmetic/CSV tool call.
7. Capture an actual managed tool invocation and returned execution result.
   Associate a sandbox/container identifier when exposed. If the underlying
   ACA resource is not customer-visible, cite the documented backing and label
   direct infrastructure inspection as unavailable, not as tested.

**Expected output:** Deployment/configuration table, separate host/sandbox
settings, verified execution result, upload outcome, ownership classifications,
and sanitized invocation IDs in `01-deployment.md`.

**Validation:** Unit-test role-specific tool registration, configuration and
credential failures, and no-tool behavior for other roles. Hosted verification
must show real Code Interpreter execution; successful text-only smoke or tool
enumeration alone is insufficient.

**Exit gate:** A usable hosted deployment executes through the managed tool.
If region, permissions, API compatibility, or access prevent this, report the
specific blocker and documentation evidence; do not claim the criterion is met.

## Step 2 - Demonstrate analysis and deliver outputs

**Acceptance criterion:** Demonstrate filtering, grouping, summarization, and
visualization using synthetic tabular data. Show how summaries, files, and
visuals reach the supervisor or user.

**VS Code Task:** `Agents: Story 1 - Demonstrate analysis and outputs`

### Implementation

1. Generate deterministic synthetic sales data with a fixed seed and a small,
   stable schema: row ID, date, region, category, units, unit price in cents,
   revenue in cents, and status. Include known null/category cases and compute
   an independent expected-results fixture.
2. Upload the small agreed fixture and request four explicit operations:
   filter completed rows in a chosen region; group by category; summarize
   count/units/revenue; produce a bar chart of the grouped revenue.
3. Return a bounded typed result containing the text summary, structured
   aggregates, and real artifact references with name, media type, length,
   checksum, and available container/file IDs. Preserve SDK output metadata
   through the host response rather than reducing everything to `response.text`.
4. Download the filtered CSV, grouped summary CSV, and PNG using the applicable
   authenticated API. Validate file type, safe basename, size bound, and content;
   never follow arbitrary model-generated URLs or write traversal paths.
5. Show the direct user path: hosted Data Analysis response -> authenticated
   scenario client download -> local Markdown table, CSV links, and embedded
   PNG. The code sandbox's own path is not the user's download path.
6. If the separately validated Supervisor delegation path is available, preserve
   the same references through its report/child result and demonstrate it too.
   Otherwise explicitly mark that branch `BLOCKED`; direct user delivery is
   sufficient for this criterion. Do not loosen identity checks to make it pass.

**Expected output:** Filtered sample table, group totals, summary, validated file
links/checksums, inline chart, and a delivery-path table in
`02-analysis-and-outputs.md`. Large tables are downloadable files, not terminal dumps.

**Validation:** Compare counts and integer-cent sums exactly against the
independent fixture; define tolerance only for calculated averages. Verify that
downloaded CSV data matches the fixture, the PNG decodes and is nonempty, and
the plotted values match the verified aggregates. Test missing/malformed file
citations, failed downloads, unauthorized access where applicable, and partial
tool failure. Natural-language plausibility is not the correctness oracle.

**Exit gate:** All four operations execute, their numerical results are checked,
and the user can actually retrieve files and view the plot.

## Step 3 - Evaluate progressively larger datasets

**Acceptance criterion:** Test a small, agreed set of progressively larger
datasets. Record observed limits and failures, and conclude whether the tested
workloads are viable in the evaluated configuration.

**VS Code Task:** `Agents: Story 1 - Evaluate dataset capacity`

### Implementation

1. Before executing, obtain agreement on dataset sizes, fixed workload, request
   timeout, and any viability target. Proposed starting set: **1,000, 10,000,
   and 100,000 rows**, using the same eight-column schema. These sizes are a
   proposal, not an agreed requirement or an assertion of platform capacity.
2. Generate and checksum each dataset locally; measure actual serialized bytes.
   Reuse Step 2's operations and independent aggregates for a comparable
   functional workload. Keep the seed, null distribution, output formats, and
   sandbox-session reuse/freshness policy fixed and recorded.
3. Execute one case at a time, with one measured attempt per approved size.
   Bound upload, execution, download, artifact sizes, and total request count.
   Do not add progressively larger cases automatically or search for a crash limit.
4. Record upload/execution/download/total duration where observable, tool
   status, error category, input/output bytes, correctness, and artifact access.
   Record sandbox memory/CPU metrics only if the service/tool exposes them.
   Otherwise show `Not observable`; hosted-container metrics are not substitutes.
5. On a timeout, resource error, unexpected artifact growth, or quota response,
   stop further escalation and preserve evidence. An operator may approve a
   later diagnostic rerun, recorded as a separate attempt.
6. Produce a configuration-specific conclusion: viable for these tested inputs,
   not viable under the agreed requirements, or inconclusive. If no SLA was
   agreed, conclude functional viability only and report timings without
   inventing a performance threshold.

**Expected output:** One Markdown row per approved dataset with rows, bytes,
correctness, timings, observable resources, files, outcome, and failure detail
in `03-capacity.md`; followed by the largest successful tested case, first
observed failure if any, and scope-limited viability recommendation.

**Validation:** Test generator determinism, expected aggregates, bounded case
selection, timeout/stop behavior, metrics units, and partial report generation.
Assert the runner never launches concurrent work or silently retries inference.

**Exit gate:** The agreed bounded cases have results or explicit not-run reasons,
and there is an evidence-backed conclusion. A small all-pass run demonstrates
the tested envelope, not the platform's maximum capacity.

## Step 4 - Assess sandbox security, lifetime, and configuration

**Acceptance criterion:** Document sandbox security boundaries, session lifetime,
network restrictions, and available configuration options.

**VS Code Task:** `Agents: Story 1 - Assess sandbox boundaries`

### Implementation

1. Assemble a source-dated matrix covering Hyper-V/session boundaries, identity
   and project scope, filesystem/artifact access, active/idle lifetime, egress,
   private networking, region, resource tiers, packages/runtime, and exposed
   configuration settings. List exact API fields where verified.
2. Distinguish Foundry-managed Code Interpreter behavior from hosted-agent
   container behavior and from separately provisioned ACA session pools.
   Mark each setting as exposed, fixed/documented, or unverified.
3. Explicitly record the hosted-toolbox warning about shared project container
   context and unsupported user isolation. State the resulting limitation for
   sensitive or multi-tenant workloads; application-level download authorization
   cannot establish isolation inside a shared sandbox.
4. Where authorized and feasible, run only targeted functional probes:
   temporary synthetic marker visibility across selected sessions/identities,
   access to run-owned files, and one bounded outbound request to an approved
   benign test endpoint. No network scans, escape attempts, or secrets in files.
   Distinguish policy rejection, DNS failure, and unavailable diagnostics.
5. For lifetime, record documented values separately from an optional bounded
   idle/expiry observation. Never infer Code Interpreter expiry from the
   Data Analysis host's 120-second idle setting. If a one-hour test is not
   practical, document the gap rather than simulate elapsed time.
6. Record exposed settings and sanitized configuration readback. Document
   unavailable options; do not create a custom session pool to make the
   managed baseline appear more configurable.

**Expected output:** Security/lifecycle/configuration matrix in
`04-sandbox-boundaries.md`, with columns for layer, expected/documented behavior,
observed result, evidence basis, ownership, source/date, limitation, and follow-up.

**Validation:** Test matrix completeness and report provenance. Validate any
observations against exact expected probe outcomes. If shared context is
observed, report the limitation explicitly; do not report user isolation as
passing just because hosted State Store isolation works.

**Exit gate:** Every requested boundary is assessed with evidence and gaps.
Documentation/architectural evidence may support this assessment when live
access is unavailable, but all unperformed probes remain visibly untested.

## Step 5 - Assess continuity, rehydration, and customer-network storage

**Acceptance criterion:** Assess state continuity across requests, rehydration
after session recreation, and whether state storage can reside in the customer's
network.

**VS Code Task:** `Agents: Story 1 - Assess state continuity`

### Implementation

1. Define and report distinct state layers: Responses conversation/history,
   hosted-agent session, managed interpreter session/variables/files,
   small workflow/checkpoint metadata, and durable input/output artifacts.
   Track their real IDs where exposed; never assume a conversation or hosted
   session identifier is the interpreter's identifier.
2. Make a first request that loads a synthetic dataset and creates a variable
   and file marker. Make a second request in the intended same context and
   inspect actual tool-visible state, not an answer inferred from chat history.
3. Persist a small versioned checkpoint of input reference/checksum, deterministic
   transformations, validated aggregates, artifact references, and known bindings.
   Preserve ETag and ownership checks; keep binaries outside the state document.
4. Recreate only a run-owned interpreter session using a supported API or
   documented expiry mechanism. Verify a changed interpreter identifier or
   equivalent service evidence. If the toolbox offers no safe recreation
   control, record this test as blocked/documented; a new conversation alone
   is not proof of a new interpreter container.
5. Inspect continuity before restoring anything. Then explicitly rehydrate by
   loading the authorized checkpoint, re-uploading/reloading the original
   dataset, and replaying deterministic transformations. Compare checksum and
   aggregates with Step 2. Record which state survived, was lost, or was rebuilt.
   Label this as custom rehydration, not restored Python RAM or automatic recovery.
6. Treat rehydration as an explicit operation that creates a fresh verified
   execution binding after authorizing the original checkpoint. Do not silently
   remint mappings or relax the existing same-session continuation contract.
   Test missing, corrupted, unauthorized, stale, and unavailable checkpoints.
7. Assess customer-network storage by layer. Verify whether the chosen managed
   state service supports customer-network placement; do not assume it does.
   Separately assess a custom private Blob/database checkpoint/artifact store,
   including managed identity, private endpoint/DNS, host egress, and retention.
8. For the custom option, describe the mediated path: a network-authorized host
   retrieves approved data from the customer store and uploads it to managed
   Code Interpreter. This does not put the managed sandbox inside the customer
   network or prove that data never leaves it. Direct sandbox access to private
   endpoints must not be assumed given the documented egress restriction.
9. If authorized private infrastructure exists, run a tiny read/write and
   retrieval test through that path. Otherwise give an architectural assessment
   and specific follow-up. The repository's current authenticated public/NSP
   Blob setup is not evidence of private-endpoint connectivity.

**Expected output:** Request-by-request continuity table, before/after
rehydration comparisons, lost/restored-state inventory, and a per-layer
customer-network feasibility matrix in `05-state-continuity.md`.

**Validation:** Unit-test checkpoint schemas, checksums, ownership, conflicts,
missing mappings, stale references, and explicit rebind behavior. Hosted
evidence must distinguish conversation continuity from tool state. Verify
restored outputs against the deterministic fixture, not model assurances.

**Exit gate:** Continuity and rehydration have tested results or explicit
validation gaps, and each storage layer has a scoped placement assessment.
Do not claim private-network residency or transparent recovery without evidence.

## Planned change surfaces and regression checks

| Surface | Planned changes |
|---|---|
| [runtime.py](../../src/backend/app/runtime.py), [settings.py](../../src/backend/app/core/settings.py), [azure.yaml](../../azure.yaml) | Data Analysis-only managed toolbox configuration, lifecycle, and version metadata |
| [hosting.py](../../src/backend/app/hosting.py), [delegation.py](../../src/backend/app/services/delegation.py), [workflows.py](../../src/backend/app/services/workflows.py) | Typed analysis results/artifact references and explicit checkpoint/rehydration handling; preserve existing text/workflow compatibility and fail-closed bindings |
| [foundry_state.py](../../src/backend/app/services/foundry_state.py) | Reuse existing storage guardrails; change only if the verified checkpoint contract requires it |
| [reporting.py](../../scripts/common/reporting.py), [trigger_agent.py](../../scripts/run_scenario/trigger_agent.py) | Opt-in sanitized Markdown rendering/persistence and preservation of visible output metadata, with existing smoke behavior retained |
| [scripts/run_scenario](../../scripts/run_scenario) | Proposed single five-step scenario module, deterministic fixture generation, bounded execution, evidence collection, and report index |
| [tasks.json](../../.vscode/tasks.json) | Five explicit story tasks, shared inputs, dedicated panels, no implicit deployment dependencies |
| [tests/unit](../../tests/unit) | Focused runtime, artifact, reporting, state, bounded-runner, and editor-task tests; preserve current text-only roles and existing task contracts |
| [pyproject.toml](../../pyproject.toml), [uv.lock](../../uv.lock) | Only if verified SDK compatibility or required application functionality needs dependency changes; keep the approved feed and locked Python baseline |
| [README](../../README.md), [tasks-detailed.md](../tasks-detailed.md), [deployment.md](../deployment.md), [ADR-0001](../adr/0001-hosted-agents-poc.md) | Task run order, output interpretation, managed-sandbox decision, observed constraints, and explicit evidence boundaries |

Prefer a small analysis/artifact service helper only if runtime responsibilities
warrant it. Do not duplicate client, auth, redaction, or state abstractions.

During implementation:

- Run focused unit tests for the touched runtime, reporter, task, transport,
  and state surfaces; then the repository's lint/format/type checks.
- Verify the exact five task labels, input wiring, no automatic paid dependencies,
  Markdown escaping/redaction, valid local artifact links, and failure reports.
- Exercise the new tasks individually against the authorized deployment.
  Reuse verified versions; ordinary demonstration runs must not redeploy.
- Inspect a generated report in VS Code Markdown Preview: readable tables,
  relative CSV links, and a rendered PNG. JSONL alone does not satisfy the
  readable-Markdown requirement.
- Keep unit-test results separate from hosted evidence. No tests or cloud
  demonstrations were run as part of writing this plan.

## Decisions required before implementation/live execution

| Decision | Proposed default / required confirmation |
|---|---|
| Managed sandbox baseline | Adopt the story's managed toolbox integration; record the scoped ADR change |
| Shared-context limitation | Confirm an authorized synthetic-only POC scope; do not promise multi-tenant sandbox isolation |
| Target and spending | Reuse the approved existing project/service; confirm before deployment/toolbox mutations or paid calls |
| Dataset profile | Agree on the proposed 1k/10k/100k rows, request/size bounds, and any latency target before Step 3 |
| Artifact delivery | Direct authenticated user download first; Supervisor path only after its separate guard issue is resolved |
| Private storage | Assessment first; live private-endpoint validation only with authorized infrastructure and connectivity |
| Evidence gaps | POC owner reviews documentation-only findings and remaining follow-ups; never silently treats them as live proof |

## Completion checklist

- [ ] Exactly five independently selectable VS Code demonstration Tasks exist.
- [ ] Every acceptance criterion has its own Markdown report and index entry.
- [ ] Summaries, verified CSV outputs, and plots are visible/retrievable.
- [ ] Configuration and dependency/API versions, reproduction steps, sanitized
      evidence, ownership classifications, limits, failures, and follow-ups are recorded.
- [ ] Host resources and managed sandbox resources are never conflated.
- [ ] The bounded dataset assessment gives an explicit, scoped viability conclusion.
- [ ] Shared-context limitations, state lifecycle, and network/storage boundaries
      are called out without unsupported guarantees.
- [ ] Existing smoke, role selection, authorization, state-binding, and task
      behavior remain covered by regression checks.
- [ ] Run-owned temporary remote resources have a documented cleanup/retention
      outcome; durable evidence is preserved and secrets remain excluded.
- [ ] Any partial/unverified criteria are visible to the POC owner before closing #1.

## References

- [Story #1](https://github.com/wxifaras/hosted-agents-poc/issues/1)
- [Code Interpreter for Microsoft Foundry agents][code-interpreter]
- [Dynamic sessions in Azure Container Apps](https://learn.microsoft.com/azure/container-apps/sessions)
- [Dynamic session security and usage](https://learn.microsoft.com/azure/container-apps/sessions-usage#security)
- [Manage hosted-agent sessions](https://learn.microsoft.com/azure/foundry/agents/how-to/manage-hosted-sessions)
- [MAF hosted-agent toolbox example](https://github.com/microsoft-foundry/foundry-samples/tree/main/samples/python/hosted-agents/agent-framework/responses/04-foundry-toolbox)

Recheck preview documentation and installed SDK signatures during implementation;
documentation is not a substitute for configuration-specific POC evidence.

[code-interpreter]: https://learn.microsoft.com/azure/foundry/agents/how-to/tools/code-interpreter
