# Explore Human-in-the-Loop Tool Approval (#6)

> **Disclaimer**
>
> This document is provided "as is" for general informational purposes. It is
> not official Microsoft guidance, a warranty or a commitment. Azure features
> and preview behavior can change; verify capabilities against current
> Microsoft Learn documentation. These POC results do not establish an SLA
> or production security certification.

Status report: what works, what does not and why, and next steps for each
requirement of [issue #6](https://github.com/wxifaras/hosted-agents-poc/issues/6).

**Conclusion:** approval and rejection work through the Hosted Agent, and
also work without toolbox against a custom Entra MCP: approval produced one
independently observed execution; rejection produced none. Another authenticated
user cannot decide the owner's operation. However, an
authorized caller can execute the toolbox tool directly without approval.
**The evaluation is complete within the tested scope; universal approval
enforcement is not achieved.**

| Field | Scope |
|---|---|
| Report date | 2026-10-30 |
| Environment | `hosted-agents-poc-dev`, `northcentralus`, `gpt-5-mini` |
| Agent scenario | Dedicated Story6 Hosted coordinator calls unchanged Research for context, then requests approval for the selected MCP tool |
| Tool boundary | Direct custom Entra MCP with one synthetic receipt tool; separately tested authenticated Foundry toolbox proxy over public Learn |
| Data and actors | Synthetic case IDs/actions, fixed Learn query, synthetic HOME file, operator and two distinct authenticated test users |
| Existing stories | Story1/3/4 agents, toolboxes and behavior were preserved |

**Final-results HTML deep dive:**
[View with HTML Preview](https://htmlpreview.github.io/?https://github.com/wxifaras/hosted-agents-poc/blob/feature/story6-tool-approval/docs/reports/story-6/human-in-the-loop-approval/final-results/index.html)
The GitHub preview becomes available after this snapshot and the branch are
committed and pushed. This is one outcome-focused report across all five
criteria, not a deployment history. Its script reads the summary below and
explicitly selected sanitized evidence; the direct-call enforcement FAIL
remains visible. Expand each **What this task validates** section for its
test-flow diagram, actors and exact recorded-check links. These are reviewed
task plans with evidence badges, not reconstructed execution traces.

**Evidence in the single final report:**
[Approval/rejection](reports/story-6/human-in-the-loop-approval/final-results/index.html#criterion-1)
| [Two-user access](reports/story-6/human-in-the-loop-approval/final-results/index.html#criterion-5)
| [Direct-call boundary](reports/story-6/human-in-the-loop-approval/final-results/index.html#criterion-4)
| [Conversation history](reports/story-6/human-in-the-loop-approval/final-results/index.html#criterion-3).
Technical configuration and execution bindings remain in those reports rather
than an implementation history here.

**Status:** Works = demonstrated on the hosted path; Partial = useful behavior
with explicit gaps; Gap = the required control did not prevent the tested action.
Completing an evaluation does not turn a failed enforcement check into a pass.

## Summary

| # | Requirement | Status | What works | What does not work, and why | Next step |
|---|---|---|---|---|---|
| 1 | Approve/reject a multi-agent MCP action; identify enforcement ownership | Works, scoped | Coordinator delegates context to Research and exposes the actual pending request. Direct Entra MCP without toolbox independently recorded one approved execution and no rejected execution; Learn/toolbox approval also worked | The provider approval path is not a shared server-side gate covering every MCP caller | Use this interaction pattern where callers cannot bypass the required runtime |
| 2 | Discover pending approvals, submit decisions, and assess notification options | Works, with notification limitations | A fresh client discovers the pending operation through custom `inspect`; explicit decisions resume it. Polling, webhook and Event Grid options were assessed | No native approval-specific webhook/Event Grid contract was established; generic completion is not approval | Keep the tested inspect/decide path; add notifications only if needed |
| 3 | Observe execution, conversation/session and files during pause; resume | Works, scoped | Actual stored conversation items progress from empty to pending, inspected and completed. The same operation/session continues and the original HOME file/hash remains | This does not prove interpreter RAM survival, arbitrary file retention or injected process-loss recovery | Reuse the observed state/history/file behavior; keep process recovery under #7 |
| 4 | Call MCP directly with a valid token but no approval | Gap demonstrated | Ordinary authorized direct access works, making the no-approval probe meaningful | The direct call returns a result despite `require_approval=always`; the proxy does not enforce that flag on `tools/call` | Accept/document the boundary, or separately design shared enforcement if all routes must be gated |
| 5 | Verify authentication, permissions, correlation and unauthorized approver | Works, with identity/trace limitations | Distinct real users, foreign approval denials, owner continuation, allowed proxy access and Consumer-user HTTP403 were demonstrated | No enterprise approver-role system, downstream user delegation or joined tool-side user trace was established | Retain scoped ownership/permission controls; define stronger identity requirements separately |

## Acceptance review

These are **evaluation-coverage verdicts**, not claims that every security
control passed. Coverage includes the custom Entra MCP approve/reject cases
and the separate toolbox, two-user and history observations. The GitHub issue
has not been closed.

| Issue criterion | Coverage verdict | Evidence | Reproduction tasks | Ownership and acceptance boundary |
|---|---|---|---|---|
| 1. Approve/reject a multi-agent tool action; identify enforcing component and shared-enforcement feasibility | Met within tested scope | [Approval/rejection and authenticated call](reports/story-6/human-in-the-loop-approval/final-results/index.html#criterion-1) | Story6 MCP hosted task: start/inspect/approve; separate start/reject; compare tool receipts. Step2 also covers Learn/toolbox | Foundry-managed native provider approval; custom coordinator owns binding. Direct Entra MCP independently confirmed execution counts; Entra authenticates the workload, not human approval. |
| 2. Discover pending approval, submit a decision and assess Event Grid/webhook/polling | Met as an evaluation | [Discovery and stored decision linkage](reports/story-6/human-in-the-loop-approval/final-results/index.html#criterion-2), plus [notification assessment](#notification-availability) | Step3 inspect; Step2 explicit decision | Tested custom polling/commands backed by platform state. Other notification capabilities are documentation assessments, not live integrations; no native approval-event guarantee. |
| 3. Observe execution, conversation/session and generated files during pause; resume | Met within observed state/file scope | [Actual history and file continuity](reports/story-6/human-in-the-loop-approval/final-results/index.html#criterion-3) | Step2 `--capture-history`; Step4 inspect; Step2 explicit decision | Actual stored history, pinned binding, pending/terminal state and backend-generated HOME file observed. Provider counts are not independent effects; interpreter RAM, arbitrary artifacts and crash recovery are not claimed. |
| 4. Direct valid-token/no-approval test; record blocking, enforcing component and bypass | Met as an evaluation; enforcement **FAIL** | [Direct proxy boundary](reports/story-6/human-in-the-loop-approval/final-results/index.html#criterion-4) | Step5 with the fixed isolated toolbox and existing Consumer profile | Authenticated Foundry proxy returned an unapproved result; approval flag is not a server-side gate. Downstream Learn is public. The failed control remains visible. |
| 5. Verify callers/approver, permissions, authorization and correlation; allowed/denied access and unauthorized approver | Met within application ownership/proxy scope, with explicit trace limitations | [Two-user, proxy-permission and caller-correlation evidence](reports/story-6/human-in-the-loop-approval/final-results/index.html#criterion-5) | Step6 with existing profiles; Step5 permission control; audit-enabled Steps2/4 | Foundry enforces sessions/proxy permissions; custom coordinator/platform state enforces operation ownership. Authenticated API caller is distinct from human chat review. Downstream user delegation and joined tool-side tracing remain unverified. |

Task details are in the [operator guide](tasks-detailed.md#story-6-approval-qualification-current-implementation).
Exact configuration/API/SDK and execution bindings remain in the linked
evidence. Local unit tests are not substituted for hosted observations, and
untested behavior is not classified as unsupported.

## 1. Approval and rejection through Hosted Agents

**Question:** Can a multi-agent tool action pause for human approval and then
execute or be rejected, without putting approval logic in every specialist?

**What works**

- The dedicated coordinator calls the existing Research Hosted Agent for
  context. The coordinator owns the subsequent approval-required MCP action;
  Research does not implement approval handling.
- Native Responses MCP with `require_approval=always` returns an actual pending
  request containing the proposed server, tool and arguments.
- The client displays the fixed Learn search for human review.
- An explicit approval completes the original operation with **one
  provider-reported MCP call**.
- An explicit rejection completes a separate operation as `rejected` with
  **zero provider-reported MCP calls**.
- The authenticated toolbox route also pauses and completes after approval.
- The direct custom Entra MCP route, **without toolbox**, passed both cases
  with independent tool-server observations:

| Case | During approval pause | After explicit decision | Independent server evidence |
|---|---|---|---|
| Approve | Pending, zero reported tool calls, server total `0 -> 0` | Completed, one reported tool call, server total `0 -> 1` | Exactly one execution receipt matching the approved case |
| Reject | Separate pending case, zero reported calls, server total `1 -> 1` | Rejected, zero reported calls, server total stays `1` | No receipt for the rejected case; same epoch and unchanged global total |

Both custom-MCP cases used the same single-process server epoch and sequential
requests. A fresh-client inspect confirmed the pending state without execution.
Rechecking a pending operation does not supply a human decision: it remains
pending until the requester explicitly approves or rejects.

**What does not work, and why**

This only gates calls using the selected provider/runtime path. An authorized
direct toolbox caller can bypass that path, as demonstrated in section 4.
Provider-reported calls alone are not an execution ledger. The separate custom
MCP cases additionally have server counts and matching receipts; they do not
establish a shared server-side approval gate or original-user delegation.

**Next step:** use the coordinator interaction where appropriate, but do not
describe it as universal shared approval enforcement.

## 2. Pending approval discovery, decisions and notifications

**Question:** How does a client discover a pending approval and submit a decision?

**What works**

- A fresh client invocation uses `inspect` to read the same authoritative
  pending operation, rather than relying on an in-memory client cache.
- Inspection creates no new model/tool request or replacement operation.
- The requester can explicitly approve or reject its bound pending request.
  Self-approval is allowed by this POC's policy.
- Provider continuation references remain private. Clients receive a
  structured pending reference, tool and fixed arguments (Learn query or the
  synthetic action's case ID).

### Client contract

The custom `poc-story6-approval-v1` JSON response exposes the operation's
`phase`, `pending` request and `decision`. Use the same authenticated
coordinator conversation/session and saved case locator.

| Command | Behavior |
|---|---|
| `start` | Creates one new operation and stops at pending approval; never auto-approves |
| `inspect` | Reads the existing operation and file state without model/tool execution |
| `decide` | Submits the explicit bound approve/reject decision |

Outer Responses `completed` means the **command** finished, not that the
logical operation was approved. Check its reported `phase`. Ambiguous
submissions are retained and are not automatically replayed.

### Notification availability

| Mechanism | What is established | Limitation |
|---|---|---|
| Initial response | Actual native pending approval is returned on the hosted path | Requires the client to inspect the structured result |
| Polling/inspection | Custom, model-free `inspect` works from a fresh client | Application-owned discovery, not a native notification feed |
| Webhooks | Azure OpenAI documents response lifecycle webhooks | Reviewed event types do not list an MCP approval-specific webhook; no webhook was configured/tested here |
| Event Grid | Official sources were assessed | No native Foundry approval-event contract was established. ARM resource events describe management changes, not data-plane approval |
| Custom event publication | An application could publish its own pending/decision changes | Additional custom implementation, not a Foundry guarantee; not needed for this POC |

**What does not work or remains unverified**

A generic `response.completed` notification cannot establish approval or tool
execution: a command can complete while the logical operation is pending.
Native approval-specific webhook/Event Grid support remains unverified, not
universally classified as unsupported.

**Next step:** retain the tested inspect/decide flow. No additional notification
service is required to meet the evaluation's assessment scope.

## 3. Execution, state and files during an approval pause

**What works**

- Before approval, the operation stays pending with no decision and zero
  provider-reported MCP calls.
- The same operation/session/conversation binding continues after the decision;
  the client does not resubmit it as new work.
- The original backend-generated synthetic HOME file retains its hash before,
  during and after the pause.
- A changed host boot marker was observed while the pending state and file
  remained available.
- Foreign-user attempts did not alter the owner's pending request or file.

**Actual conversation observations**

These are real reads of the stored Hosted conversation items, not counts
inferred from the number of requests or reconstructed from a completed case.

| Point | Stored items | Observed content |
|---|---|---|
| Before start | 0 | New conversation was empty |
| Pending approval | 2 | Start command and actual pending reply; no decision, zero reported MCP calls |
| During pause, after fresh-client inspect | 4 | Same start/pending items plus inspect command/pending reply; still no decision or tool execution |
| After human approval | 6 | Same items plus explicit decision command/completed reply; decision reference matches the stored pending item; reported call counts progress `0, 0, 1` |

All observed command/reply operation references matched, and the authenticated
requester, conversation and session aliases remained the same across reads.
Inspect adds command/reply items; it does not create another model/tool request.
Raw transcript text and private references are not exported.

**What is not established**

- A boot marker is not a physical process trace or proof of arbitrary crash
  recovery. No forced restart or process-loss injection was performed.
- The file is a Hosted HOME sentinel, not an interpreter file or proof of
  interpreter RAM, arbitrary generated-artifact lifetime or indefinite storage.
- Retained state does not establish exactly-once remote effects.

**Next step:** preserve this scoped result. Process recovery around pending
approval belongs to #7, not a repeat of Story1 storage/capacity tests.

## 4. Direct valid-token MCP access without approval

**Question:** Does the authenticated MCP endpoint itself reject an unapproved
call from an otherwise authorized caller?

**Answer: no, on the tested Foundry toolbox proxy.**

| Check | Result | Interpretation |
|---|---|---|
| Exact toolbox definition and MCP metadata | `require_approval=always` | The test targeted an approval-required tool configuration |
| Ordinary authorized direct call | Successful search result | Positive control: the valid-token caller has tool access |
| Direct call without an approval message | Successful search result | **Approval enforcement FAIL** at the direct proxy boundary |
| Existing invoke-only Consumer user calls the proxy | HTTP403 | Ordinary resource authorization works; this is not approval enforcement |

**Why it does not block**

The [official hosted toolbox guide](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/use-toolbox-hosted-agent#enforce-tool-approval)
states that toolbox `tools/call` does not block when `require_approval` is
`always`; the consuming runtime must enforce the setting before invocation.
The live result matches that documented boundary.

**Scope of this direct-call probe:** the endpoint is an Entra-authenticated
Foundry proxy over public Learn. No credential was sent directly to public
Learn. The separate custom Entra MCP was tested for hosted approve/reject, not
as a new direct-call bypass probe. It authenticates/authorizes the workload but
has no human-approval-proof validation; do not infer universal enforcement from
the hosted passes. No enterprise business tool was tested.

**Next step:** treat the bypass as an architecture finding. If every authorized
route must require human approval, separately design a shared server/gateway
control or prevent access outside the enforcing runtime. Do not silently add
per-agent workarounds or relabel the failed control as passing.

## 5. Caller, approver, permissions and correlation

**What works**

- Two existing isolated CLI profiles were verified as distinct authenticated
  Entra users. Both already had project-scoped Foundry Agent Consumer access;
  no new grants, sign-in or privilege elevation was needed.
- User A created a real pending operation.
- User B reached coordinator handling in B's own authorized context, proving
  that the negative tests were not merely failed authentication.
- A remained pending after B's attempts, then successfully continued after the
  user's explicit approval submitted through A's original credential.

| Access case | Observed result | Enforcing boundary |
|---|---|---|
| B inspects A's session | HTTP403 `session_not_accessible` | Foundry session authorization |
| B submits A's decision using A's session | HTTP403 `session_not_accessible` | Foundry session authorization |
| B inspects A's operation in B's own session | `operation_not_visible` | Coordinator/user-isolated state ownership |
| B uses A's valid approval reference in B's own context | `operation_not_visible` | Approval reference does not grant ownership |
| A inspects after B's attempts | Same pending request/file; no decision; zero reported calls | Owner continuity positive control |
| A explicitly approves | Original operation completes with one reported MCP call | Authenticated owner continuation |
| Workload/operator accesses authenticated toolbox | Allowed | Foundry proxy token/permission checks |
| Consumer user accesses toolbox directly | HTTP403 | Foundry proxy resource authorization |
| Hosted workload accesses custom Entra MCP | Native discovery and approved action succeed; unauthenticated direct request gets HTTP401 | Custom API audience/signature/issuer/lifetime, actual workload principal and Invoke-role checks |

**Caller-to-tool correlation**

| Link | Observed evidence |
|---|---|
| Authenticated requester -> API caller | Actual CLI account and authenticated Graph signed-in-user matched; per-operation caller alias was unchanged for start, inspect and decision |
| Caller -> conversation/session/operation | Pinned Hosted bindings and matching aliases across all three commands; no raw identifiers exported |
| Operation -> pending approval | Actual stored pending reply belongs to that operation |
| Pending approval -> human decision | Stored decision reference matches the actual stored pending reference; explicit decision is `true` |
| Decision -> terminal tool observation | Same conversation stores the completed reply reporting one MCP call on the authenticated toolbox route; original file retained |
| Research/downstream original user | Research reports no nested original-user context; downstream original-user authority and an independent tool invocation span remain unverified |

These history/toolbox links are **application-protocol correlation**, not a
joined distributed trace. Separately, the custom MCP execution receipt matches
the approved case, while the rejected case has no effect; this is independent
execution evidence, not downstream end-user authority. A correlation alias does
not grant authorization. Private raw bindings remain local, and human chat
review is distinct from the authenticated API decision caller.

**What is not established**

- Coordinator ownership is not an enterprise approver-role system.
- Human review happened in chat; the API decision used the authenticated
  requester credential. Chat itself was not an Entra-authenticated approval UI.
- Research reported no nested original-user context. Correlation and operation
  binding do not prove downstream user delegation or a joined distributed trace.
- Authentication and permissions are independent of human approval, as the
  direct-call result demonstrates.

**Next step:** retain the demonstrated boundaries. Add enterprise approver
roles, delegated identity or a protected downstream business tool only if
those become explicit requirements.

## Capability ownership

| Capability | Owner / assessment |
|---|---|
| Native MCP pending request and approved continuation | Foundry-managed provider behavior on the tested hosted path |
| Approval configuration and SDK serialization | Agent Framework / SDK-managed |
| Client inspect/decide API, owner binding and evidence tooling | Custom implementation (Dev Team), backed by platform state |
| Foreign session denial and toolbox resource permissions | Foundry-managed |
| Approval enforcement on direct toolbox `tools/call` | Not enforced on the tested route; documented and observed gap |
| Custom Entra MCP authentication and synthetic receipts | Custom implementation (Dev Team), using Entra-signed workload tokens; human approval remains runtime-managed |
| Native approval-specific Event Grid/webhook events | Unverified; not labeled universally unsupported |
| Enterprise downstream MCP authorization / original-user delegation | Not established by this POC |

## Conclusion and next steps

Hosted approval/rejection against a custom Entra MCP without toolbox,
discovery, pause/state/file continuity and
unauthorized-user ownership controls are demonstrated. The direct-call
approval gate is **not** enforced by the toolbox endpoint.

1. Review and accept the scoped findings, including the enforcement gap.
2. Use the existing [Story6 operator tasks](tasks-detailed.md#story6-human-decision-flow),
   [two-user flow](tasks-detailed.md#story6-two-user-unauthorized-approver-flow)
   and [direct proxy test](tasks-detailed.md#story6-authenticated-proxy-setup-and-direct-call-test)
   to reproduce the selected cases.
   The [simple Entra MCP flow](tasks-detailed.md#story6-simple-custom-entra-mcp-test-no-toolbox)
   reproduces approval/rejection with independent execution evidence.
3. Keep existing Story1/3/4 behavior unchanged; no broad retest or extra service
   is required merely to close this evaluation.
4. Treat universal approval enforcement and #7 process recovery as separate
   follow-on decisions, not capabilities implied by these results.

## Final validation sign-off

- Focused fixture/authentication, coordinator, client, packaging, editor and
  report regression checks passed.
- Repository-configured mypy passed; targeted Ruff lint
  and formatting checks passed.
- The single final report retains ten selected observations containing
  **37 PASS checks and 1 FAIL**. The FAIL is
  `Direct valid-token call without approval` in the proxy-boundary evidence.
  These are recorded checks, not 38 independent end-to-end scenarios.
- The actual direct-Entra hosted approval and rejection cases passed with
  independent same-epoch server evidence. Only the dedicated coordinator was
  updated; Supervisor, Research and Data Analysis remain unchanged.
- Existing toolbox, two-user and history results retain their original
  evidence bindings; local checks do not recertify every previous story.

**Human review:** the requester explicitly approved the custom MCP's first
pending action and explicitly rejected the second. The direct toolbox
enforcement FAIL and limitations remain visible.
**Pending:** final report review, publication and issue update. Test decisions
do not imply a commit, push, PR, closure of #6 or authorization for a custom
enforcement redesign.

## Regenerate the final-results HTML

Run **Agents: Build Story6 final-results HTML**, or:

```powershell
uv run --frozen --no-sync --group validation python -m scripts.story6_report --overwrite
```

The [Story6-specific generator](../scripts/story6_report.py) uses the shared
reporting redaction/schema helpers, its dedicated presentation template and
the retained [sanitized evidence fixture](validation/story6-final-evidence.json).
Older report versions are not generator inputs and are not retained under
`docs/reports/story-6`. Task flows use the shared SVG renderer and
evidence-mapping template used by the other stories. Missing checks stay
**NOT RECORDED**, and the direct-call node retains its **FAIL** badge.
No local private run configuration, Azure
credentials, new tests or hosted calls are required after cloning the
published branch. Missing or incompatible evidence stops generation.

The output is the fixed `final-results` snapshot linked above, with downloadable
sanitized evidence. `--overwrite` permits repeat generation only for unchanged
Markdown, template and evidence; use `--output artifacts\story-6\<fresh-folder>`
when the inputs change, then review the replacement explicitly. Generating
HTML does not change any acceptance or original check verdict.

## Evidence notes and references

The single final HTML report retains selected sanitized original checks and their
configuration bindings. Results are scoped observations across the recorded
scenarios, not a claim that every criterion was rerun on one deployment.
Private credentials, profiles, session/conversation IDs and case locators are
not included. Local tests support implementation correctness; hosted findings
above come from actual deployed calls.

- [Foundry MCP integration](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/model-context-protocol)
- [Hosted toolbox approval enforcement](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/use-toolbox-hosted-agent#enforce-tool-approval)
- [Azure OpenAI webhook event reference](https://learn.microsoft.com/azure/foundry/openai/how-to/webhooks#webhook-events-reference)
- [Event Grid system topics](https://learn.microsoft.com/azure/event-grid/system-topics)
- [Resource-group Event Grid events](https://learn.microsoft.com/azure/event-grid/event-schema-resource-groups)
