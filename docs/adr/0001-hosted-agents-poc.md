# ADR-0001: Hosted Agents POC architecture and evidence boundaries

| Field | Value |
|---|---|
| Status | Accepted POC baseline; authorization-guard alignment remains proposed |
| Decision owner | POC owner and maintainers |
| Recorded / status reviewed | 2026-10-20 |
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

**Proposed correction, not implemented by this documentation change:** separate
State Store authorization requirements from container-local user-ID requirements.
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
validated: our guard rejects a missing nested user ID before State Store
authorization is exercised. This is not a proven Foundry limitation. Analysis
tools and automatic recovery are not implemented.

| Capability | Implementation | Observed evidence / remaining validation |
|---|---|---|
| Python/uv, core modules, packaging, Rich scripts | Implemented | Local import/readiness, packaging, lint/types/tests exercised |
| Bicep foundation and operator access | Implemented | Provisioning, model, developer Blob/monitoring probes observed; isolated NSP-denial checks pending |
| Three independent hosted model agents | Implemented | Last all-three hosted smoke passed on version 4; not an orchestration test |
| Current deployment snapshot | Supervisor 6, Research 5, Data Analysis 4 | Targeted diagnostics updated only the relevant services; rediscover versions before testing |
| Idle policy: 120 seconds | Configured | Raw deployed metadata verified; does not prove file restoration or exact deprovision timing |
| Random durable mappings/registry | Implemented | Supervisor readback/reuse and unknown-workflow rejection observed; restart and two-user proof pending |
| Supervisor delegation and child histories | Partial | Research reference persisted before failure; both specialists' completed histories/final result unverified |
| Context diagnostic | Implemented | Only nested user ID absent; adding the delegation header did not change that observation |
| CSV/XLSX analysis and dataset generation | Not implemented | Parse/filter/group/statistics, derived data, and visualization still required |
| ArtifactStore/Blob references | Not implemented | Storage exists; application I/O, checksums, and authorization still required |
| Capacity harness | Not implemented | Measure progressive workloads at 0.5/1, 1/2, and 2/4 vCPU/GiB; no measured boundary yet |
| Session filesystem idle/resume | Not validated | Upload, derive, idle/resume same session, verify hashes, continue analysis |
| MAF checkpoints and crash recovery | Not implemented | Stored background execution, process replacement, original-result completion, and replay safety |
| Reconnect/cancellation/stream replay | Not implemented | Distinguish client disconnect from process loss; advanced steering remains optional |
| User/session/specialist isolation | Not validated end to end | Genuine users, unrelated histories/files/state/artifacts, and negative authorization cases |
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
| Hosted-agent Toolboxes (GA) + Tool Search (GA) | Planned Research experiment | Not connected; require approved read-only tool discovery/execution and traces |
| [Long-running hosted-agent resilience (public preview)][resilience] | Planned core recovery experiment | Not enabled; meaningful checkpoints and opt-in failure tests required |
| Insights (public preview) | Optional after repeatable traces | Not used; findings require human review, not automatic changes |
| A2A (GA) | Optional interoperability experiment | Not used; Responses delegation is not A2A interoperability proof |
| Routines (GA) | Candidate for scheduled runs/reporting | Not used; no schedule or recurring spend enabled |
| Model Router session affinity/routing metadata (preview) | Optional routing comparison | Not used; keep the explicitly selected model until separately reviewed |
| Cross-session semantic memory | Optional, separate experiment | Not used; Foundry/Cosmos choices need their own compatibility and isolation checks |

MAF/uv are the framework/toolchain; ACR, Blob, NSP, and Azure Monitor are supporting
Azure services, not additional Foundry features. Managed Code Interpreter would
be a separate experiment. Voice, Foundry Local, model-catalog upgrades, a frontend,
production load testing, and multi-region/private-network production design are
not automatically added because a release announcement mentions them.

## Next validation increments

1. Align the authorization guard with the documented contract; prove hosted
   State Store access and missing/mismatched-scope rejection without weakening
   isolation. Then complete both specialist calls and distinct-history follow-up.
2. Build a tiny synthetic CSV analysis with a real derived artifact; add XLSX
   and plotting. Analysis logic can progress in parallel with authorization work.
3. Add scoped artifact I/O, actual file idle/resume checks, and measured capacity
   progression across the three tiers.
4. Add MAF phase checkpoints, stored-background reconnect, and separately
   authorized process-loss/re-entry/idempotency experiments.
5. Complete cross-user/session/specialist denial tests, Research tools, and
   trace-backed findings. Keep A2A, semantic memory, HITL, advanced steering,
   and optional Insights/evaluation work outside the initial success gate.

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

- [Hosted agent runtime contract][runtime].
- [State Store caller identity and isolation][state-store].
- [Long-running agent resilience][resilience].
- [September 2026 feature announcement][september] (published October 15, 2026).
