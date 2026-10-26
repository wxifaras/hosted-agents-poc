# ADR-0001: Hosted Agents POC architecture and evidence boundaries

| Field | Value |
|---|---|
| Status | Accepted POC baseline; authorization-guard alignment remains proposed |
| Decision owner | POC owner and maintainers |
| Recorded / status reviewed | 2026-10-20 / 2026-10-21 |
| Scope | Synthetic-data experiments, not a production service |
| Validation | Partial; an accepted design is not proof of deployed behavior |

## Context and goals

Prove what three independently hosted agents can do beyond answering prompts:
coordinate analysis, preserve the right state, produce artifacts, and recover
logically after interruption. Measure runtime boundaries rather than assume them.

The core questions are:

- Which CSV/XLSX analysis workloads fit each supported CPU/RAM tier?
- What survives idle/resume, process loss, and later handler re-entry?
- Can Supervisor and specialists keep distinct, recoverable conversations?
- Can authorized users resume their own work without accessing another user's
  history, state, files, or artifacts?
- Can results and failures be traced and reproduced?

This record is self-contained. The [README](../../README.md) is the entry point;
the [deployment guide](../deployment.md) contains commands. This is the only
architecture/status record for now; split it only when its size warrants it.

## Decision

### Execution and deployment

Use exactly three logical Microsoft Foundry Hosted Agents: Supervisor, Research,
and Data Analysis. Each is deployed independently, with its own runtime identity
and session boundary, while sharing one project, model deployment, source package,
and container recipe.

- Use Microsoft Agent Framework (MAF) with the public Responses host contract.
  Plain chat uses the role's model; explicit workflow commands coordinate
  specialists. Current orchestration is deterministic, not model-driven planning.
- Use Python 3.14, root `uv.lock`, `src/backend/app`, and custom Linux containers.
  All Python package resolution uses
  `https://packagefeedproxy.microsoft.io/pypi/simple/`.
- Keep native Bicep/az/azd provisioning separate from agent-code deployment and
  explicit RBAC operations. No deployment or permission change runs from setup/F5.
- Use typed layered configuration, sanitized structured logs, and Rich operator
  scripts with machine-readable results. Optional integrations stay optional.
- Start with developer-controlled Python analysis in Data Analysis. A separately
  invoked managed Code Interpreter would be a different execution environment,
  not evidence of this container's capacity.
- **Amendment (2026-10-22, Story #1):** for Story 1, the baseline is
  Microsoft-managed Code Interpreter. It is attached through a Foundry toolbox
  to the **existing** Data Analysis hosted agent, using `FoundryToolbox`, only
  when `TOOLBOX_ENDPOINT` is set. Sandbox measurements describe that managed
  sandbox, not the hosted container's 0.5 CPU / 1 GiB. Developer-controlled
  in-container Python remains a separate option and is not implemented.

The disposable POC permits authenticated public endpoints. Blob connectivity
uses enforced Network Security Perimeter (NSP) with broad IPv4 ingress,
not unrestricted Storage firewall fallback. Allow-all NSP ingress is **not**
a restrictive IP boundary or private networking. Anonymous/shared-key Blob
access remains disabled. This is not a production security architecture.

### State and ownership

| Layer | Responsibility |
|---|---|
| RAM | Transient Python objects; never the recovery contract |
| Session filesystem | `$HOME` and `/files`; verify actual idle/resume restoration separately |
| Responses conversations | Platform-managed history, distinct for Supervisor and each specialist |
| FoundryStateStore | Small versioned mappings, registry, progress, and future checkpoints |
| Blob Storage | Large inputs, derived files, and plots, passed as authorized references |
| Semantic memory | Optional cross-session recall; never execution recovery |

Persist independently random UUID4 owner/session labels in platform-user-isolated
State Store. A namespaced SHA-256 of the verified platform session is a lookup key,
not a credential, authorization check, or random-ID seed.

Only explicit new-workflow creation initializes absent mappings. Continuation,
inspection, and recovery retrieve existing bindings; missing, expired, inconsistent,
or unavailable state fails explicitly without reminting IDs or local-store fallback.
Create-only conflicts read the committed winner; exact server ETags guard mutable
updates. Preserve actual conversation/session/response references, not invented IDs.

Supervisor owns the root registry; each specialist has its own verified context
and mappings. Never assume shared store names, sessions, or opaque labels mean
shared ownership. Artifact authorization is separate from State Store isolation.
Keep JSON below the documented 1 MB item limit; the current adapter applies a
stricter 200 KB guard. Binaries belong in Blob with checksums and scoped references.

### Authorization contract and proposed alignment

The [State Store caller contract][state-store] derives user isolation from the
opaque `x-agent-foundry-call-id`, validated by the service with the caller's
credentials and permissions. Forward it unchanged on first-party Foundry calls,
including other agents. Capture the original call context for deferred item
operations; never log it or pass it to the model.

The [runtime contract][runtime] describes `x-agent-user-id` for partitioning data
the container manages itself. Its visibility is not documented as a separate
State Store authorization prerequisite. Conversely, merely receiving a call-ID
string is not proof that an operation is authorized.

**Current implementation gap:** `Caller.current()` requires a visible user ID,
and the specialist-result check requires an exact visible-user comparison.
The nested Research probe has runtime/call/session/conversation context, but no
visible user ID. Our guard therefore stops before the authoritative State Store
item operations run. This is an application validation blocker, **not established
proof that Foundry cannot preserve caller identity or authorize nested state access**.

**Proposed validation-led alignment, not implemented by this documentation
change:** separate State Store authorization requirements from container-local
user-ID requirements, but do not remove the guard and infer that delegation
works. The runtime contract describes platform-injected user IDs, and the
[official user-aware Python handler][python-isolation] requires both user and
call context before serving platform-managed history. That is an appropriate
sample policy, not a universal State Store API prerequisite.

Retain verified hosted context, call/session/workflow bindings, service-enforced
user isolation, and all missing-mapping failures. Validate actual authorized
access and cross-user/session denial before marking delegation complete.
Do not substitute a user ID from a model, request body, diagnostic flag, or
untrusted header. Application-owned files/caches/artifacts need their own
documented ownership checks.

`x-ms-user-identity` is a [separate trusted-service delegation mechanism][delegation]
requiring the scoped impersonation action. Supervisor currently sends it alongside
the call-context header; the approved Supervisor-only project role was confirmed.
That attempt did not populate the nested user ID or prove a successful workflow.
Neither widening permissions speculatively nor requiring every diagnostic flag
to be true is a substitute for testing the authorization contract.

#### Identity mechanisms are not interchangeable

| Mechanism | Meaning / boundary |
|---|---|
| Entra credential on the request | Authenticates the calling human, service, or agent; a service credential alone is not proof of the original human caller |
| `x-agent-foundry-call-id` | Opaque first-party caller context; forward unchanged and let Foundry validate it |
| `x-ms-user-identity` | Stable application-user label supplied by an authorized trusted service; not an access token or OAuth on-behalf-of exchange |
| `x-agent-user-id` | Platform-resolved output for user-aware handling and application-owned partitioning; not a client-supplied authorization input |

The [session guide][sessions] separates sandbox continuity (`agent_session_id`)
from history continuity (`conversation` or `previous_response_id`). A conversation
autobinds a session; a session ID alone does not replay history. The
[agent-management guide][agent-management] governs versions/protocol configuration,
not proof that a request acts for the original end user.

The [delegation guide][delegation] requires stable labels derived from
authenticated server-side identity and explicit impersonation permission.
The sources inspected do not establish that copying a resolved global user ID
into an application-user label must reproduce the identical resolved ID downstream.
Define the identity model and expected binding before making raw-ID equality
the acceptance criterion. A present call ID also does not prove its original
caller context survived the hop; check the actual request and resource behavior.

Administrative session visibility is a separate case: the isolation documentation
allows project-level Foundry User administrators to manage sessions across users.
Do not classify that expected privilege as a user-isolation failure.

### Recovery and validation

Use meaningful analysis phases, MAF checkpoints, stable operation identities,
and artifact watermarks for later recovery. Full Responses recovery requires
stored background work and the supported resilient-runtime opt-in; these are
not enabled in the current workflow.

Distinguish file restoration, client reconnect, process replacement, and
application-checkpoint recovery. Never describe any of them as restoring RAM.
An unfinished `progress: running` record is not evidence of a still-active or
successful worker; ambiguous work is not automatically replayed today.

Local tests prove application rules, not hosted isolation, capacity, or recovery.
Hosted findings need observed outcomes and reproducible commands. Failed,
unsupported, and untested scenarios remain explicit. Keep generated evidence
local and sanitized; published documentation summarizes findings rather than
depending on private run IDs or files.

Provisioning, model/hosting usage, and deliberate failure experiments require
owner-approved targets and execution. Crash/OOM experiments must be bounded,
opt-in, and isolated to a dedicated test target. No such experiment runs by default.

## Alternatives and consequences

| Alternative | Reason not selected for the core POC |
|---|---|
| Three agents inside one process | Simpler locally, but cannot answer independent-host runtime/identity questions |
| Prompt-only agents or managed Code Interpreter alone | Does not measure developer-controlled Python analysis inside the chosen hosted sandbox |
| New database for every state concern | Adds services and obscures the Foundry State Store experiment; Blob handles large objects |
| Filesystem markers or RAM as recovery state | Cannot prove durable logical recovery or safe replay |
| Mandatory A2A, semantic memory, or production infrastructure | Expands scope without answering the initial runtime questions |

Benefits: a small deployable baseline, explicit state boundaries, and experiments
that can be evaluated independently. Costs: preview/SDK compatibility risk,
shared-model throttling, additional hosted identities, and explicit checkpoint/
artifact ownership work. Public POC connectivity and absent production SLAs are
accepted limitations, not production recommendations.

## Implementation and evidence snapshot

This dated appendix is a living summary, separate from the accepted decisions.
Implementation, platform availability, and hosted verification are different axes.

### Current status

Independent hosted model smoke tests have passed. Full delegation is not yet
validated: normal workflows still stop at the unchanged nested-user-ID guard.
Separate, opt-in hosted experiments have now exercised actual State Store/history
operations without requiring that visible field. A continuity passed, but synthetic
delegated B could access A's state and history on the tested path. Do not remove
the normal guard or declare end-user isolation proven. The cause of missing/
ineffective delegated identity remains unresolved. A later direct Research test
using two genuinely authenticated Entra users passed state/history isolation and
owner continuation in both directions. That is not hosted-to-hosted delegation
proof. Analysis tools and automatic recovery are not implemented.

| Capability | Implementation | Observed evidence / remaining validation |
|---|---|---|
| Python/uv, core modules, packaging, Rich scripts | Implemented | Local import/readiness, packaging, lint/types/tests exercised |
| Bicep foundation and operator access | Implemented | Provisioning, model, developer Blob/monitoring probes observed; invalid Monitoring Reader role ID in `project-access` fixed 2026-10-25; isolated NSP-denial checks pending |
| Three independent hosted model agents | Implemented | Last all-three hosted smoke passed on version 4; not an orchestration test |
| Last verified deployment snapshot | Supervisor 11, Research 6, Data Analysis 4 | Native-user tests independently verified Research 6; rediscover versions before testing |
| Idle policy: 120 seconds | Configured | Raw deployed metadata verified; does not prove file restoration or exact deprovision timing |
| Random durable mappings/registry | Implemented | Supervisor readback/reuse and unknown-workflow rejection observed; restart and two-user proof pending |
| Supervisor delegation and child histories | Partial | Research reference persisted before failure; both specialists' completed histories/final result unverified |
| Context diagnostic | Implemented | Only nested user ID absent; adding the delegation header did not change that observation |
| Separate forwarding/state/history experiments | Implemented, opt-in | Hosted nested delegated B denial failed; direct native-user Research isolation subsequently passed |
| Direct native-Entra-user control | Passed for Research 6 in both directions | Two distinct authenticated users, no delegated headers, state/history denial and final owner continuation; not nested propagation proof |
| Direct delegated-user control | Passed with temporary approved operator permission | Platform user visible, A continuity and B denial verified; temporary grants removed; nested flow still fails |
| CSV/XLSX analysis and dataset generation | Partial (Story 1) | Managed Code Interpreter on the existing Data Analysis agent (v6 in `hosted-agents-poc-dev`) verified 2026-10-24 with a sandbox-computed SHA-256 and container reference; filter/group/visualize with summary, aggregates, CSVs, and chart delivered to the user (Story 1 Step 2, AC2) Met on 2026-10-24, Supervisor path blocked at the nested caller guard and file forwarding through it not built; Story 1 Step 3 (AC3) on 2026-10-24: exact results and all outputs at 1k/10k/100k rows (up to 5.4 MiB) in one sandbox execution each, peak RSS 162 MiB of a 2 GiB / 1 CPU enforced sandbox limit; viable for the tested sizes only; Story 1 Step 4 (AC4) on 2026-10-24: sandbox runs as non-root `sandbox` with no capabilities and a read-only root, has no DNS or egress, gives every tool call a new container, and lets other project principals read sandbox outputs; per-call and toolbox-level `memory_limit` 4g did not change the enforced 2 GiB, network allowlists are rejected by organization policy, a 100 s execution completed beyond the tool's self-described 90 s limit (consistent with the documented 220 s maximum), and containers follow a 20 min idle-expiry policy (expired after 21.5 min idle) versus the documented 30 min; Story 1 Step 5 (AC5) on 2026-10-24: conversation history continues and is isolated per conversation, sandbox variables and files never carry over, a deleted container's files are gone, and state was rebuilt exactly from a sealed checkpoint stored in customer Blob |
| ArtifactStore/Blob references | Not implemented | Storage exists; application I/O, checksums, and authorization still required |
| Capacity harness | Not implemented | Measure progressive workloads at 0.5/1, 1/2, and 2/4 vCPU/GiB; no measured boundary yet |
| Session filesystem idle/resume | Not validated | Upload, derive, idle/resume same session, verify hashes, continue analysis |
| MAF checkpoints and crash recovery | Not implemented | Stored background execution, process replacement, original-result completion, and replay safety |
| Reconnect/cancellation/stream replay | Not implemented | Distinguish client disconnect from process loss; advanced steering remains optional |
| User/session/specialist isolation | Partial, not end-to-end | Direct native-user Research state/history passed; normal workflow mappings, other agents, files/artifacts and originating-user downstream permissions remain unvalidated |
| Tracing | Partial | Structured logging and monitoring resources exist; end-to-end export/correlation unverified |

The [context probe](../deployment.md#workflow-and-context-diagnostics) collects
booleans without model inference or application State Store writes. Its
`user_id_present: false` result is an observation, not a service authorization
verdict. No actual identities or opaque call IDs are published.

## Foundry feature adoption

Availability below follows the [September 2026 announcement][september] or the
linked feature documentation, reviewed 2026-10-20. GA/preview does not establish
regional availability, pinned-SDK compatibility, implementation, or test success.

| Capability | POC role | Implementation / evidence |
|---|---|---|
| [Hosted Agents][hosted], custom containers, Responses | Core | In use; independent hosted model responses verified |
| [State Store (preview)][state-store] | Core durable state | Partial hosted mapping proof, not full checkpoint/isolation proof |
| Session filesystem and conversation history | Core lifecycle experiment | Interfaces in use; lifecycle and private-history boundaries unverified |
| Dedicated agent identities and model deployment | Core | In use; actual principals and scoped model access checked |
| Hosted-agent Toolboxes (GA) + Tool Search (GA) | Story 1 Data Analysis (Code Interpreter); planned Research experiment | Toolbox `story1-data-analysis-ci` with `code_interpreter` is in use by the existing Data Analysis agent; Research is not connected |
| [Long-running hosted-agent resilience (public preview)][resilience] | Planned core recovery experiment | Not enabled; meaningful checkpoints and opt-in failure tests required |
| Insights (public preview) | Optional after repeatable traces | Not used; findings require human review, not automatic changes |
| A2A (GA) | Optional interoperability experiment | Not used; Responses delegation is not A2A interoperability proof |
| Routines (GA) | Candidate for scheduled runs/reporting | Not used; no schedule or recurring spend enabled |
| Model Router session affinity/routing metadata (preview) | Optional routing comparison | Not used; keep the explicitly selected model until separately reviewed |
| Cross-session semantic memory | Optional, separate experiment | Not used; Foundry/Cosmos choices need their own compatibility and isolation checks |

MAF/uv are the framework/toolchain; ACR, Blob, NSP, and Azure Monitor are supporting
Azure services, not additional Foundry features. Managed Code Interpreter is
used only for Story 1 (see the amendment above). Voice, Foundry Local, model-catalog upgrades, a frontend,
production load testing, and multi-region/private-network production design are
not automatically added because a release announcement mentions them.

## Next validation increments

1. Define the intended identity model and run the focused delegation tests below.
   Use the findings to align the guard without weakening ownership or isolation;
   do not assume guard removal fixes the workflow. Then complete both specialist
   calls and distinct-history follow-up.
2. Build a tiny synthetic CSV analysis with a real derived artifact; add XLSX
   and plotting. Analysis logic can progress in parallel with authorization work.
3. Add scoped artifact I/O, actual file idle/resume checks, and measured capacity
   progression across the three tiers.
4. Add MAF phase checkpoints, stored-background reconnect, and separately
   authorized process-loss/re-entry/idempotency experiments.
5. Complete cross-user/session/specialist denial tests, Research tools, and
   trace-backed findings. Keep A2A, semantic memory, HITL, advanced steering,
   and optional Insights/evaluation work outside the initial success gate.

### Planned delegation tests

DLG-01 is **implemented and passed locally** as of 2026-10-21. Dedicated hosted
DLG-02/03 experiments have been executed with the results below, including a
passing direct two-Entra-user Research control. Hosted-to-hosted propagation
remains unresolved. DLG-04 is still **planned, not executed
successfully**. Presence probes and client-default-header checks alone do not
satisfy this matrix.
Use synthetic data and fresh explicit workflows; do not reset existing mappings.

Before hosted execution, record the chosen identity model, actual invoking
principal and scoped permissions, target agents/versions, and owner approval.
Distinguish two genuine Entra identities from two delegated application labels:
the latter tests the trusted-service delegation contract, not two independent
user authentications. Hosted calls can incur usage and need separate authorization.
No new grant, deployment, guard change, failure injection, commit, or push is
authorized merely by recording this plan.

| ID | Environment | Test | Required evidence |
|---|---|---|---|
| DLG-01 | Local, no network | Capture actual prepared outbound requests through the pinned SDK, not just client defaults | Intended target and exact forwarded call context verified in memory; delegation label included only under the chosen contract; diagnostics contain booleans, never credentials/context values |
| DLG-02 | Hosted | User A creates/reads a small user-isolated State Store marker; user B cannot retrieve A's marker | A continuity succeeds; B is isolated or receives the expected denial/not-found outcome; missing/mismatched workflow mappings fail without writes or reminting |
| DLG-03 | Hosted | A creates/continues a response, B attempts A's chain, then A continues again | B is denied without A history; A's post-B continuation uses the original owner client/session/response chain, sees its marker, and creates a new response; unrelated failures are not isolation passes |
| DLG-04 | Hosted, after the preceding cases | Full Supervisor -> Research -> Data Analysis workflow and follow-up | Both specialists complete, distinct conversations/mappings persist, authorized follow-up reuses them, and unrelated users/conversations cannot inherit private history |

**DLG-01 local evidence:** [transport tests](../../tests/unit/test_delegation_transport.py)
exercise the unchanged `SpecialistClient` with the real pinned Projects/OpenAI
SDKs. Only the credential and final HTTP transport are replaced by synthetic,
in-memory implementations; any attempt to enter the real HTTP transport fails.
Both Research and Data Analysis cases capture conversation creation, the initial
workflow call, and continuation. They verify the actual endpoint/API version,
credential scope, call-context/delegation/expected-caller headers, serialized
command and conversation binding, and reuse of the child platform session.
The Supervisor session is not sent as the child's session. Values are compared
only in memory; assertion diagnostics expose booleans and sanitized HTTP details.

Result: all six captured requests preserve the configured headers and bindings.
The tests request no Azure tokens from a real credential, network, model
inference, or application State Store access. Synthetic responses exercise
parsing/binding checks but do not establish server-side caller propagation or
identity equality. No local request-construction bug was found; the hosted
gateway/runtime and resource-authorization behavior remain unverified.
The runtime guard is unchanged. Run this focused check with:

```powershell
uv run --frozen --no-sync pytest tests\unit\test_delegation_transport.py -q
```

**SDK-aligned history-test strengthening, local change on 2026-10-21:**
the [official Projects SDK isolation sample][sdk-user-isolation] demonstrates
A creating a response, B receiving `404 NotFound` on that chain, and A then
continuing successfully. Our diagnostic now extends its prior A-A-B sequence
with an explicit final A continuation:

```text
A creates -> A continues -> B attempts that chain -> A continues the same owner chain
```

The final request uses A's original client, the same child platform session, and
the response ID preceding B's attempt, never a response returned to B. It must
see the synthetic history marker and create a distinct response in the original
session. The existing owner GET confirmation for a generic `404/not_found`
remains: retrieving an owned response and continuing its history are separate
checks. A successful final A request never erases an unexpected B-success failure.

Regression cases cover B success followed by A success, a missing owner resource,
failed marker recall, a changed owner session, and a reused response ID.
These are local fake-client tests, not additional hosted isolation evidence.
The earlier Supervisor 7-9 and direct-control findings used the owner-GET check;
they do not establish success of the new post-B continuation assertion.
The local refinement changed no guard, permission, deployment, or hosted execution.
The subsequently authorized Supervisor 10 follow-up below records the new
assertion's first real hosted execution.

**Step-2/3 hosted findings, 2026-10-21:** the owner authorized targeted diagnostic
deployment and bounded synthetic state/history operations. Actual Supervisor 7
and Research 6 were active with the unchanged 120-second idle policy; Data
Analysis was not redeployed. Existing Supervisor impersonation permission was
verified read-only; no grants or foundation changes were applied.

The [experiment module](../../src/backend/app/services/identity_checks.py) tests
one mechanism at a time: the original call ID only, or a server-generated
application-user label only. The two labels are distinct, run-scoped synthetic
identities under the same Supervisor service credential, not two real users.
The separate expected-caller header arrived, but no visible nested platform
user ID was present for either variant. Local final-transport tests also verify
that these variants do not accidentally mix identity headers.

| Observed hosted assertion | Call context only | Explicit delegated labels |
|---|---|---|
| Runtime/call/session/conversation context present | Yes | Yes |
| Visible platform user ID | Missing | Missing |
| A creates and rereads the same synthetic State Store marker | Passed | Passed |
| A continues its platform response history and sees its synthetic marker | Passed | Passed |
| B cannot retrieve A's state marker | Blocked: second genuine caller unavailable | **Failed: marker was found** |
| B cannot continue A's response chain | Blocked: second genuine caller unavailable | **Failed: continuation accepted and private synthetic marker seen** |

These results demonstrate that missing visible `user_id` alone does not prevent
service state/history access. They do **not** demonstrate original-human
identity propagation or user isolation. On the tested nested service path,
the labels did not establish the intended A/B boundary. Do not generalize this
to a platform-wide isolation defect: effective delegated identity/gateway
handling under this credential/path still needs investigation.

The matrix correctly remained non-passing (context diagnostics and B denial
checks failed, genuine-user cases blocked). It requested no model inference,
normal workflow-registry mutation, fault injection, or permission expansion.
Only dedicated synthetic markers were created in a user-isolated diagnostic
store with a verified one-hour TTL; platform conversations/response records also
exist. Ordinary workflow guards, mappings, and replay rules remain unchanged.
Repeat commands and the opt-in switch are documented in the
[deployment guide](../deployment.md#identity-variants-and-isolation-experiment).
Do not bypass the failed isolation checks to complete DLG-04.

**Direct-control follow-up, 2026-10-21:** a developer-to-Research delegated-label
control was attempted against the existing Research 6, without redeployment or
new grants. Both the preliminary conversation-create path and a direct Responses
request returned HTTP 403 (`forbidden`) before the diagnostic handler ran.
The service error did not explicitly name the impersonation action. This is
not an A/B isolation result and does not establish the exact denial cause.
Supervisor's scoped permission does not apply to the local developer identity.

A read-only attempt to verify the current operator's assignments could not
complete because the directory lookup encountered a Conditional Access
`InteractionRequired` challenge (`TokenCreatedWithOutdatedPolicies`). Do not
automatically refresh/login or widen roles. Restore the operator's authentication,
verify its exact permissions, and obtain explicit approval for any missing
scoped impersonation grant before completing this control. The reusable
`--direct-control` command uses response chains, not invented call context, and
leaves normal workflow checks unchanged.

**Subsequent approved control, 2026-10-21:** the directory challenge persisted
after user-driven sign-in. The independent ARM effective-permissions API succeeded
and showed no explicit impersonation action for the developer at the project.
The owner then approved a temporary assignment of the existing single-action role
at that exact project, with cleanup after each bounded control. No role definition,
administrative role, or permanent developer grant was created. Principal metadata
was resolved locally from the current ARM credential without exporting the token.
Each run-created assignment was removed and its absence verified; existing grants
and Supervisor's assignment were untouched. Assignment removal is not proof of
instant service-cache/token revocation.

The direct Research control then established visible platform user context and
successful A state/history continuity. Delegated B could not find A's synthetic
state marker. B's history continuation returned `404/not_found`; the initial
strict classifier conservatively left that generic response non-passing.
A follow-up control confirmed that A could still retrieve the exact completed
response after B's rejection, distinguishing hidden-resource denial from an
absent resource or broken endpoint. All direct delegated-label assertions passed.
This is still a trusted-service synthetic-label test, not two genuine Entra users.

Supervisor 8 adds the `compare-chains` diagnostic, which omits conversation
creation and uses `previous_response_id`, matching the direct-control history
pattern. Its real nested comparison still lacked visible user context, and
delegated B could still read A's state and continue A's history. Research remains
6 and Data Analysis 4; 120-second idle policies were verified unchanged.

| Boundary | Delegated user visible | A continuity | B state/history denied |
|---|---|---|---|
| Direct operator -> Research, approved temporary permission | Yes | Passed | Passed, including positive owner response readback |
| Supervisor -> Research, explicit conversations | No | Passed | Failed |
| Supervisor -> Research, response chains | No | Passed | Failed |

The remaining discrepancy is specific to the tested hosted-agent-origin path;
explicit conversation binding is not its sole cause. The precise credential,
egress/gateway, or SDK/runtime interaction is not yet established. Do not infer
a platform-wide defect, route around isolation with application-supplied identity,
or remove the workflow guard based on A-only access succeeding.
That finding motivated the hosted egress audit below. No model calls,
normal-registry mutations, commits, or pushes were performed during these controls.

**Hosted egress audit, 2026-10-21:** deployed only Supervisor 9, retaining the
120-second idle policy. Before comparison, the diagnostic resolved Supervisor's
actual instance principal/client metadata through the public project SDK.
It then inspected the final request objects for five call-context and seven
delegated-label Research invocations. The audit covers the application HTTP
transport boundary, not packet contents after any platform proxy.

All twelve inspected requests matched the expected public Foundry Research
endpoint. The selected call-context/delegation/expected-caller headers matched
the client configuration, with no client-supplied platform user header.
The bearer credential's readable principal/client claims matched Supervisor's
instance metadata and used the expected Foundry audience. Claim decoding is
diagnostic comparison only, not signature validation or an authorization check;
no token, claim value, actual identity, or opaque call ID was returned.

The previously approved custom impersonation assignment was independently
reverified read-only against the same actual Supervisor principal/project.
Nevertheless, both nested modes still lacked visible platform user context,
and delegated B still found A's marker and continued A's synthetic history.
This rules out a reproduced application-side target/header mismatch or wrong
principal selection in the inspected requests; it does not identify which
post-transport proxy/gateway/runtime behavior causes the difference.

The direct delegated-user control remains the passing comparison. Preserve
the normal workflow guard: there is no evidence-backed local bypass that
establishes the intended isolation. Next choices are a minimal reproduction
for Foundry support, or an explicitly approved experiment using the official
Toolbox/A2A connection pattern and its documented authentication mode.
Do not silently replace the accepted Responses architecture, widen roles,
export credentials, or force isolation tests green. Any alternative connection/
protocol experiment needs its own scope and execution approval.

**Hosted final-A follow-up, 2026-10-21:** the owner authorized only a
Supervisor redeploy and one nested response-chain comparison, with no RBAC
changes or direct-operator control. Supervisor 10 was active with exclusive
latest-version routing; Research 6 and Data Analysis 4 were not redeployed.
All three retained the verified 120-second idle policy.

The deployment initially stopped on an ACR build-log `404 BlobNotFound` while
the submitted build remained queued. That build subsequently succeeded.
Deployment recovery with `--from-package` completed, but registry history
showed an additional successful ACR task and a different published image.
Do not infer image/build reuse from the recovery flag or azd's progress output.
No infrastructure, permission, or diagnostic guard was changed.

| New hosted response-chain observation | Result |
|---|---|
| A's initial state/history continuity | Passed |
| B cannot read A's state marker | Failed: marker was found |
| B cannot continue A's history | Failed: request accepted and synthetic private marker seen |
| A continues after B's attempt | Passed: marker seen, original child session reused, distinct response created |
| Visible nested platform user ID | Still missing in both diagnostic modes |
| Genuine-Entra-user negative test | Blocked: second authenticated principal unavailable |

The new final-A assertion is now **hosted evidence**, displayed separately in
the Rich report and persisted results. It proves owner continuity after B's
attempt, not after a successful B rejection: B was not rejected in this run.
DLG-03 and the overall isolation scenario therefore remain non-passing.
The report recorded 30 PASS, 4 FAIL, and 1 BLOCKED, with exit code 1.
The boolean-only egress checks passed for all thirteen Research requests
(five call-context, eight delegated-label), including the added A continuation.

No direct control was rerun and no temporary developer grant was created.
These synthetic-label observations do not establish two-human isolation or a
platform-wide defect. No model inference, normal workflow-registry mutation,
fault injection, automatic workflow replay, commit, or push was performed.
The next diagnostic remains a minimal support reproduction or a separately
approved official Toolbox/A2A experiment; preserve the normal workflow guard.

**Local diagnostic refinement, 2026-10-21:** the expected-user comparison now
targets the original caller only for call-context forwarding, and the selected
synthetic A/B label for delegated-label forwarding. Each context result identifies
that expectation's source. The comparison remains diagnostic, not authorization.
Previously, the nested delegated variant compared against the original caller,
unlike the direct control. Correcting that measurement does not explain or fix
the observed B state/history access.

The operator now reports matrix completeness separately. Every required core
assertion must appear exactly once, including both B denial checks and final-A
continuation; duplicate check names and an impossible genuine-user PASS while
the protocol declares no genuine-user test also fail collection validation.
Collected FAIL/BLOCKED observations remain visible, and completeness alone never
makes a scenario pass. Offline CLI regressions cover direct/nested results,
empty/incomplete/duplicate matrices, and preservation of failed/blocked checks.
These changes were first validated locally, then deployed with the separately
authorized Supervisor 11 history-only experiment below.

At this increment the classifications were observed delegated isolation **FAIL**,
caller-type contract **UNCONFIRMED**, genuine-user tests **BLOCKED**, and root
cause **UNKNOWN**. The later direct native-user pass below changes only that
control's status, not the earlier nested failures. Do not reclassify failed
assertions as blocked to make the results appear less severe.

Before a Header/isolation-key experiment, reconcile the [session-management
page][sessions] with the [user-isolation page's unsupported protocol-1.0.0
isolation-key guidance][legacy-isolation]. This is not an accepted protocol-2.0.0
authorization workaround. No endpoint, header-isolation setting, or role was
changed. The earlier combined response/session test did not establish the result
of a history-only B attempt without explicitly supplying A's session.

**Hosted history-only experiment, 2026-10-21:** the owner authorized a
Supervisor-only redeploy and one nested matrix using `compare-history-only`.
Supervisor 11, Research 6, and Data Analysis 4 were verified active with the
unchanged 120-second idle policy. Only Supervisor was redeployed.

The delegated-label branch retained its A state/history positive controls and
final-A continuation. Only B's history request omitted the explicit
`agent_session_id`, retaining A's `previous_response_id`. The B result recorded
`explicit_session_requested: false`. Boolean-only inspection of the actual
application HTTP request bodies confirmed the requested previous-response and
session bindings, including omission when not requested.

| History-only delegated-label assertion | Hosted result |
|---|---|
| A state/history continuity | Passed |
| B cannot read A's state marker | Failed: marker found |
| B cannot continue A's previous response without an explicit session | Failed: request accepted and synthetic private marker seen |
| A continues after B's attempt | Passed: marker seen, original session reused, distinct response created |
| Visible nested platform user ID | Still missing |

Explicitly sending A's sandbox ID is therefore **not required** for the observed
B history access on this path. This narrows the reproduction, not the root cause:
the effective delegated-user boundary remains unestablished, and no general
platform defect or unsupported caller contract is proven.

The separate call-context history phase returned HTTP 500 (`server_error`).
It was recorded as a failure, not an isolation denial. Its missing A-history
assertion caused **Matrix completeness** to fail as designed. The whole matrix
was incomplete/non-passing even though the delegated-label branch completed
its positive, negative, and final-A checks. No automatic retry was made.
The report recorded **33 PASS, 6 FAIL, 1 BLOCKED**, exit code 1. All 26 boolean
egress checks passed over twelve inspected Research requests: four call-context
(including the failed HTTP request), eight delegated-label.

The 107 focused offline tests, Ruff lint/format, and mypy passed before deployment;
they are not substitutes for these hosted outcomes. No permission, infrastructure,
normal workflow guard, or session-isolation configuration changed. No model
inference, fault injection, direct-operator control, new Entra user, commit, or
push was performed. Preserve both this failed/incomplete run and the earlier
combined-binding findings rather than replaying until a green result appears.

**Genuine native-Entra-user Research control, 2026-10-21:** following explicit
owner authorization, two distinct authenticated Member users invoked the existing
Research 6 directly under their project-scoped Foundry Agent Consumer grants.
The setup and tests used isolated Azure CLI profiles. CLI account metadata and
Graph `/me` confirmed distinct principals before execution. The operator
credential read only deployed-version/routing/120-second-idle metadata; the
actual requests used the corresponding native-user credential with no fallback.

The [native-user harness](../../scripts/run_scenario/native_user_isolation.py)
reused the existing diagnostic operations, not normal workflow creation.
Each direction used a fresh random diagnostic run ID and synthetic one-hour
State Store markers. All **22 assertions passed**, exit code 0:

| Direct Research assertion | User A owns / B attempts | User B owns / A attempts |
|---|---|---|
| Both callers expose hosted call/user/session context | Passed | Passed |
| Owner creates and rereads the same marker | Passed | Passed |
| Other user's read of that item key | Marker absent | Marker absent |
| Owner continues its own response history | Passed | Passed |
| Other user supplies owner's previous response only | 404 `not_found`, exact owner resource confirmed | 404 `not_found`, exact owner resource confirmed |
| Other user also supplies owner's session ID | 403 `session_not_accessible` | 403 `session_not_accessible` |
| Owner continues after both attempts | Marker seen, original session, new response | Marker seen, original session, new response |
| Owner's state after the attempts | Original marker unchanged | Original marker unchanged |

The actual 22 outgoing Responses-create requests were inspected for absence of
`x-ms-user-identity`, forwarded `x-agent-foundry-call-id`, and client-supplied
`x-agent-user-id`. Two additional owner GETs confirmed the generic 404s referred
to existing completed responses. The harness handled acquired token values only
in memory, without returning, logging, or persisting them in run evidence.
Azure CLI retained its existing local authentication caches.

This establishes native direct-user isolation and history continuity for the
tested Research path. It does **not** establish hosted Supervisor propagation,
other specialists, normal owner/session registry authorization, artifact/files
access, protected downstream user permissions, idle/restart recovery, or model
quality. The existing nested delegated-label failures remain valid findings.
No model inference, deployment, RBAC change, login, fault injection, or automatic
request retry occurred during this control. The new harness is available through
**Agents: Test native Entra user isolation**; use the
[deployment guide](../deployment.md#test-genuine-native-entra-users) to repeat it.

**Local visibility refinement:** the native-user task subsequently enables
`--verbose` to display sanitized request/response panels, including actual HTTP
status/correlation metadata and validated diagnostic replies. Exchange events
are saved separately from assertion results; this does not change the 22-check
acceptance criteria or turn an HTTP rejection into a passing assertion by itself.
Mock-transport tests cover successful replies, expected rejections, server errors,
and exclusion of credentials/cookies/call IDs/error messages from output.
No additional hosted run was performed for this presentation-only increment;
the earlier saved evidence was not rewritten.

Adapt [the official A-A-B test][aab-isolation] for DLG-03. A controlled delegated-user
variant may intentionally share one sandbox to test history isolation independently
of sandbox routing; this does not make arbitrary shared files/caches user-isolated.
Keep a separate genuine-Entra-user variant, and distinguish administrative
session-management operations from end-user history/state operations.

Do not silently fall back to empty history, accept any exception as isolation
success, or retry model requests until the test happens to pass. Record expected
versus actual outcomes. Only the full behavior test can complete the delegation
validation milestone; a diagnostic flag or successful HTTP transport cannot.

Review this record when authorization semantics, runtime/protocol, state ownership,
or POC scope changes. Update dated findings without rewriting decision history;
use a new superseding ADR for a materially different accepted architecture.

## References

[hosted]: https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents
[runtime]: https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-contract#platform-request-headers-container-protocol-200
[state-store]: https://learn.microsoft.com/azure/foundry/agents/concepts/agent-state-store#caller-identity
[delegation]: https://learn.microsoft.com/azure/foundry/agents/how-to/isolate-sessions-per-user#isolate-sessions-for-your-own-users
[resilience]: https://learn.microsoft.com/azure/foundry/agents/concepts/long-running-agent-resilience
[september]: https://devblogs.microsoft.com/foundry/whats-new-in-microsoft-foundry-september-2026/
[sessions]: https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-sessions?pivots=azd
[agent-management]: https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-agent?pivots=rest
[python-isolation]: https://github.com/microsoft-foundry/foundry-samples/blob/41299fd5a460a9287e29c8414b1c027906cf5b6f/samples/python/hosted-agents/bring-your-own/responses/session-multiplexing/src/hello-world-session-multiplexing-python-responses/main.py
[aab-isolation]: https://github.com/microsoft-foundry/foundry-samples/blob/41299fd5a460a9287e29c8414b1c027906cf5b6f/samples/python/hosted-agents/bring-your-own/responses/session-multiplexing/src/hello-world-session-multiplexing-python-responses/scripts/invoke_previous_response_isolation.py
[a2a-sample]: https://github.com/microsoft-foundry/foundry-samples/tree/41299fd5a460a9287e29c8414b1c027906cf5b6f/samples/python/hosted-agents/agent-framework/a2a/01-delegation
[a2a-caller]: https://github.com/microsoft-foundry/foundry-samples/blob/41299fd5a460a9287e29c8414b1c027906cf5b6f/samples/python/hosted-agents/agent-framework/a2a/01-delegation/caller/src/agent-framework-a2a-caller-responses/main.py
[a2a-auth]: https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/agent-to-agent
[maf-user-isolation]: https://github.com/microsoft/agent-framework/tree/08851e69d722b0e0961c08eecf8575db995f0172/dotnet/samples/04-hosting/FoundryHostedAgents/responses/Using-Samples/UserIsolationAgent
[toolbox-context]: https://github.com/microsoft-foundry/foundry-samples/blob/41299fd5a460a9287e29c8414b1c027906cf5b6f/samples/python/hosted-agents/agent-framework/responses/25-claude-foundry-toolbox/src/claude-foundry-toolbox-responses/main.py
[sdk-user-isolation]: https://github.com/Azure/azure-sdk-for-python/blob/c95a09e1466cfaa69aafd1dc2a0808e2240edc21/sdk/ai/azure-ai-projects/samples/hosted_agents/sample_agent_user_identity_isolation.py
[legacy-isolation]: https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/isolate-sessions-per-user#isolation-keys-on-container-protocol-100-unsupported

- [Hosted agent runtime contract][runtime].
- [State Store caller identity and isolation][state-store].
- [Hosted session lifecycle and conversation binding][sessions].
- [Hosted agent/version management][agent-management].
- [Application-user delegation and its trust boundary][delegation].
- [Long-running agent resilience][resilience].
- [September 2026 feature announcement][september] (published October 15, 2026).

### Official sample references

Source-inspected on 2026-10-20/21 at the pinned revisions below; not deployed or run
as part of this research. Samples illustrate contracts, not proof that our current
implementation or pinned dependency set behaves identically.

| Sample | Useful pattern | Boundary / caution |
|---|---|---|
| [Python session-multiplexing handler][python-isolation] | Platform-authorized `get_history()` and explicit user/call-context checks | User-aware single-agent handler, not our complete Responses-to-Responses chain |
| [Python A-A-B isolation script][aab-isolation] | Authorized continuation followed by a cross-user attempt using `x-ms-user-identity` | Delegated labels are not two independently authenticated Entra principals; assert the intended denial, not any failure |
| [Projects SDK delegated-user isolation][sdk-user-isolation] | A creates, B gets `404 NotFound`, A continues using the same delegated label | Direct client, not a hosted Supervisor; sample deploys/routes a version and does not explicitly assert failure if B unexpectedly succeeds |
| [Python hosted A2A delegation][a2a-sample] | Hosted caller/executor through Toolbox and a remote-agent connection | Different transport/authentication arrangement; not a drop-in fix for the current bare Responses client |
| [MAF .NET UserIsolationAgent][maf-user-isolation] | Per-user AgentSession and explicit application-user delegation | .NET API example; explicitly distinguishes delegated labels from OAuth token exchange |
| [Python Foundry Toolbox client][toolbox-context] | SDK-managed opaque call-ID forwarding to a first-party service | Toolbox example, not evidence that our specialist state/history access is authorized |

The A2A sample uses `UserEntraToken`, while the [current A2A tool guide][a2a-auth]
recommends `agentic-identity` for Foundry targets. Reconcile the intended auth mode,
SDK/API versions, and permissions before adoption; do not make A2A mandatory here.
The [sample caller][a2a-caller] also includes an SDK workaround that falls back to
empty history after a fetch failure. Do not copy that workaround into our
continuity/isolation validation, where it would hide the behavior being tested.
