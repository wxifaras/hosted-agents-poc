# Implementation plan: Story 1 - Hosted Data Analysis Agent with managed Code Interpreter

- **Story:** [#1](https://github.com/wxifaras/hosted-agents-poc/issues/1)
- **Plan date:** 2026-10-22
- **Status:** Step 1 implemented and verified live on 2026-10-22: verdict **Met**, with the existing `data-analysis-agent` now at v2 and Supervisor and Research unchanged at v1. Steps 2-5 are planned.
- **Target:** azd environment `hosted-agents-poc-dev`, North Central US, model `gpt-5-mini`
- **Demonstration format:** each acceptance criterion is a separate VS Code Task
  (**Terminal > Run Task**). Each Task renders a readable Markdown report in its
  terminal and saves the same report for VS Code Markdown Preview.

## 1. Goal and guiding rules

Evaluate the existing Data Analysis Hosted Agent with Microsoft-managed
Code Interpreter, using synthetic tabular data. Produce Markdown evidence for
each acceptance criterion: what was tested, what is documented, and what
remains unverified.

| Rule | How this plan applies it |
|---|---|
| Reuse existing services and agents | Reuse the existing project, model deployment, ACR, storage, monitoring, `data-analysis-agent` service, Supervisor/Research agents, reporting helpers, and Task conventions. |
| Create or deploy only when unavoidable | Reuse the **already-deployed** `data-analysis-agent` (same agent, endpoint, identity, and RBAC). AC1 still needs two additions: one Code Interpreter toolbox, and one new **version** of that existing agent (not a new agent). Both are created **once**, only by Task 1, and are reused afterward. Supervisor and Research are not redeployed. Tasks 2-5 never deploy, provision, upload new toolbox inputs, or grant roles. |
| Keep layers separate | Never treat hosted-agent container settings (0.5 CPU, 1 GiB, 120 s idle) as Code Interpreter sandbox limits. |
| Separate evidence types | Every finding is labeled `Tested`, `Documented`, `Architectural assessment`, or `Unverified`. Untested does not mean unsupported. |
| Stay in scope | Use synthetic data and sequential requests only. Do not run load, concurrency, stress, OOM, or escape tests. Process-loss recovery (#7) and long-term memory (#8) are out of scope. |

## 2. Confirmed decisions

| Decision | Choice |
|---|---|
| Target environment | `hosted-agents-poc-dev` (default azd env; all three agents `active` at v1; no toolboxes yet) |
| Reuse of deployed agents | **Option A:** reuse the existing `data-analysis-agent` and add only a new version (v2) of it. Supervisor and Research v1 stay as they are. See [Step 1](#step-1---enable-code-interpreter-on-the-existing-data-analysis-agent). |
| New resources allowed | One `code_interpreter` toolbox + one new version of the existing `data-analysis-agent`, created once by Task 1 |
| AC3 dataset sizes | **1,000 / 10,000 / 100,000 rows** with the same 8-column schema |
| Plan location | This file |

## 3. Current baseline (verified 2026-10-22)

### Environment

| Item | Observed | Basis |
|---|---|---|
| Project endpoint | `https://aif-poc-k46bujqf6iic2.services.ai.azure.com/api/projects/hosted-agents-poc-dev` | Tested: `azd env get-values` |
| `data-analysis-agent` | Version 1, status `active`, no tools | Tested: `azd ai agent show` |
| Toolboxes in project | None | Tested: `azd ai toolbox list` |
| Region supports Code Interpreter | North Central US: yes | Documented: [tool support by region][regions] |
| Model supports Code Interpreter and MCP | `gpt-5-mini`: yes / yes | Documented: [tool support by model][regions] |
| Locked SDKs | `agent_framework.foundry.FoundryToolbox` and `azure.ai.projects.models.CodeInterpreterToolboxTool` / `AutoCodeInterpreterToolParam` are available in the pinned versions | Tested: import/signature check in `.venv` |

No dependency changes to [pyproject.toml](../../pyproject.toml) or [uv.lock](../../uv.lock) are expected.

### Code

| Area | Current state | Gap for Story 1 |
|---|---|---|
| [runtime.py](../../src/backend/app/runtime.py) | `FoundryChatClient` with no tools. Data Analysis instructions forbid claiming execution. | Attach a toolbox to Data Analysis only and update its instructions when the toolbox is configured. |
| [hosting.py](../../src/backend/app/hosting.py) | Plain chat returns `response.text` only. | Preserve generated-file references (container ID, file ID, name) so the client can download them. |
| [settings.py](../../src/backend/app/core/settings.py) | No toolbox setting | Add an optional `toolbox_endpoint` (env `TOOLBOX_ENDPOINT`). |
| [azure.yaml](../../azure.yaml) | Data Analysis env has no toolbox variable | Map `TOOLBOX_ENDPOINT: ${DATA_ANALYSIS_TOOLBOX_ENDPOINT}` for the Data Analysis service only. |
| [reporting.py](../../scripts/common/reporting.py) | Rich console plus JSONL evidence | Add opt-in Markdown report output. Keep the existing JSONL, redaction, and exit codes. |
| [hosted_smoke.py](../../scripts/run_scenario/hosted_smoke.py), [trigger_agent.py](../../scripts/run_scenario/trigger_agent.py) | Hosted invocation, routing check, response summary | Reuse their invocation, `@latest` routing, and summary helpers. |
| [tasks.json](../../.vscode/tasks.json) | `Agents:` / `Access:` tasks using `uv run --frozen --no-sync --group validation` | Add five Story 1 tasks with the same pattern. |

### Lessons from the earlier Step 1 attempts

Evidence in `artifacts\story-1\` (git-ignored) shows four runs against
`ai-engineering-default`. All failed before managed execution: first an ACR
push 403, then an image-pull 403 on a private registry. That environment is
**not** used here. The scripts that produced those runs are no longer in the
tree. Their stale `__pycache__` files are not reused.

Carry forward one correction. Toolbox file uploads must use the project GUID
from `properties.amlWorkspace.internalId`, not `properties.internalId`. The
earlier run used `properties.internalId`, which the current
[Code Interpreter docs][ci-docs] say the toolbox container rejects.

## 4. Documented platform facts to validate

These come from the [Code Interpreter documentation][ci-docs] (consulted
2026-10-22). They are hypotheses for the POC to confirm, not POC observations.

| Topic | Documented behavior |
|---|---|
| Backing | Microsoft-managed sandbox using [Azure Container Apps dynamic sessions][aca-ci]; each session is isolated by a Hyper-V boundary |
| Region | Sandbox runs in the Foundry project's region |
| Lifetime | Up to 1 hour active, 30-minute idle timeout |
| Network | No outbound network requests; does not inherit the agent subnet configuration |
| Toolbox + hosted agent isolation | **"User isolation isn't supported. All users in the same project share the same container context."** |
| File input | Upload to the **resource-level** `/openai/v1/files` with `x-aml-project-id`; reference it in toolbox `container.file_ids`; mounted at `/mnt/data/{file-id}-{name}` |
| File output | `container_file_citation` (container ID + file ID); download from `/openai/v1/containers/{container_id}/files/{file_id}/content` |
| Packages | Fixed common data-science set; custom packages need [Custom Code Interpreter][custom-ci] |
| Billing | Code Interpreter sessions are charged in addition to tokens |
| Toolbox versions | Immutable; built-in tools cannot be added to an existing toolbox |

## 5. Shared design for all five Tasks

### Agent reuse contract (applies to every step)

Every step runs against the **existing** hosted agents in `hosted-agents-poc-dev`.
No step creates, deletes, or renames an agent.

| Existing agent | Version used | Used by | How |
|---|---|---|---|
| `data-analysis-agent` | v1 today. Step 1 moves the same agent to its Code Interpreter version (v2) once; Steps 2-5 use that version as-is. | Steps 1-5 | Invoked by name through its existing Responses endpoint (`@latest` routing) |
| `supervisor-agent` | v1, unchanged | Step 2 (delivery-path check only) | Its existing `POC_WORKFLOW` command, sent as-is; no code or version change |
| `research-agent` | v1, unchanged | Not invoked directly (reached only through the Supervisor's existing workflow) | None |

Every Task enforces this with a shared **reuse guard** in the scenario module:

1. **At start:** snapshot all three agents (name, active version, status,
   instance principal ID, `@latest` routing, toolbox endpoint presence).
   - Steps 2-5: compare the snapshot with the Step 1 baseline recorded in
     `artifacts\story-1\agents-baseline.json`.
   - If an agent is missing or inactive, or Data Analysis lacks the Story 1
     toolbox, report `BLOCKED: run Task 1` and exit.
   - If an agent version or identity differs unexpectedly, report `FAIL` and
     exit.
2. **During the run:** the scenario module never calls `azd deploy`,
   `azd provision`, agent create/update/delete APIs, or toolbox creation in
   Steps 2-5. These code paths exist only behind Step 1's `enable-if-missing`
   mode, and a unit test asserts that.
3. **At end:** take the snapshot again and assert it is identical to the start
   snapshot. (Step 1 allows only its one Data Analysis version change.) Print
   an **Agents reused** table at the top of every Markdown report.

Conversations, hosted-agent sessions, and Code Interpreter sandbox sessions
are per-request runtime objects created by the platform. They are not new
agents or deployments.

### One scenario module, five steps

Add `scripts/run_scenario/story1_data_analysis.py` with
`--step {1,2,3,4,5}`, `--environment`, and the existing output arguments.
It reuses:

- `ValidationRun` / `sanitize` for JSONL evidence and exit codes (`0` pass, `1` fail, `2` blocked).
- `native_json("azd", ["env", "get-values", ...])` for the project endpoint, as in [hosted_smoke.py](../../scripts/run_scenario/hosted_smoke.py).
- `AIProjectClient.get_openai_client(agent_name="data-analysis-agent")` for hosted Responses calls, including the `@latest` routing and active-status checks.
- `response_summary` / `http_details` for sanitized request and response details.

Add `scripts/common/synthetic_data.py`. It is a deterministic, stdlib-only
generator with a fixed seed, plus an independent expected-results calculator
that uses integer cents for exact comparisons.

Schema (8 columns): `row_id, order_date, region, category, units,
unit_price_cents, revenue_cents, status`. Each file contains known nulls and
categories.

### Readable Markdown output

Extend [reporting.py](../../scripts/common/reporting.py) with an opt-in
`MarkdownReport` that runs alongside `ValidationRun`:

- Writes `artifacts\story-1\<run-folder>\0N-<step>.md` incrementally, so a
  partial run still produces a readable report.
- Step 1 run folders use
  `<UTC-date>_<UTC-time>Z-step-1-code-interpreter-<mode>-<8-character-id>`,
  for example `2026-10-23_12-04-08Z-step-1-code-interpreter-verify-a1b2c3d4`.
  The mode is `verify` or `enable-if-missing`; time uses Windows-safe hyphens.
  The full run ID remains in the manifest and JSONL evidence. Existing folders
  and links are preserved.
- Renders the same Markdown in the Task terminal with `rich.markdown.Markdown`.
- Updates `artifacts\story-1\index.md` with a link, timestamp, and verdict for
  each step run. Runs never overwrite each other.
- Prints the report path at the end. Open it with **Open Preview**
  (`Ctrl+Shift+V`) to see tables, CSV links, and embedded PNG charts.

Every report uses the same layout:

1. **Header:** criterion text, Task label, run ID, UTC time, environment, toolbox version, and an **Agents reused** table (start/end snapshot of all three existing agents).
2. **Configuration:** SDK, CLI, and API versions; host settings and sandbox settings in separate tables.
3. **Reproduction:** the exact Task, inputs, dataset seed, row and byte counts, and SHA-256 checksums.
4. **Results:** expected vs. observed table with `PASS` / `FAIL` / `BLOCKED` / `SKIP` per check.
5. **Findings:** columns for capability, evidence basis, and ownership
   (`Foundry-managed`, `Agent Framework / SDK-managed`, `Custom implementation (Dev Team)`, or `Not supported`).
6. **Verdict:** `Met`, `Partially met`, `Not met`, or `Not evaluated`, plus limitations and follow-ups.

Reports exclude credentials, tokens, signed URL queries, call-context headers,
and hidden reasoning.

### Execution-proof oracle

Fluent model text is never accepted as proof of execution. Each step asks the
sandbox to compute values the model cannot guess, such as the SHA-256 of a
mounted file, exact integer-cent aggregates, or `/proc` readings. The Task
then compares those values with local, independently computed values.

### Task conventions

Each Task:

- Uses `"type": "process"`, `"command": "uv"`, and
  `run --frozen --no-sync --group validation python -m scripts.run_scenario.story1_data_analysis --step N --environment ${input:infraEnvironment}`.
- Uses `"presentation": { "reveal": "always", "panel": "dedicated" }`.
- Has no `dependsOn` chain to deploy or grant tasks.
- Reruns with **no automatic inference retries**.
- Checks prerequisites at startup with the reuse guard (see the
  [agent reuse contract](#agent-reuse-contract-applies-to-every-step)).
  Steps 2-5 never deploy; if the existing agents aren't ready, they report
  `BLOCKED: run Task 1` and exit.

## 6. Steps (one per acceptance criterion)

### Step 1 - Enable Code Interpreter on the existing Data Analysis agent

> **AC1 (verbatim from #1):** Deploy a Foundry Hosted Data Analysis Agent using Microsoft-managed
> Code Interpreter, backed by Azure Container Apps dynamic sessions.

**How this POC satisfies AC1 by reuse:** no new agent is deployed. The
**already-deployed** hosted `data-analysis-agent` in `hosted-agents-poc-dev`
is the "Foundry Hosted Data Analysis Agent". Step 1 enables managed
Code Interpreter on that existing agent and proves it runs code in the
Microsoft-managed sandbox. The agent's name, endpoint, identity, RBAC, and
routing all stay the same. Supervisor and Research are not touched.

**Task:** `Agents: Story 1 - 1 Enable Code Interpreter on existing Data Analysis agent`
(input `story1Step1Mode`: `verify` (default; no changes) or `enable-if-missing`)

#### Reusing the deployed agents (option A)

**Why the deployed v1 can't be used as it is.** This was verified read-only on 2026-10-22:

- The project contains only `supervisor-agent`, `research-agent`, and `data-analysis-agent`. All three are v1, `active`, and have no tools.
- A hosted agent definition has no `tools` field. Its only fields are `container_configuration`, `code_configuration`, `cpu`, `memory`, `environment_variables`, `protocol_versions`, `session_configuration`, and `telemetry_config`.
- Tools must therefore be wired in the agent's code.
- The v1 image has no toolbox code and no `TOOLBOX_ENDPOINT` variable.
- Versions are immutable, so the smallest possible change is one new version of the **same** agent.

The existing toolbox `story1-interpreter-7b968480191d` cannot be reused either. It lives in a different project (`ai-engineering-default`), and its uploaded file IDs belong to that project.

**What is reused and what changes:**

| Item | Treatment |
|---|---|
| `data-analysis-agent` (name, endpoint, Responses route, `@latest` routing) | **Reused.** v2 becomes `@latest`, so callers and the Supervisor's by-name reference need no change. |
| Agent instance identity and its RBAC | **Reused.** The Task records the principal ID before and after the deployment and fails if it changes. If the toolbox call returns 403, the Task reports `BLOCKED` and points to the existing **Access: Apply hosted agent permissions** task. It does not grant roles automatically. |
| `supervisor-agent`, `research-agent` | **Untouched** (stay v1). Do **not** use **Agents: Deploy all hosted agents**: it runs `azd deploy --all` and would create new versions of all three agents. |
| Project, model deployment, ACR, storage, monitoring | **Reused**; no `azd provision`. |
| Data Analysis container image | Rebuilt once for v2, because the code changes. CPU, memory, and session settings stay as they are in [azure.yaml](../../azure.yaml). |
| Data Analysis v1 | Kept, so rollback is possible by routing back to v1 (manual and documented; never automatic). |

**Code changes (once):**

1. `settings.py`: add optional `toolbox_endpoint: HttpUrl | None` (env `TOOLBOX_ENDPOINT`). An empty value maps to `None`. Do not use a `FOUNDRY_`-prefixed name because the platform reserves it.
2. `runtime.py`: for `data_analysis` only, when `toolbox_endpoint` is set, add `FoundryToolbox(credential, url=...)` to the agent's tools, following the [MAF `04-foundry-toolbox` sample][toolbox-sample] lifecycle and credential pattern. Update Data Analysis instructions:
   - Use Code Interpreter for all computation.
   - Read inputs from `/mnt/data`.
   - Save requested outputs as files.
   - Never invent numbers.

   Supervisor and Research stay unchanged. Without the setting, Data Analysis behaves exactly as v1. There is no local `exec` or subprocess fallback.
3. `hosting.py`: in the plain-chat path, collect generated-file references from the MAF response contents and Code Interpreter tool results, then return them with the answer. Preferred: `output_text` annotations. Fallback: a fenced `poc-artifacts-v1` JSON block. Step 1's raw probe decides which shape is available. Workflow, identity, and context-probe paths stay unchanged.
4. `azure.yaml`: add `TOOLBOX_ENDPOINT: ${DATA_ANALYSIS_TOOLBOX_ENDPOINT}` to `data-analysis-agent` only, and update its description.

**Task behavior:**

For the current operator procedure, execution locations, and complete local
and remote output inventory, see the
[Step 1 task guide](../tasks-detailed.md#agents-story-1---1-enable-code-interpreter-on-existing-data-analysis-agent).
The task runs a local validation client against a deployed hosted agent and
managed Code Interpreter; it is not an entirely sandbox-hosted test.
The [cloud-only execution proposal](../tasks-detailed.md#cloud-only-execution-proposed-not-implemented)
is not implemented and is not a mode of this task.

| # | Action | Mutates? |
|---|---|---|
| 1 | Read the azd env, region, model, SDK and CLI versions, and operator identity. Snapshot all three deployed agents: version, status, routing, `TOOLBOX_ENDPOINT` presence, and the Data Analysis instance principal ID. | No |
| 2 | Generate the three datasets locally (1k, 10k, 100k) and record rows, bytes, and SHA-256 | Local only |
| 3 | Look for toolbox `story1-data-analysis-ci`. If its description manifest matches the dataset checksums, **reuse** it. | No |
| 4 | Otherwise, with `enable-if-missing` only: upload the three CSVs to the **project-scoped** Files API (see [Step 1 as implemented](#step-1-as-implemented-2026-10-22)) and create **one** toolbox version (`code_interpreter`, `container: {type: auto, file_ids: [...]}`) with the checksum manifest in its metadata | Yes, once |
| 5 | Raw MCP probe as operator: `tools/list`, then `tools/call` to hash all three mounted CSVs, followed by a separate call generating `story1-probe.png`. List the generated file through the container files API, download its bytes into local memory, and check the PNG signature. Record the result shape; no local PNG is saved. | Sandbox execution; local checks/evidence |
| 6 | **Reuse check.** If the active `data-analysis-agent` version already carries this toolbox endpoint, skip to row 7 with no deployment; every rerun takes this path. Otherwise, in `enable-if-missing` mode only: `azd env set DATA_ANALYSIS_TOOLBOX_ENDPOINT ...`, then `azd deploy data-analysis-agent -e hosted-agents-poc-dev`. This updates the existing agent to a new version, for that one service only (no new agent, no `provision`, no `--all`). In `verify` mode, report `BLOCKED: run with enable-if-missing` instead. | Yes, once |
| 7 | Invoke the existing hosted `data-analysis-agent` endpoint: "Compute the SHA-256 of the 1k file and print Python's version." Compare the hash with the local value. | Paid inference |
| 8 | Take the same snapshot again and compare it with row 1. Expected: Data Analysis is on its new version and `active`, with an unchanged principal ID; Supervisor and Research are unchanged at v1. Any other difference is `FAIL`. If the call fails with 403, report `BLOCKED` and direct the operator to the existing **Access: Apply hosted agent permissions** task. Do not grant roles automatically. On success, write the snapshot to `artifacts\story-1\agents-baseline.json` for the Steps 2-5 reuse guard. | Local only |

**Markdown report `01-existing-agent-code-interpreter.md`:**

- Agent reuse table: a before/after snapshot of all three agents (version, status, principal ID, routing).
- Configuration tables for the host and the toolbox/sandbox.
- Dataset manifest.
- Toolbox reuse or creation record.
- Raw MCP probe result.
- Hosted execution proof (hash match).
- ACA dynamic-session backing labeled `Documented`, since the customer cannot inspect that resource directly.
- Ownership classification.

**Exit gate:**

- **Met:** the existing `data-analysis-agent` endpoint returns a sandbox-computed hash that matches the local hash. Its identity is unchanged, and Supervisor and Research are untouched.
- **Not met:** the deployment succeeds, but tool listing or text-only smoke tests are the only evidence.

**Tests:**

- The toolbox is registered only for `data_analysis` and only when configured.
- An empty or invalid endpoint is handled.
- Other roles are unchanged.
- Artifact-reference extraction, including when no files are returned.
- Dataset generator determinism.

#### Step 1 as implemented (2026-10-22)

**Result:** the `enable-if-missing` run created toolbox `story1-data-analysis-ci`
v1 and moved the existing `data-analysis-agent` from v1 to v2, in about
2 minutes, deploying that service only. The checks passed:

- The sandbox SHA-256 proof.
- The container reference returned by the agent.
- The reuse guard.

A following `verify` run reused everything without deploying (**Met**), and
**Agents: Smoke test all hosted agents** passed for all three agents.

**Code:**

- [story1_data_analysis.py](../../scripts/run_scenario/story1_data_analysis.py)
- [synthetic_data.py](../../scripts/common/synthetic_data.py)
- [code_interpreter.py](../../src/backend/app/services/code_interpreter.py)
- Changes to `settings.py`, `runtime.py`, `hosting.py`, `reporting.py`, `azure.yaml`, and `tasks.json`
- [test_story1_step1.py](../../tests/unit/test_story1_step1.py)

**Differences from the plan, found during implementation:**

| Planned | Observed | Change made |
|---|---|---|
| Upload to the resource-level Files API with `x-aml-project-id` from `amlWorkspace.internalId` | This project has no `amlWorkspace`. Resource-level uploads (no header, or `internalId` in dashed or no-dash form) failed toolbox ownership verification. Project-scoped uploads worked. | Upload project-scoped and record the finding in the report |
| Generated files referenced by `container_file_citation` | Citations appear inconsistently. `structuredContent.container_id` is always present, and the container files API lists generated files (`source=assistant`) separately from inputs (`source=user`). | The agent returns a `poc-artifacts-v1` block of container IDs, read only from tool results. Clients list and download files through the authenticated container files API. |
| MAF default MCP result handling | The default `structured_first` would show the model only `{"container_id": ...}`, hiding printed output. | `FoundryToolbox(..., tool_result_content="both")` |
| Interpreter is a stateful session | Each `tools/call` ran in a new container. Container reuse (`container`) is rejected when the toolbox preloads `file_ids`. | Instructions require one execution per request. Step 5 must test this behavior as a key continuity finding. |
| Documented 30-minute idle timeout | Container API reports `expires_after: last_active_at + 20 min` and a `2g` default memory limit. The tool schema offers `memory_limit` (1g/4g/16g/64g) and `network_policy` (disabled/allowlist). | Carried into Steps 3-4 for testing |

### Step 2 - Analysis operations and output delivery

> **AC2:** Demonstrate filtering, grouping, summarization, and visualization
> using synthetic tabular data. Show how summaries, files, and visuals reach the
> supervisor or user.

**Task:** `Agents: Story 1 - 2 Analyze data and deliver outputs`. It reuses the
Step 1 toolbox and the 1k dataset, with no uploads or deployments.

**Agents reused:**
- Existing `data-analysis-agent` (Step 1 version) for the analysis.
- Existing `supervisor-agent` v1 for the Supervisor delivery-path check.
- No agent is created or changed.

1. Send one request to hosted Data Analysis that asks the sandbox to:
   - **Filter:** `status == "completed"` and `region == "West"`.
   - **Group:** by `category`.
   - **Summarize:** row count, total units, and total `revenue_cents` per category, plus the overall total.
   - **Visualize:** a bar chart of revenue by category.
   - Save `filtered.csv`, `by_category.csv`, and `revenue_by_category.png`, and print the aggregates as JSON.
2. Parse the returned file references. Download each file through the authenticated containers API, with these limits:
   - Safe file names, size limits, and expected MIME types.
   - Never follow arbitrary model-provided URLs.
3. Validate the results against the independent local calculator:
   - Counts and integer-cent sums must match exactly.
   - Downloaded CSVs must parse and match.
   - The PNG signature and `IHDR` header must decode, using stdlib only.
4. Delivery paths:

| Path | Planned evidence |
|---|---|
| Summary text -> user | Agent answer shown in the report |
| Structured aggregates -> user | JSON parsed and checked |
| Files and chart -> user | Hosted response -> file references -> authenticated download by the Task client -> CSV links and embedded PNG in the report |
| Supervisor -> Data Analysis | Reuse the existing `supervisor-agent` v1 by sending its existing `POC_WORKFLOW` create command once, with the same analysis request. It delegates by name to the existing Research and Data Analysis agents. Expected outcome: `BLOCKED` at the existing nested-user-ID guard ([ADR](../adr/0001-hosted-agents-poc.md#authorization-contract-and-proposed-alignment)). Record the observed result and do not weaken the guard or change the Supervisor. Coordinate the pattern with #3 and #4. |

**Markdown report `02-analysis-and-outputs.md`:**

- Sample of filtered rows.
- Table of expected vs. observed group totals.
- The agent's summary.
- Links to `outputs\filtered.csv` and `outputs\by_category.csv`.
- Inline `![Revenue by category](outputs/revenue_by_category.png)`.
- Delivery-path table.

**Exit gate:**

- **Met:** all four operations are numerically correct, and the user can retrieve the files and see the chart.
- **Partially met:** the Supervisor path is reported as blocked, but direct user delivery satisfies "supervisor **or** user".

**Tests:**

- Expected-aggregate fixture.
- Download validation: missing, malformed, oversized, and traversal-style names.
- PNG validation.
- Markdown escaping and relative links.

### Step 3 - Progressively larger datasets

> **AC3:** Test a small, agreed set of progressively larger datasets. Record
> observed limits and failures, and conclude whether the tested workloads are
> viable in the evaluated configuration.

**Task:** `Agents: Story 1 - 3 Evaluate dataset sizes`. It reuses the 1k, 10k,
and 100k files already mounted by the Step 1 toolbox.

**Agents reused:** the existing `data-analysis-agent` (Step 1 version) only.
No agent is created or changed, and nothing is uploaded or deployed.

1. For each size in order, send one request with the same Step 2 workload in a fresh conversation. Use a bounded client timeout (proposed 300 s; recorded). Make one attempt per size and do not retry automatically.
2. The sandbox code also prints in-sandbox measurements:
   - Wall time for load, filter/group, and plot.
   - Peak RSS from `resource.getrusage`.
   - `os.cpu_count()` and `MemTotal` from `/proc/meminfo`.
   - Free disk space under `/mnt/data`.

   These are sandbox observations and are reported separately from the host container's 0.5 CPU / 1 GiB.
3. Record:
   - End-to-end latency, tool status, input bytes, and output file sizes.
   - Correctness against local aggregates.
   - Token usage.
   - Error category.
4. Stop escalating at the first timeout, resource error, or quota response. Keep the evidence and mark the remaining sizes `SKIP (not run after failure)`. Do not search for a crash limit.
5. Conclusion: functional viability for the tested inputs only. Timings are reported, but no SLA is invented, because none was agreed.

**Markdown report `03-capacity.md`:**

- One row per size with these columns: rows, bytes, correct?, sandbox time, end-to-end time, peak RSS, CPU count, memory total, tokens, files, outcome, and failure detail.
- Largest successful size.
- First failure, if any.
- Scoped viability statement.

**Exit gate:** all three sizes have results or an explicit reason for not
running, and a scoped conclusion is written. Passing all sizes shows the tested
envelope, not the platform maximum.

**Tests:**

- Sizes are bounded to the agreed list.
- Stop-on-failure.
- No concurrency and no retries.
- Metric units and parsing.
- A partial report is still written on failure.

### Step 4 - Sandbox security, lifetime, network, configuration

> **AC4:** Document sandbox security boundaries, session lifetime, network
> restrictions, and available configuration options.

**Task:** `Agents: Story 1 - 4 Assess sandbox boundaries`. It runs targeted,
benign probes through the existing agent and toolbox, and produces the
documentation matrix.

**Agents reused:**
- The existing `data-analysis-agent` (Step 1 version).
- The optional second-user check calls that same agent with the existing auth profiles.
- No agent is created or changed.

| Probe | Expected (documented) | Method |
|---|---|---|
| Runtime and environment | Fixed Python and package set | Print Python version, key package versions (`pandas`, `numpy`, `matplotlib`), user ID, CPU, memory, and disk |
| Outbound internet | Blocked | One 5 s HTTPS request to a benign Microsoft URL; record the error class (DNS vs. connect vs. policy) |
| Customer resource reachability | Blocked | One 5 s request to the POC storage account's blob endpoint (no credentials) |
| Shared context | Shared across conversations and users in the project | Write the synthetic marker `/mnt/data/story1-marker-<run>.txt` in conversation A, then read it in conversation B. Optional: a second user, if profiles from the existing **Access: Create isolated user auth profiles** task already exist; otherwise label it `Documented`. |
| Session identity and lifetime | 1 h active / 30 min idle | Record a sandbox identity fingerprint (hashed hostname + `/proc/sys/kernel/random/boot_id` + uptime) in both requests. The actual idle expiry is tested in Step 5. |
| Configuration surface | Toolbox `container: {type: auto, file_ids}` only | Read back the toolbox definition and compare it with the docs. List host-side settings (`container.resources`, `sessionConfiguration.idleTimeoutSeconds`) separately. Note [Custom Code Interpreter][custom-ci] as the documented alternative, without provisioning it. |

**Markdown report `04-sandbox-boundaries.md`:**

- A matrix with these columns: layer (hosted container / toolbox / sandbox), behavior, documented value, observed value, evidence basis, ownership, source and date, limitation, follow-up.
- A highlighted limitation: there is no per-user sandbox isolation with toolbox + hosted agent, so the setup is unsuitable for sensitive multi-tenant data without a custom design.

**Exit gate:** each requested area (security boundary, lifetime, network,
configuration) has an observed or documented entry and a stated gap. No
network scanning, escape attempts, or secrets.

**Tests:** matrix completeness, probe-result classification, and marker cleanup.

### Step 5 - State continuity, rehydration, and customer-network storage

> **AC5:** Assess state continuity across requests, rehydration after session
> recreation, and whether state storage can reside in the customer's network.

**Task:** `Agents: Story 1 - 5 Assess state continuity`. Optional input:
`story1IdleWait` (`skip` default, or `wait-31-min`).

**Agents reused:**
- The existing `data-analysis-agent` (Step 1 version).
- "Session recreation" below refers only to the Code Interpreter **sandbox**
  session. The agent, its version, and its deployment are never recreated or
  redeployed.

1. **State layers.** Track the real ID for each layer separately:
   - Responses conversation.
   - Hosted-agent session (`agent_session_id`).
   - Sandbox fingerprint and container ID.
   - Mounted input files.
   - Custom checkpoint.
2. **Continuity:**
   - Request 1 (conversation C1) loads the 10k file, defines `story1_state`, and writes `/mnt/data/state-marker.json`.
   - Request 2 (same C1) checks the variable, marker, and fingerprint through the tool, not chat history.
   - Request 3 (new conversation C2) runs the same checks, which tests the documented shared context.
3. **Checkpoint.** The Task client stores a small versioned checkpoint containing the dataset file ID and checksum, the transform spec, validated aggregates, and file references. Storage locations:
   - Locally in the run folder.
   - Optionally in the **existing** `artifacts` Blob container (`stpock46bujqf6iic2`) if developer Blob access is already granted.

   No binaries, Python heap, or new storage. Hosted workflow state ([foundry_state.py](../../src/backend/app/services/foundry_state.py)) is not modified.
4. **Session recreation.** Use one method, in this order of preference:
   - (a) If Step 1 showed the container ID and the containers API allows it, delete only the Story 1 sandbox container. The report records that this resets the project-shared context.
   - (b) With `wait-31-min`, idle past the documented 30-minute timeout.
   - (c) Otherwise, mark the step `BLOCKED` with the documented behavior.

   A new conversation alone is not treated as recreation. The fingerprint or container ID must change.
5. **Before rehydration:** record what survived. Expected:
   - Toolbox `file_ids` are re-mounted.
   - Variables and the marker file are lost.
6. **Rehydrate explicitly:** send the checkpoint to the agent, reload the file, replay the transforms, and compare checksum and aggregates with the checkpoint. Label this `Custom implementation (Dev Team)`. It is not restored RAM or automatic recovery.
7. **Customer-network storage.** Assess each layer (documentation and architecture; no new infrastructure):

| Layer | Customer-network placement | Basis |
|---|---|---|
| Code Interpreter sandbox | Not possible: Microsoft-managed, no outbound access, does not inherit the subnet | Documented |
| Uploaded files, conversations, threads | Possible via [Standard Agent Setup][std-setup] with BYO Storage/Cosmos DB in the customer subscription; not the POC's current Basic setup | Documented (not tested) |
| Hosted-agent workflow state | Foundry-managed State Store; verify BYO options | Unverified -> follow-up |
| Custom checkpoints and artifacts | Customer Blob with private endpoint is feasible. The hosted agent mediates: it reads the customer store, then passes data to the sandbox, because the sandbox cannot reach the customer network. The POC storage uses an NSP, not a private endpoint, so this is not live private-endpoint evidence. | Architectural assessment |

**Markdown report `05-state-continuity.md`:**

- Request-by-request continuity table.
- Before/after recreation inventory (survived / lost / rebuilt).
- Rehydration comparison.
- Customer-network placement matrix.

**Exit gate:** continuity and rehydration have tested results or explicit gaps,
and each storage layer has a scoped placement assessment. There are no claims
of private residency or transparent recovery without evidence.

**Tests:**

- Checkpoint schema and checksum validation.
- Missing, corrupt, and stale checkpoints.
- Fingerprint comparison.
- Recreation-method selection.

## 7. Change surfaces

| File | Change |
|---|---|
| [settings.py](../../src/backend/app/core/settings.py) | Optional `toolbox_endpoint` |
| [runtime.py](../../src/backend/app/runtime.py) | Data Analysis-only `FoundryToolbox` + conditional instructions |
| [hosting.py](../../src/backend/app/hosting.py) | Preserve generated-file references in the plain-chat response |
| [azure.yaml](../../azure.yaml) | `TOOLBOX_ENDPOINT` for `data-analysis-agent` only; description |
| [reporting.py](../../scripts/common/reporting.py) | Opt-in `MarkdownReport` + Rich Markdown rendering + `story-1/index.md` |
| `scripts/common/synthetic_data.py` (new) | Deterministic generator + independent aggregates |
| `scripts/run_scenario/story1_data_analysis.py` (new) | Five-step scenario runner |
| [tasks.json](../../.vscode/tasks.json) | Five `Agents: Story 1 - N ...` tasks; inputs `story1Step1Mode`, `story1IdleWait` |
| [tests/unit](../../tests/unit) | Settings, runtime registration, artifact extraction, generator, reporting, runner bounds, task labels (extend [test_vscode_config.py](../../tests/unit/test_vscode_config.py)). Reuse guard: missing, inactive, or changed agents are blocked or failed; Steps 2-5 have no deploy, provision, agent-mutation, or toolbox-creation code path. |
| [README.md](../../README.md), [tasks-detailed.md](../tasks-detailed.md), [deployment.md](../deployment.md) | Task table, run order, output interpretation |
| [ADR-0001](../adr/0001-hosted-agents-poc.md) | Amend the decision: managed Code Interpreter is now the story baseline (the ADR currently prefers in-container Python); record observed constraints and evidence |

## 8. Run order and cost

| Order | Task | Existing agents used | Paid usage | Creates or deploys |
|---|---|---|---|---|
| 0 | **Quality: Run all local checks** | None | None | None |
| 1 | `Agents: Story 1 - 1 Enable Code Interpreter on existing Data Analysis agent` | `data-analysis-agent` (snapshot of all three) | Data Analysis image build (first run only), tokens, sandbox session | Toolbox + update of the existing `data-analysis-agent` (new version, same agent), **first run with `enable-if-missing` only**; reruns are verify-only |
| 2 | `Agents: Story 1 - 2 Analyze data and deliver outputs` | `data-analysis-agent`, `supervisor-agent` v1 | Tokens, sandbox | None |
| 3 | `Agents: Story 1 - 3 Evaluate dataset sizes` | `data-analysis-agent` | Tokens, sandbox (3 requests) | None |
| 4 | `Agents: Story 1 - 4 Assess sandbox boundaries` | `data-analysis-agent` | Tokens, sandbox (few requests) | None (temporary marker file is deleted) |
| 5 | `Agents: Story 1 - 5 Assess state continuity` | `data-analysis-agent` | Tokens, sandbox; optional 31 min idle | None (may reset the Story 1 sandbox container, not the agent) |

Tasks 2-5 can be rerun independently. Task 4's documentation section also runs
without Task 1 and marks live probes `BLOCKED`.

**Cleanup and retention:** keep the toolbox, the uploaded synthetic files, and
the DA version for reruns. To retire them later, set the active DA version back
to v1 and delete the toolbox and its files. These are documented manual steps
only, never automatic.

## 9. Validation during implementation

1. Focused unit tests for each touched surface, then **Quality: Run all local checks** (ruff, format, mypy strict, pytest).
2. Run the Data Analysis host locally (F5 **Data Analysis host**) with `TOOLBOX_ENDPOINT` set, to check tool wiring before any deployment.
3. Run Task 1 in `verify` mode first, to confirm it is read-only. Then run it once in `enable-if-missing` mode.
4. Run Tasks 2-5 in order. Open each `0N-*.md` in Markdown Preview and confirm the tables, links, and chart render.
5. Rerun **Agents: Smoke test all hosted agents** to confirm there are no regressions for Supervisor or Research. Data Analysis must still answer the exact smoke marker.

## 10. Completion checklist

- [ ] Five independently runnable Tasks, one per acceptance criterion.
- [ ] Each Task writes a readable Markdown report, linked from `artifacts\story-1\index.md`.
- [ ] Every step ran against the existing `data-analysis-agent` (and the existing `supervisor-agent` v1 in Step 2). No agent was created. Only Step 1 changed the existing Data Analysis agent's version, once, and each report's **Agents reused** start/end snapshots match.
- [ ] Only one toolbox and one new version of the existing Data Analysis agent were created; Tasks 2-5 created nothing.
- [ ] Sandbox execution is proven by computed values, not model text.
- [ ] Summaries, CSVs, and the chart are retrievable by the user; the Supervisor path status is recorded.
- [ ] AC3 conclusion is scoped to 1k/10k/100k and the evaluated configuration.
- [ ] Shared-context limitation, lifetime, network, and configuration findings are documented, with evidence basis.
- [ ] Continuity, rehydration, and per-layer customer-network placement are assessed, with gaps listed.
- [ ] Versions, reproduction steps, ownership classifications, limitations, and follow-ups are recorded.
- [ ] Existing smoke, identity, workflow-guard, and task behavior is unchanged.

## References

- [Story #1](https://github.com/wxifaras/hosted-agents-poc/issues/1); related: #3, #4, #6, #7, #8
- [Code Interpreter for Foundry agents][ci-docs]
- [ACA dynamic sessions - code interpreter][aca-ci]
- [Foundry tool support by region and model][regions]
- [MAF hosted-agent toolbox sample][toolbox-sample]
- [Custom Code Interpreter][custom-ci]
- [Standard Agent Setup][std-setup]

[ci-docs]: https://learn.microsoft.com/azure/foundry/agents/how-to/tools/code-interpreter
[aca-ci]: https://learn.microsoft.com/azure/container-apps/sessions-code-interpreter
[regions]: https://learn.microsoft.com/azure/foundry/agents/concepts/limits-quotas-regions#tool-support-by-region-and-model
[toolbox-sample]: https://github.com/microsoft-foundry/foundry-samples/tree/main/samples/python/hosted-agents/agent-framework/responses/04-foundry-toolbox
[custom-ci]: https://learn.microsoft.com/azure/foundry/agents/how-to/tools/custom-code-interpreter
[std-setup]: https://learn.microsoft.com/azure/foundry/agents/concepts/standard-agent-setup
