"""Safe operation explanations shared by live reporting and offline legacy presentation.

Descriptions explain implementation intent; they are not reconstructed HTTP receipts.
No native/session identifiers, credentials or dynamic user payloads belong here.
"""

from typing import Literal

type CheckDetails = dict[str, str]


HOSTED = "/api/projects/{project}/agents/{agent}/endpoint"
FILES = "/api/projects/{project}/openai/v1"


def describe_check(
    name: str, check_id: str | None = None, *, basis: Literal["recorded", "implementation"] = "recorded"
) -> CheckDetails:
    """Classify the actual operation, without inventing its prompt or observed HTTP outcome."""
    lower = name.lower()
    operation = check_id.split(":", 1)[-1] if check_id else ""
    details: CheckDetails = {
        "execution_type": "local_assertion",
        "purpose": "Compare the recorded evidence with this check's expected condition.",
        "api_family": "Local evidence verification",
        "method": "Not applicable",
        "route_template": "Not applicable",
        "request_template": "No network request for this local comparison.",
        "authentication_context": "No credential needed for the local comparison.",
        "prompt_applicability": "Not applicable: this check does not invoke a model.",
        "prompt": "No prompt is sent by this operation.",
        "controls": "Use the separately recorded input/runtime/byte receipts; do not infer missing evidence.",
        "pass_meaning": "Only this recorded comparison satisfied its expected condition.",
        "limitations": "Not proof of VM isolation, privileged-user isolation, or another API's authorization.",
        "evidence_basis": (
            "Description recorded by this runner before execution; outcomes are separate evidence."
            if basis == "recorded"
            else "Implementation explanation for a legacy check, not a captured request or reconstructed outcome."
        ),
    }
    native = operation.startswith("native-")
    download = operation in {"foreign-json", "foreign-png"} or "download" in lower
    if native or download or operation == "listing" or "native files privilege boundary" in lower:
        details.update(
            execution_type="http_request",
            authentication_context="Acting user's authenticated Consumer client. "
            "No impersonation or operator fallback.",
            purpose="Verify the specified file/API authorization boundary using a direct request, not a chat prompt.",
            method="GET",
            controls="Exact resource positive control before and after the attempt; "
            "byte controls verify hashes/lengths.",
            pass_meaning="For a negative probe: explicit authorization denial while resource controls still pass. "
            "For a positive operation: actual success and its required content checks, not an isolation verdict.",
        )
        if native:
            kind = operation.removeprefix("native-").split(":", 1)[0]
            routes = {
                "input-metadata": "/files/{file}",
                "input-content": "/files/{file}/content",
                "container-list": "/containers/{container}/files",
                "output-metadata": "/containers/{container}/files/{file}",
                "output-content": "/containers/{container}/files/{file}/content",
            }
            details["api_family"] = "Native Files" if kind.startswith("input") else "Native container files"
            details["route_template"] = FILES + routes.get(kind, "/{unrecorded-operation}")
            details["request_template"] = "GET " + details["route_template"]
            if kind == "container-list":
                details["request_template"] += "?limit=20"
            details["controls"] = (
                "privileged-control verifies the exact sealed-lineage resource before and after. "
                "Input/output byte controls compare original hashes/lengths; listing verifies expected IDs. "
                "The resource user's normal Hosted delivery is rechecked. Consumers receive no extra permissions."
            )
            details["limitations"] = (
                "Only this Consumer API operation is tested; a listing denial is not content denial. "
            )
            details["limitations"] += "Privileged-control success is not evidence of privileged-user isolation."
        elif operation == "listing" or "native files privilege boundary" in lower:
            details["api_family"] = "Native Files"
            details["route_template"] = FILES + "/files"
            details["request_template"] = "GET " + FILES + "/files?limit=1"
            details["controls"] = (
                "Effective Consumer privileges were checked separately; no exact foreign file is used."
            )
            details["limitations"] = "Listing permission only. Does not prove metadata or content-download denial."
        else:
            details["api_family"] = "Hosted Session Files"
            details["route_template"] = HOSTED + "/sessions/{resource-user-session}/files/content"
            details["request_template"] = "GET " + details["route_template"] + "?path={resource-user-output-path}"
            details["purpose"] = (
                "Attempt a real generated-file byte download using the exact recorded output reference."
            )
            details["limitations"] = "Hosted Session Files authorization only; not native container-file API access."
    elif operation == "response" or "hosted response" in lower:
        details.update(
            execution_type="http_request",
            api_family="Hosted Responses",
            method="GET",
            route_template=HOSTED + "/protocols/openai/responses/{response}",
            request_template="GET " + HOSTED + "/protocols/openai/responses/{resource-user-response}",
            purpose="Check whether a Consumer can retrieve another user's Hosted response.",
            authentication_context="Acting user's Consumer credential; "
            "positive control uses the resource user's client.",
            controls="Resource user can retrieve the same response before and after the attempt.",
            pass_meaning="A specific session authorization denial with valid positive controls.",
            limitations="Response/session access only, not file content access.",
        )
    elif ("upload" in lower and "input" in lower) or "session files upload" in lower:
        details.update(
            execution_type="http_request",
            api_family="Hosted Session Files",
            method="PUT",
            route_template=HOSTED + "/sessions/{acting-user-session}/files/content",
            request_template="PUT "
            + HOSTED
            + "/sessions/{acting-user-session}/files/content?path={authorized-input-path}",
            purpose="Upload synthetic input.xlsx to the acting user's authorized Hosted session.",
            authentication_context="Acting user's Consumer credential; not the backend native Files credential.",
            controls="Input size/hash and the backend's original-input binding are checked separately.",
            pass_meaning="Input upload succeeded; this is not a cross-user isolation assertion.",
        )
    elif operation in {"inspect", "artifact"} or any(
        word in lower
        for word in (
            "foreign application",
            ": application inspect",
            ": application artifact",
            ": inspect",
            ": artifact",
            ": prepare",
            ": analyze",
        )
    ):
        action = next((word for word in ("prepare", "analyze", "artifact", "inspect") if word in lower), "inspect")
        details.update(
            execution_type="application_command",
            api_family="Hosted Responses",
            method="POST",
            route_template=HOSTED + "/protocols/openai/responses",
            request_template='POC_CONSUMER_CI {"operation":"'
            + action
            + '"'
            + (',"workflow_id":"{submitted-workflow-reference}"' if action != "prepare" else "")
            + (',"artifact_id":"{submitted-artifact-reference}"' if action == "artifact" else "")
            + "}",
            authentication_context="Acting user's Consumer credential on its own Hosted session/conversation.",
            purpose="Ask the application to perform " + action + " with trusted ownership checks.",
            controls="The submitted reference is not proof of ownership. Use the trusted caller/session binding.",
            pass_meaning="Only the recorded command result or authorization assertion passed.",
        )
        if action == "analyze":
            details["prompt_applicability"] = "Outer structured command triggers a separate backend model prompt."
            details["prompt"] = (
                "Exact submitted model prompt is available only in the backend execution receipt. "
                "Planned prompt descriptions are not proof of execution."
            )
            details["limitations"] = "Model writes Python; this command is not developer-embedded analysis code."
    elif "deployment" in lower or "effective role" in lower or "runtime" in lower or "preflight" in lower:
        details.update(
            execution_type="metadata_verification",
            purpose="Inspect deployment/role/runtime metadata or compare source and package receipts.",
            api_family="Read-only metadata and local comparison",
            authentication_context="Operator reads deployment/RBAC metadata; "
            "runtime receipts originate from the backend.",
            request_template="See explicit metadata/role receipts; exact legacy calls are not reconstructed.",
            controls="Expected role, scope, version, key/source fingerprints and lockfile comparisons as recorded.",
            pass_meaning="Prerequisites match this check, not proof of execution or isolation.",
        )
    elif "backend execution receipt" in lower:
        details.update(
            purpose="Compare the backend's submitted-prompt receipt, native completed-call count and output inventory.",
            prompt_applicability="Backend-reported prompt is in observed.model_prompt when actually recorded.",
            prompt="See the recorded backend execution receipt, not a reconstructed prompt.",
            controls="Source/runtime comparison establishes which deployed implementation produced the receipt.",
            pass_meaning="Backend reported the required prompt, one completed interpreter call "
            "and both original outputs.",
            limitations="Backend-reported evidence, not a client-side downstream wire trace "
            "or model reasoning capture.",
        )
    elif "original bytes" in lower:
        details.update(
            purpose="Compare a downloaded original output's byte length and SHA-256 with its backend receipt.",
            controls="Output was retrieved through the separate authenticated download operation.",
            pass_meaning="Original-byte delivery is unchanged. This local comparison does not itself test a denial.",
            limitations="A matching hash is not proof of numerical correctness, chart visual quality, or isolation.",
        )
    elif "analysis values" in lower:
        details.update(
            purpose="Parse result.json and compare its numeric total and marker to this user's synthetic input.",
            controls="user1's amounts total 10; user2's amounts total 31. Markers must match the correct input.",
            pass_meaning="The recorded JSON total and input-specific marker match.",
        )
    elif "png bytes" in lower:
        details.update(
            purpose="Check the original chart bytes begin with the PNG file header.",
            pass_meaning="PNG format header is present; visual quality, axis labels and aesthetics are not graded.",
        )
    elif "input binding" in lower:
        details.update(
            purpose="Compare the backend input hash/length to the actual synthetic XLSX uploaded for this user.",
            pass_meaning="Backend-reported input bytes match the originally uploaded workbook.",
        )
    return details
