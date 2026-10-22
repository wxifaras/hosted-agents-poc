"""Test two real Entra users against Research, without delegated-user headers.

Uses existing isolated CLI profiles, synthetic one-hour State Store markers,
and deterministic history probes. No login, grants, deployment, models, or
automatic request retries. The harness handles acquired tokens only in memory;
Azure CLI retains its existing local authentication cache.
"""

import argparse
import asyncio
import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter, time
from types import TracebackType
from typing import Literal, Self
from uuid import UUID, uuid4

from app.core.redaction import sanitize
from app.services.identity_checks import (
    IDENTITY_PREFIX,
    IdentityCommand,
    IdentityReply,
    IdentityRunner,
    error_code,
    is_visibility_denial,
)
from app.services.workflows import WorkflowError
from azure.ai.projects import AIProjectClient
from azure.ai.projects.aio import AIProjectClient as AsyncAIProjectClient
from azure.core.credentials import AccessToken
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import AzureError
from azure.identity import AzureCliCredential
from openai import APIStatusError, AsyncOpenAI, OpenAIError
from pydantic import JsonValue
from rich.json import JSON
from rich.panel import Panel

from scripts.common.auth_profiles import ProfileError, parse_users, profile_json, profile_path, verify_account
from scripts.common.http_details import http_details
from scripts.common.reporting import ValidationRun, add_output_arguments
from scripts.deploy.grant_local_rbac import CliError, command_prefix, native_json, object_value
from scripts.run_scenario.hosted_workflow import verify_idle_timeout


@dataclass(frozen=True)
class NativeUser:
    """Verified CLI identity and its isolated configuration path, never a token."""

    upn: str
    principal: str
    tenant: str
    path: Path

    def environment(self) -> dict[str, str]:
        """Pass a profile only to child processes; do not mutate global credentials."""
        return {**os.environ, "AZURE_CONFIG_DIR": str(self.path), "AZURE_EXTENSION_USE_DYNAMIC_INSTALL": "no"}


class ProfileCredential(AsyncTokenCredential):
    """Acquire only Foundry tokens from one explicit CLI cache, with no fallback."""

    def __init__(self, user: NativeUser) -> None:
        self.user = user
        self._token: AccessToken | None = None

    def acquire(self) -> AccessToken:
        """Keep sensitive CLI stdout in memory; never include it in diagnostics."""
        result = subprocess.run(
            [
                *command_prefix("az"),
                "account",
                "get-access-token",
                "--tenant",
                self.user.tenant,
                "--resource",
                "https://ai.azure.com",
                "--output",
                "json",
            ],
            env=self.user.environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        if result.returncode:
            raise ProfileError("Native-user token acquisition failed; no login or developer fallback was attempted.")
        try:
            raw: object = json.loads(result.stdout)
            if not isinstance(raw, dict):
                raise ValueError("Expected token object.")
            token, expiry = raw.get("accessToken"), raw.get("expires_on")
            if not isinstance(token, str) or not token or type(expiry) is not int or expiry <= time():
                raise ValueError("Expected a nonexpired token.")
        except (ValueError, TypeError) as error:
            raise ProfileError("CLI returned invalid token metadata; sensitive output was not exported.") from error
        return AccessToken(token, expiry)

    async def get_token(
        self,
        *scopes: str,
        claims: str | None = None,
        tenant_id: str | None = None,
        enable_cae: bool = False,
        **kwargs: object,
    ) -> AccessToken:
        """Support only the selected tenant's normal Foundry authentication flow."""
        if scopes != ("https://ai.azure.com/.default",) or claims or enable_cae or kwargs:
            raise ProfileError("Unsupported native-user token request; no credential fallback is allowed.")
        if tenant_id is not None and tenant_id.casefold() != self.user.tenant:
            raise ProfileError("Native-user token request changed tenant.")
        if self._token is None or self._token.expires_on <= time() + 60:
            self._token = await asyncio.to_thread(self.acquire)
        return self._token

    async def close(self) -> None:
        """Release the in-process token reference without changing the CLI cache."""
        self._token = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        await self.close()


class NativeRunner(IdentityRunner):
    """Reuse diagnostic request parsing, but never the synthetic-label client factory."""

    def __init__(self, project: AsyncAIProjectClient, run: ValidationRun, actors: dict[int, str]) -> None:
        super().__init__(project, None, uuid4())
        self.reporter = run
        self.actors = actors
        self._response_http: dict[int, dict[str, object]] = {}

    def exchange(self, kind: str, details: dict[str, object]) -> None:
        """Persist sanitized exchange evidence without adding a passing assertion."""
        event = sanitize(
            {
                "run_id": self.reporter.run_id,
                "timestamp": datetime.now(UTC).isoformat(),
                "kind": kind,
                "details": details,
            }
        )
        with (self.reporter.path / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")
        if self.reporter.verbose:
            self.reporter.console.print(
                Panel(
                    JSON.from_data(event),
                    title=kind.upper(),
                    border_style="cyan" if kind == "request" else "yellow",
                )
            )

    async def send(
        self,
        client: AsyncOpenAI,
        operation: Literal["presence", "state-create", "state-read", "history-seed", "history-read"],
        *,
        conversation: str | None = None,
        previous: str | None = None,
        session: str | None = None,
    ) -> tuple[IdentityReply, str, str]:
        """Show validated diagnostic replies or safe errors, without changing assertion semantics."""
        self._response_http.pop(id(client), None)
        started = perf_counter()
        try:
            result = await super().send(
                client, operation, conversation=conversation, previous=previous, session=session
            )
        except (AzureError, OpenAIError, WorkflowError, ProfileError, ValueError, OSError) as error:
            details: dict[str, object] = {
                "actor": self.actors[id(client)],
                "operation": operation,
                "error_type": type(error).__name__,
                "http": self._response_http.pop(id(client), None),
                "duration_ms": round((perf_counter() - started) * 1000, 2),
                "note": "Transport/protocol outcome only; the isolation assertion is evaluated separately.",
            }
            if isinstance(error, APIStatusError):
                details.update(http_status=error.status_code, error_code=error_code(error))
            self.exchange("response", details)
            raise
        reply, response_id, resolved_session = result
        self.exchange(
            "response",
            {
                "actor": self.actors[id(client)],
                "operation": operation,
                "protocol_status": "completed",
                "response_id": response_id,
                "platform_session_id": resolved_session,
                "diagnostic_reply": reply.model_dump(mode="json"),
                "http": self._response_http.pop(id(client), None),
                "duration_ms": round((perf_counter() - started) * 1000, 2),
            },
        )
        return result

    def inspect_egress(
        self,
        client: AsyncOpenAI,
        url: str,
        headers: Mapping[str, str],
        *,
        content: bytes,
        previous: str | None,
        session: str | None,
        status_code: int | None = None,
        response_headers: Mapping[str, str] | None = None,
    ) -> None:
        """Inspect actual SDK requests without returning identity-bearing headers."""
        normalized = {key.casefold(): value for key, value in headers.items()}
        payload = object_value(json.loads(content))
        wire = payload.get("input")
        if not isinstance(wire, str) or not wire.startswith(IDENTITY_PREFIX):
            raise WorkflowError("Expected a bounded native diagnostic request.")
        command = IdentityCommand.model_validate_json(wire[len(IDENTITY_PREFIX) :])
        safe_http = http_details("POST", url, headers, status_code=status_code, response_headers=response_headers)
        self._response_http[id(client)] = safe_http
        self.exchange(
            "request",
            {
                "actor": self.actors[id(client)],
                "operation": command.operation,
                "message": command.wire(),
                "previous_response_id": payload.get("previous_response_id"),
                "explicit_agent_session_id": payload.get("agent_session_id"),
                "http": http_details("POST", url, headers),
            },
        )
        if not self.reporter.verbose:
            self.reporter.console.print(f"Research request: {self.actors[id(client)]} / {command.operation}")
        self.egress.append(
            {
                "no_delegated_user_header": "x-ms-user-identity" not in normalized,
                "no_forwarded_call_context": "x-agent-foundry-call-id" not in normalized,
                "no_forged_platform_user": "x-agent-user-id" not in normalized,
            }
        )


def prepare_user(upn: str, tenant: str) -> NativeUser:
    """Confirm each profile independently through CLI metadata and Graph /me."""
    path = profile_path(tenant, upn)
    if not path.is_dir():
        raise ValueError("An isolated user profile is missing; complete the profile setup task first.")
    environment = {**os.environ, "AZURE_CONFIG_DIR": str(path)}
    prefix = command_prefix("az")
    verify_account(
        profile_json(
            prefix,
            ["account", "show", "--query", "{tenant:tenantId,username:user.name,kind:user.type}"],
            environment,
        ),
        upn,
        tenant,
    )
    identity = object_value(
        profile_json(
            prefix,
            ["ad", "signed-in-user", "show", "--query", "{id:id,username:userPrincipalName}"],
            environment,
        )
    )
    actual = identity.get("username")
    if not isinstance(actual, str) or actual.casefold() != upn:
        raise ProfileError("Authenticated Graph user does not match the requested native profile.")
    return NativeUser(upn, str(UUID(str(identity.get("id")))), tenant, path)


def record(run: ValidationRun, name: str, passed: bool, **observed: JsonValue) -> None:
    """Persist the actual assertion outcome, not a successful transport proxy."""
    run.result(
        name, "PASS" if passed else "FAIL", expected="Native-user isolation/continuity assertion", observed=observed
    )


async def direction(
    owner: AsyncOpenAI, other: AsyncOpenAI, runner: NativeRunner, run: ValidationRun, label: str
) -> None:
    """Exercise positive controls, cross-user state/history, then owner continuation."""
    owner_context, _, owner_session = await runner.send(owner, "presence")
    other_context, _, other_session = await runner.send(other, "presence")
    for actor, context in (("owner", owner_context), ("other", other_context)):
        flags = context.presence.model_dump(exclude={"conversation_id_present"})
        record(run, f"{label}: {actor} native context", all(flags.values()), **flags)
        if not all(flags.values()):
            raise WorkflowError("Native caller context is incomplete; negative checks cannot establish isolation.")
    created, _, _ = await runner.send(owner, "state-create", session=owner_session)
    read, _, _ = await runner.send(owner, "state-read", session=owner_session)
    valid_state = (
        created.marker_found is True
        and read.marker_found is True
        and bool(created.marker_fingerprint)
        and created.marker_fingerprint == read.marker_fingerprint
    )
    record(run, f"{label}: owner state continuity", valid_state)
    if not valid_state:
        raise WorkflowError("Owner's state positive control failed.")
    hidden, _, _ = await runner.send(other, "state-read", session=other_session)
    record(
        run, f"{label}: other cannot read owner state", hidden.marker_found is False, marker_found=hidden.marker_found
    )
    seed, first, history_session = await runner.send(owner, "history-seed", session=owner_session)
    recalled, second, _ = await runner.send(owner, "history-read", previous=first, session=history_session)
    valid_history = bool(seed.history_marker) and recalled.history_contains_marker is True
    record(
        run,
        f"{label}: owner history continuity",
        valid_history,
        initial_response_id=first,
        continued_response_id=second,
        platform_session_id=history_session,
    )
    if not valid_history:
        raise WorkflowError("Owner's history positive control failed.")
    for binding, session in (("previous-only", None), ("previous-and-session", history_session)):
        try:
            leaked, _, _ = await runner.send(other, "history-read", previous=second, session=session)
        except APIStatusError as error:
            confirmed = False
            if error.status_code == 404 and error_code(error) == "not_found":
                resource = await owner.responses.retrieve(second)
                confirmed = resource.id == second and resource.status == "completed" and not resource.error
            record(
                run,
                f"{label}: other history denied ({binding})",
                is_visibility_denial(error, owner_resource_confirmed=confirmed),
                http_status=error.status_code,
                error_code=error_code(error),
                owner_resource_confirmed=confirmed,
            )
        else:
            record(
                run,
                f"{label}: other history denied ({binding})",
                False,
                request_was_accepted=True,
                private_marker_seen=leaked.history_contains_marker,
            )
    resumed, final, final_session = await runner.send(owner, "history-read", previous=second, session=history_session)
    record(
        run,
        f"{label}: owner continues after other attempts",
        resumed.history_contains_marker is True
        and final_session == history_session
        and bool(final)
        and final != second,
        history_marker_seen=resumed.history_contains_marker,
        original_session_reused=final_session == history_session,
        new_response_created=bool(final) and final != second,
    )
    after, _, _ = await runner.send(owner, "state-read", session=owner_session)
    record(
        run,
        f"{label}: owner state remains unchanged",
        after.marker_found is True and after.marker_fingerprint == created.marker_fingerprint,
    )


async def execute(endpoint: str, users: list[NativeUser], run: ValidationRun) -> None:
    """Keep both credentials/clients distinct without changing process-global auth."""
    async with AsyncExitStack() as stack:
        clients: list[AsyncOpenAI] = []
        projects: list[AsyncAIProjectClient] = []
        for user in users:
            credential = await stack.enter_async_context(ProfileCredential(user))
            project = await stack.enter_async_context(
                AsyncAIProjectClient(endpoint, credential, allow_preview=True, logging_enable=False)
            )
            projects.append(project)
            clients.append(
                await stack.enter_async_context(
                    project.get_openai_client(
                        agent_name="research-agent", timeout=180, max_retries=0, default_headers={}
                    )
                )
            )
        for owner_index, other_index in ((0, 1), (1, 0)):
            label = f"user{owner_index + 1} owner / user{other_index + 1} other"
            run.console.rule(label)
            runner = NativeRunner(
                projects[owner_index], run, {id(client): f"user{i + 1}" for i, client in enumerate(clients)}
            )
            try:
                await direction(clients[owner_index], clients[other_index], runner, run, label)
            except (
                AzureError,
                OpenAIError,
                WorkflowError,
                ProfileError,
                ValueError,
                OSError,
                subprocess.TimeoutExpired,
            ) as error:
                observed: dict[str, JsonValue] = {"error_type": type(error).__name__}
                if isinstance(error, APIStatusError):
                    observed.update(http_status=error.status_code, error_code=error_code(error))
                elif isinstance(error, (WorkflowError, ProfileError)):
                    observed["reason"] = str(error)
                record(run, f"{label}: execution", False, **observed)
            record(
                run,
                f"{label}: native request headers",
                bool(runner.egress) and all(value is True for item in runner.egress for value in item.values()),
                requests_checked=len(runner.egress),
                synthetic_user_labels=False,
            )


def build_parser() -> argparse.ArgumentParser:
    """Require explicit native users and an explicit paid test run."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--users", required=True, help="Exactly two full UPNs with existing isolated CLI profiles.")
    parser.add_argument("--environment", default="hosted-agents-poc-dev")
    parser.add_argument(
        "--run", action="store_true", help="Run the real hosted synthetic-data tests in both directions."
    )
    add_output_arguments(parser)
    parser.add_argument(
        "--no-verbose",
        action="store_false",
        dest="verbose",
        default=argparse.SUPPRESS,
        help="Show request labels and assertion summaries without request/response panels.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Preflight with the operator, but invoke Research only with the two native users."""
    args = build_parser().parse_args(argv)
    run = ValidationRun(
        "native-user-isolation", "hosted", args.output_dir, no_color=args.no_color, verbose=args.verbose
    )
    try:
        names = parse_users(args.users)
        if len(names) != 2:
            raise ValueError("Exactly two distinct native user profiles are required.")
        config = object_value(
            native_json("azd", ["env", "get-values", "--environment", args.environment, "--output", "json"])
        )
        tenant = str(UUID(str(config.get("AZURE_TENANT_ID"))))
        endpoint = config.get("FOUNDRY_PROJECT_ENDPOINT")
        if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
            raise ValueError("Expected the selected environment's HTTPS Foundry project endpoint.")
        users = [prepare_user(name, tenant) for name in names]
        if len({user.principal for user in users}) != 2:
            raise ValueError("The profiles do not represent two distinct authenticated Entra principals.")
        record(run, "Distinct genuine Entra users", True, users=list[JsonValue](names), distinct_principals=True)
        with (
            AzureCliCredential(process_timeout=60) as credential,
            AIProjectClient(endpoint, credential, allow_preview=True) as project,
        ):
            agent = object_value(project.agents.get("research-agent").as_dict())
            latest = object_value(object_value(agent.get("versions")).get("latest"))
            rules = object_value(object_value(agent.get("agent_endpoint")).get("version_selector")).get(
                "version_selection_rules"
            )
            if latest.get("status") != "active" or rules != [
                {"type": "FixedRatio", "agent_version": "@latest", "traffic_percentage": 100}
            ]:
                raise ValueError("Research must be active with exclusive latest-version routing.")
            idle = verify_idle_timeout(project, "research-agent", str(latest.get("version")))
            record(run, "Research target", True, version=latest.get("version"), idle_timeout_seconds=idle)
        if args.run:
            asyncio.run(execute(endpoint, users, run))
        else:
            run.result(
                "Hosted tests",
                "BLOCKED",
                expected="Explicit --run approval",
                observed="Preflight only; no agents invoked.",
            )
    except (
        AzureError,
        OpenAIError,
        WorkflowError,
        ProfileError,
        ValueError,
        OSError,
        CliError,
        subprocess.TimeoutExpired,
    ) as error:
        run.result(
            "Native test prerequisites",
            "BLOCKED",
            expected="Verified distinct profiles and active existing Research",
            observed={"error_type": type(error).__name__},
        )
    return run.finish()


if __name__ == "__main__":
    raise SystemExit(main())
