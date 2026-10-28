"""Reviewed SDK/route reference for consumer CI reports, never a reconstructed execution log."""

from scripts.check_details import FILES, HOSTED

type ApiEntry = dict[str, str]


def api_catalog() -> list[ApiEntry]:
    """Keep Consumer, backend, and privileged/native-attempt call sites visibly separate."""
    entries: list[ApiEntry] = []

    def add(
        identifier: str,
        group: str,
        title: str,
        sdk: str,
        method: str,
        route: str,
        caller: str,
        purpose: str,
        explanation: str,
        related: str,
        source: str,
    ) -> None:
        entries.append(
            {
                "id": identifier,
                "group": group,
                "title": title,
                "sdk": sdk,
                "method": method,
                "route": route,
                "caller": caller,
                "purpose": purpose,
                "explanation": explanation,
                "related": related,
                "source": source,
            }
        )

    runner = "scripts/run_scenario/consumer_code_interpreter.py"
    backend = "src/backend/app/services/consumer_code_interpreter.py"
    probes = "scripts/run_scenario/native_bypass.py"
    add(
        "consumer-session",
        "Consumer calls",
        "Create a Hosted session",
        "project.agents.create_session(...)",
        "POST",
        HOSTED + "/sessions",
        "user1 or user2, using that user's Consumer credential",
        "Allocate the user's execution session on the verified agent version.",
        "execute supplies a VersionRefIndicator for the verified deployment version. The returned "
        "agent_session_id is kept private and used for that user's commands and Session Files operations.",
        "Session setup before prepare and analysis",
        runner + " — execute",
    )
    add(
        "consumer-conversation",
        "Consumer calls",
        "Create a Hosted conversation",
        "hosted.conversations.create(...)",
        "POST",
        HOSTED + "/protocols/openai/conversations",
        "user1 or user2, using that user's agent-bound client",
        "Create an independent conversation used with the same user's session.",
        "execute keeps the returned conversation reference in that actor's private in-memory record. "
        "Conversation creation is not Code Interpreter execution.",
        "Session/conversation setup; subsequent application commands",
        runner + " — execute",
    )
    add(
        "consumer-command",
        "Consumer calls",
        "Send a protected application command",
        "actor.hosted.responses.with_raw_response.create(...)",
        "POST",
        HOSTED + "/protocols/openai/responses",
        "user1 or user2, using that user's Consumer credential",
        "Dispatch prepare, analyze, inspect, or artifact operations.",
        "Proof.command supplies the actor's conversation and agent_session_id plus the structured "
        "POC_CONSUMER_CI envelope. with_raw_response preserves the actual HTTP status. prepare, inspect, "
        "and artifact do not invoke the model; analyze triggers the separate backend native request.",
        "Prepare; analysis submission; authorized and cross-user application-reference checks",
        runner + " — Proof.command; src/backend/app/hosting.py — handle",
    )
    add(
        "consumer-upload",
        "Consumer calls",
        "Upload authorized input.xlsx",
        "actor.project.agents.upload_session_file(...)",
        "PUT",
        HOSTED + "/sessions/{session}/files/content?path={path}",
        "user1 or user2, using that user's Consumer credential",
        "Place the original synthetic workbook in the user's Hosted session.",
        "Proof.own_analysis passes the server-allocated relative input path and already-created XLSX bytes. "
        "The raw_response_hook records actual transport status. This is not the native Files API upload.",
        "Synthetic input upload; original-input hash/length comparison",
        runner + " — Proof.own_analysis",
    )
    add(
        "consumer-download",
        "Consumer calls",
        "Download original generated JSON or PNG",
        "actor.project.agents.download_session_file(...)",
        "GET",
        HOSTED + "/sessions/{session}/files/content?path={path}",
        "user1 or user2; actor and resource user may differ during a negative probe",
        "Retrieve bytes through Hosted Session Files authorization.",
        "Proof.session_download streams the exact recorded session/path under the acting user's client, "
        "with a byte limit and HTTP hook. Positive controls compare original hashes/lengths. A cross-user "
        "attempt uses the resource user's reference but the other Consumer's credential. No prompt is sent.",
        "Own result.json/chart.png delivery; opposite-user download denial; delivery rechecks",
        runner + " — Proof.session_download, Proof.owner_artifact, Proof.direction",
    )
    add(
        "consumer-response",
        "Consumer calls",
        "Retrieve a Hosted response",
        "actor.hosted.responses.with_raw_response.retrieve(...)",
        "GET",
        HOSTED + "/protocols/openai/responses/{response}",
        "user1 or user2, using that user's Consumer credential",
        "Check Hosted response/session access independently of file access.",
        "Proof.hosted_response retrieves the exact recorded response reference without another model turn. "
        "New instrumentation records the actual success status; older evidence may contain only the error "
        "status or SDK success. Missing legacy statuses are not invented.",
        "Hosted response positive controls and cross-user retrieval denial",
        runner + " — Proof.hosted_response",
    )
    add(
        "backend-client",
        "Backend calls",
        "Construct request-local native client",
        "project.get_openai_client(default_headers=..., max_retries=0, timeout=300)",
        "Local construction",
        "No HTTP request is implied by constructing the client",
        "Backend managed credential plus trusted platform call context; not a Consumer bearer token",
        "Bind native calls to one authorized backend request without shared-header mutation.",
        "consumer_turn authorizes trusted caller/session/workflow bindings first. It then constructs the "
        "client using get_request_context().platform_headers(). The opaque call header is forwarded only "
        "from trusted context; credentials, header values and private references are not shown.",
        "Backend analysis path; trusted-context and no-fallback boundaries",
        backend + " — consumer_turn",
    )
    add(
        "backend-upload",
        "Backend calls",
        "Upload authorized original bytes to native Files",
        "client.files.create(purpose='assistants', file=...)",
        "POST",
        FILES + "/files",
        "Authorized backend credential; not user1/user2's direct native Files client",
        "Make only the approved XLSX bytes available to the native tool.",
        "ConsumerWorkflow.analyze reads the authorized Hosted input, checks workbook/byte limits, seals "
        "submitted state, uploads the bytes, and journals the returned native input ID. The native ID "
        "does not become proof of Consumer ownership or a normal download reference.",
        "Backend analysis; original input binding; sealed test-control input lineage",
        backend + " — ConsumerWorkflow.analyze",
    )
    add(
        "backend-analysis",
        "Backend calls",
        "Invoke native managed Code Interpreter",
        "client.responses.create(...)",
        "POST",
        FILES + "/responses",
        "Request-local backend client with its credential and trusted platform context",
        "Ask the model to generate Python that produces result.json and chart.png.",
        "ConsumerWorkflow.analyze submits the fixed ANALYSIS_PROMPT and exactly one native code_interpreter "
        "tool with an automatic container and authorized uploaded input. Limits include max_tool_calls=1, "
        "max_output_tokens=4000 and store=False. Prompt receipts describe what the backend submitted; "
        "the catalog itself is not a wire capture or model execution result.",
        "Backend execution receipt; numerical/marker checks; generated-output inventory",
        backend + " — ConsumerWorkflow.analyze",
    )
    add(
        "backend-output",
        "Backend calls",
        "Retrieve original native outputs",
        "client.containers.files.content.with_streaming_response.retrieve(...)",
        "GET",
        FILES + "/containers/{container}/files/{file}/content",
        "Authorized backend request-local client",
        "Retrieve—not regenerate—the original JSON and PNG bytes.",
        "original_outputs accepts one completed native call, validates SDK-native citations against that "
        "call's container and allowlisted output paths, then streams bounded original bytes. analyze "
        "stages unchanged bytes in Hosted HOME and records receipts. No model-written URL is trusted.",
        "Original-output staging and hash/length receipts; later Consumer Hosted downloads",
        backend + " — original_outputs, ConsumerWorkflow.analyze",
    )
    for kind, sdk, route, purpose in (
        (
            "input-metadata",
            "client.files.with_raw_response.retrieve(...)",
            "/files/{file}",
            "Read exact native input metadata.",
        ),
        (
            "input-content",
            "client.files.with_streaming_response.content(...)",
            "/files/{file}/content",
            "Request exact native uploaded-input bytes.",
        ),
        (
            "container-list",
            "client.containers.files.with_raw_response.list(...)",
            "/containers/{container}/files",
            "Request a bounded native container file listing.",
        ),
        (
            "output-metadata",
            "client.containers.files.with_raw_response.retrieve(...)",
            "/containers/{container}/files/{file}",
            "Read generated JSON/PNG native-file metadata.",
        ),
        (
            "output-content",
            "client.containers.files.content.with_streaming_response.retrieve(...)",
            "/containers/{container}/files/{file}/content",
            "Request original generated JSON/PNG native bytes.",
        ),
    ):
        for control in (False, True):
            group = "Privileged-control calls" if control else "Consumer native bypass attempts"
            add(
                ("control-" if control else "bypass-") + kind,
                group,
                purpose.rstrip("."),
                sdk,
                "GET",
                FILES + route,
                "privileged-control: separately approved existing operator credential"
                if control
                else "user1 or user2: Consumer-only credential, deliberately targeting "
                "the opposite user's native reference",
                purpose,
                (
                    "native_bypass.request resolves exact sealed-lineage references in harness memory. "
                    "Positive metadata/list controls validate IDs; content controls compare original byte "
                    "hashes/lengths. These reads establish live resources before and after the negative attempt, "
                    "not isolation between privileged project users."
                    if control
                    else "native_bypass.request uses the acting Consumer's native client and the opposite user's "
                    "exact sealed-lineage reference. Metadata/listing success is an unexpected access result; "
                    "successful foreign content streams are closed without reading/logging the payload. "
                    "This is a direct API attempt: no prompt, new analysis, permission grant or fallback."
                ),
                "Expanded native " + kind + " checks in both directions",
                probes + " — request, direction",
            )
    add(
        "consumer-native-list",
        "Consumer native bypass attempts",
        "List project native Files",
        "other.files.files.with_raw_response.list(limit=1)",
        "GET",
        FILES + "/files?limit=1",
        "user1 or user2, using only that user's Consumer credential",
        "Check native project-file listing privilege, separately from exact-file content access.",
        "Proof.native_listing attempts one bounded listing. This is a project privilege-boundary probe; "
        "it does not use an exact foreign file, has no matching resource existence control, and cannot "
        "substitute for input/output content-denial checks.",
        "Native Files privilege boundary",
        runner + " — Proof.native_listing",
    )
    return entries


def related_api_ids(name: str, check_id: str | None, execution: dict[str, object]) -> list[str]:
    """Map implementation references only; never infer that a catalog entry was executed."""
    operation = check_id.split(":", 1)[-1] if check_id else ""
    lower = name.lower()
    if operation.startswith("native-"):
        kind = operation.removeprefix("native-").split(":", 1)[0]
        if kind in {"input-metadata", "input-content", "container-list", "output-metadata", "output-content"}:
            return ["bypass-" + kind, "control-" + kind]
        return []
    family = execution.get("api_family")
    if execution.get("execution_type") == "http_request":
        if family == "Hosted Session Files":
            return ["consumer-upload" if execution.get("method") == "PUT" else "consumer-download"]
        if family == "Hosted Responses":
            return ["consumer-response"]
        if family == "Native Files" and "/files?" in str(execution.get("request_template")):
            return ["consumer-native-list"]
    if execution.get("execution_type") == "application_command":
        return (
            ["consumer-command", "backend-client", "backend-upload", "backend-analysis", "backend-output"]
            if '"operation":"analyze"' in str(execution.get("request_template"))
            else ["consumer-command"]
        )
    if "backend execution receipt" in lower:
        return ["backend-client", "backend-upload", "backend-analysis", "backend-output"]
    if "original input binding" in lower:
        return ["consumer-upload", "backend-upload"]
    if any(term in lower for term in ("original bytes", "png bytes", "analysis values", "output size")):
        return ["backend-analysis", "backend-output", "consumer-download"]
    if "deployed runtime" in lower:
        return ["consumer-command"]
    return []
