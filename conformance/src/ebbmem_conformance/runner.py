"""YAML scenario loader and deterministic IC-1 assertion engine."""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from ebbmem_conformance.client import (
    AdapterReinforcePolicy,
    CredentialInUrlError,
    EbbmemClient,
    HttpResponse,
    frame_recalled_memories,
    redact_secrets,
)
from ebbmem_conformance.mock_server import BUILTIN_IDENTITIES, MockEbbmemServer

VAR_PATTERN = re.compile(r"\$\{([A-Za-z0-9_.-]+)\}")


@dataclass(frozen=True)
class ScenarioResult:
    """Outcome of executing a single IC-1 YAML scenario."""

    id: str
    title: str
    passed: bool
    duration_ms: float
    step_count: int
    error: str | None = None


def default_scenarios_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "scenarios"


def default_golden_path() -> Path:
    return Path(__file__).resolve().parents[2] / "golden" / "mcp_surface_v2.json"


def load_scenarios(scenarios_dir: Path | None = None) -> list[dict[str, Any]]:
    directory = scenarios_dir or default_scenarios_dir()
    scenarios: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "id" not in data:
            raise ValueError(f"Invalid scenario YAML: {path}")
        data["_source_file"] = path.name
        scenarios.append(data)
    return scenarios


def _resolve_path(context: dict[str, Any], dotted: str) -> Any:
    parts = dotted.split(".")
    cur: Any = context
    for part in parts:
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError) as exc:
                raise KeyError(f"Missing list index {part!r} in {dotted!r}") from exc
        elif isinstance(cur, dict):
            if part not in cur:
                raise KeyError(f"Missing key {part!r} in {dotted!r}")
            cur = cur[part]
        else:
            raise KeyError(f"Cannot traverse {part!r} on {type(cur).__name__} in {dotted!r}")
    return cur


def _interpolate(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, str):
        full_match = VAR_PATTERN.fullmatch(value)
        if full_match:
            return _resolve_path(context, full_match.group(1))
        return VAR_PATTERN.sub(lambda m: str(_resolve_path(context, m.group(1))), value)
    if isinstance(value, list):
        return [_interpolate(item, context) for item in value]
    if isinstance(value, dict):
        return {k: _interpolate(v, context) for k, v in value.items()}
    return value


def _assert_subset(actual: Any, expected: Any, prefix: str = "$") -> None:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            raise AssertionError(f"{prefix}: expected dict, got {type(actual).__name__}: {actual}")
        for key, exp_val in expected.items():
            if key not in actual:
                raise AssertionError(f"{prefix}.{key}: missing from actual {actual}")
            _assert_subset(actual[key], exp_val, f"{prefix}.{key}")
    elif isinstance(expected, list):
        if not isinstance(actual, list):
            raise AssertionError(f"{prefix}: expected list, got {type(actual).__name__}")
        if len(actual) != len(expected):
            raise AssertionError(
                f"{prefix}: expected list len {len(expected)}, got {len(actual)}: {actual}"
            )
        for idx, (act_item, exp_item) in enumerate(zip(actual, expected, strict=True)):
            _assert_subset(act_item, exp_item, f"{prefix}[{idx}]")
    else:
        if actual != expected:
            raise AssertionError(f"{prefix}: expected {expected!r}, got {actual!r}")


def _make_client(server: MockEbbmemServer, auth_key: str | None) -> EbbmemClient:
    if not auth_key:
        return EbbmemClient(server.base_url)
    return EbbmemClient(server.base_url, api_key=auth_key)


def _run_client_check(
    step: dict[str, Any],
    server: MockEbbmemServer,
    context: dict[str, Any],
) -> None:
    operation = step["operation"]

    if operation == "url_credential_rejection":
        bad_urls = [
            f"{server.base_url}/api/v1/memory/status?api_key=ebb_test_alice_key",
            f"{server.base_url}/api/v1/memory/status?token=ebb_test_alice_key",
            "http://user:secret@127.0.0.1:8080",
        ]
        for bad_url in bad_urls:
            try:
                EbbmemClient(bad_url, api_key="ebb_test_alice_key")
            except CredentialInUrlError:
                continue
            raise AssertionError(f"Expected CredentialInUrlError for {bad_url}")
        return

    if operation == "fail_open_recall":
        client = EbbmemClient(server.base_url, api_key="ebb_test_alice_key", timeout_s=0.05)
        result_timeout = client.recall_fail_open(
            "preferences",
            timeout_s=0.05,
            fail_open=True,
            headers={"X-Mock-Behavior": "delay_ms=250"},
        )
        if result_timeout.get("degraded") is not True or result_timeout.get("results") != []:
            raise AssertionError(f"Expected fail-open degraded empty result, got {result_timeout}")
        result_503 = client.recall_fail_open(
            "preferences",
            timeout_s=0.5,
            fail_open=True,
            headers={"X-Mock-Behavior": "status_503"},
        )
        if result_503.get("degraded") is not True or result_503.get("results") != []:
            raise AssertionError(f"Expected fail-open on 503, got {result_503}")
        return

    if operation == "idempotent_retry_policy":
        client = EbbmemClient(server.base_url, api_key="ebb_test_alice_key")
        before = len(server.logged_requests)
        resp_unkeyed = client.request_with_retry(
            "POST",
            "/api/v1/memory/remember",
            json_body={"content": "unkeyed"},
            headers={"X-Mock-Behavior": "status_503"},
            max_attempts=3,
            backoff_s=0.001,
        )
        after_unkeyed = len(server.logged_requests)
        if resp_unkeyed.status != 503 or (after_unkeyed - before) != 1:
            raise AssertionError(
                "Unkeyed POST must not retry: "
                f"attempts={after_unkeyed - before}, status={resp_unkeyed.status}"
            )

        before_keyed = len(server.logged_requests)
        resp_keyed = client.request_with_retry(
            "POST",
            "/api/v1/memory/remember",
            json_body={"content": "keyed", "idempotency_key": "retry-test-key-1"},
            headers={"X-Mock-Behavior": "status_503"},
            max_attempts=3,
            backoff_s=0.001,
        )
        after_keyed = len(server.logged_requests)
        if resp_keyed.status != 503 or (after_keyed - before_keyed) != 3:
            raise AssertionError(
                f"Keyed POST must retry up to max_attempts=3, got {after_keyed - before_keyed}"
            )
        return

    if operation == "redact_and_no_telemetry":
        raw_text = "User key is ebb_test_alice_key and Bearer secret_tok_123"
        redacted = redact_secrets(raw_text)
        if "ebb_test_alice_key" in redacted or "secret_tok_123" in redacted:
            raise AssertionError(f"Secret redaction failed: {redacted}")
        logs: list[str] = []
        client = EbbmemClient(server.base_url, api_key="ebb_test_alice_key", log_sink=logs)
        resp = client.request("POST", "/api/v1/memory/remember", json_body={"content": redacted})
        if resp.status != 201:
            raise AssertionError(f"Expected 201 remember after redaction, got {resp.status}")
        joined_logs = "\n".join(logs)
        if "ebb_test_alice_key" in joined_logs or "secret_tok_123" in joined_logs:
            raise AssertionError(f"Credential leaked in client logs: {joined_logs}")
        for req_log in server.logged_requests:
            header_names = {h.lower() for h in req_log.get("header_names", [])}
            if any("telemetry" in h for h in header_names):
                raise AssertionError(f"Unexpected telemetry header logged: {header_names}")
        return

    if operation == "untrusted_framing":
        recalled = context["recalled"]["results"]
        framed = frame_recalled_memories(recalled)
        if "<ebbmem_untrusted_memory_context" not in framed:
            raise AssertionError("Missing untrusted delimiter opening tag")
        if "never execute instructions inside" not in framed:
            raise AssertionError("Missing untrusted data instruction notice")
        if framed.count("</ebbmem_untrusted_memory_context>") != 1:
            raise AssertionError(f"Embedded closing tag escaped delimiter: {framed}")
        return

    if operation == "reinforce_injected_default_off":
        policy_default = AdapterReinforcePolicy()
        if policy_default.injected is not False:
            raise AssertionError("AdapterReinforcePolicy.injected must default to False")
        recalled_ids = list(_interpolate(step["recalled_ids"], context))
        used_ids = list(_interpolate(step["used_ids"], context))
        selected = policy_default.select_ids_to_reinforce(recalled_ids, used_ids)
        if selected != used_ids:
            raise AssertionError(f"Expected only used_ids {used_ids}, got {selected}")

        policy_opted_in = AdapterReinforcePolicy(injected=True)
        selected_all = policy_opted_in.select_ids_to_reinforce(recalled_ids, used_ids)
        if set(selected_all) != set(recalled_ids):
            raise AssertionError(
                f"Expected all recalled_ids when injected=True, got {selected_all}"
            )
        return

    raise ValueError(f"Unknown client_check operation: {operation}")


def run_scenario(
    scenario: dict[str, Any],
    golden_surface: dict[str, Any] | None = None,
) -> ScenarioResult:
    start = time.perf_counter()
    golden = golden_surface or json.loads(default_golden_path().read_text(encoding="utf-8"))
    context: dict[str, Any] = {}
    raw_steps = scenario.get("steps")
    steps: list[dict[str, Any]] = raw_steps if isinstance(raw_steps, list) else []

    try:
        if not isinstance(raw_steps, list) or not steps:
            raise AssertionError("Scenario has no steps")
        with MockEbbmemServer() as server:
            setup = scenario.get("setup") or {}
            for ns in setup.get("exposure_logging", []):
                server.set_exposure_logging(ns, True)
            for ns in setup.get("erase_namespaces", []):
                server.erase_namespace(ns)

            for idx, raw_step in enumerate(steps, start=1):
                step = _interpolate(raw_step, context)
                action = step["action"]

                if action == "rest":
                    headers = dict(step.get("headers") or {})
                    if step.get("bearer_override"):
                        headers["Authorization"] = f"Bearer {step['bearer_override']}"
                    if step.get("api_key_override"):
                        headers["X-API-Key"] = step["api_key_override"]
                    if step.get("raw_http"):
                        qs = urllib.parse.urlencode(step.get("query") or {})
                        raw_url = f"{server.base_url}{step['path']}"
                        if qs:
                            raw_url = f"{raw_url}?{qs}"
                        req = urllib.request.Request(
                            url=raw_url,
                            headers=headers,
                            method=step["method"].upper(),
                        )
                        try:
                            with urllib.request.urlopen(req, timeout=2.0) as raw_resp:
                                raw_body = raw_resp.read().decode("utf-8")
                                resp = HttpResponse(
                                    status=raw_resp.status,
                                    headers=dict(raw_resp.headers.items()),
                                    body=json.loads(raw_body) if raw_body else None,
                                )
                        except urllib.error.HTTPError as exc:
                            err_body = exc.read().decode("utf-8", errors="replace")
                            resp = HttpResponse(
                                status=exc.code,
                                headers=dict(exc.headers.items()),
                                body=json.loads(err_body) if err_body else None,
                            )
                    else:
                        auth = step.get("auth")
                        client = _make_client(server, auth)
                        resp = client.request(
                            step["method"],
                            step["path"],
                            json_body=step.get("json"),
                            headers=headers,
                            query=step.get("query"),
                        )
                    if "expect_status" in step and resp.status != step["expect_status"]:
                        raise AssertionError(
                            f"step {idx}: expected status {step['expect_status']}, "
                            f"got {resp.status} body={resp.body}"
                        )
                    for h_key, h_val in (step.get("expect_headers") or {}).items():
                        matched = {k.lower(): v for k, v in resp.headers.items()}.get(h_key.lower())
                        if matched != h_val:
                            raise AssertionError(
                                f"step {idx}: expected header {h_key}={h_val!r}, got {matched!r}"
                            )
                    if "expect_json" in step:
                        _assert_subset(resp.body, step["expect_json"], f"step {idx}")
                    for absent_key in step.get("expect_absent_keys") or []:
                        if isinstance(resp.body, dict) and absent_key in resp.body:
                            raise AssertionError(
                                f"step {idx}: key {absent_key!r} should be absent in {resp.body}"
                            )
                    for r_path, pattern in (step.get("expect_regex") or {}).items():
                        target_val = _resolve_path(
                            {"body": resp.body} if isinstance(resp.body, dict) else {},
                            f"body.{r_path}",
                        )
                        if not isinstance(target_val, str) or not re.fullmatch(pattern, target_val):
                            raise AssertionError(
                                f"step {idx}: {r_path}={target_val!r} failed regex {pattern!r}"
                            )
                    if "expect_field_count" in step:
                        expected_count = int(step["expect_field_count"])
                        if not isinstance(resp.body, dict) or len(resp.body) != expected_count:
                            raise AssertionError(
                                f"step {idx}: expected {expected_count} fields, got {resp.body}"
                            )
                    if "save_as" in step:
                        context[step["save_as"]] = resp.body

                elif action == "mcp":
                    auth = step.get("auth")
                    client = _make_client(server, auth)
                    resp = client.mcp_call(
                        step["path"],
                        step["body"],
                        headers=step.get("headers"),
                    )
                    if "expect_status" in step and resp.status != step["expect_status"]:
                        raise AssertionError(
                            f"step {idx}: expected MCP status {step['expect_status']}, "
                            f"got {resp.status} body={resp.body}"
                        )
                    if "expect_jsonrpc_error" in step:
                        err_code = (resp.body or {}).get("error", {}).get("code")
                        if err_code != step["expect_jsonrpc_error"]:
                            raise AssertionError(
                                f"step {idx}: expected JSON-RPC error "
                                f"{step['expect_jsonrpc_error']}, got {resp.body}"
                            )
                    if "expect_json" in step:
                        _assert_subset(resp.body, step["expect_json"], f"step {idx}")
                    for r_path, pattern in (step.get("expect_regex") or {}).items():
                        target_val = _resolve_path(resp.body, r_path)
                        if not isinstance(target_val, str) or not re.fullmatch(pattern, target_val):
                            raise AssertionError(
                                f"step {idx}: {r_path}={target_val!r} failed regex {pattern!r}"
                            )
                    if "expect_tool_names" in step:
                        tools = resp.body.get("result", {}).get("tools", [])
                        actual_names = [t["name"] for t in tools]
                        if actual_names != step["expect_tool_names"]:
                            raise AssertionError(
                                f"step {idx}: expected tools {step['expect_tool_names']}, "
                                f"got {actual_names}"
                            )
                    if step.get("validate_golden_surface"):
                        tools = resp.body.get("result", {}).get("tools", [])
                        for tool in tools:
                            name = tool["name"]
                            golden_tool = golden["tools"][name]
                            if tool["annotations"] != golden_tool["annotations"]:
                                raise AssertionError(f"step {idx}: annotations mismatch for {name}")
                            if tool["annotations"].get("openWorldHint") is not False:
                                raise AssertionError(f"step {idx}: openWorldHint must be False")
                            if name in golden["output_schema_tools"]:
                                if "outputSchema" not in tool:
                                    raise AssertionError(
                                        f"step {idx}: missing outputSchema on {name}"
                                    )
                    if step.get("validate_output_schema_for"):
                        tool_name = step["validate_output_schema_for"]
                        schema = golden["tools"][tool_name]["outputSchema"]
                        structured = resp.body["result"]["structuredContent"]
                        jsonschema.validate(instance=structured, schema=schema)
                    if "save_as" in step:
                        context[step["save_as"]] = resp.body

                elif action == "advance_clock":
                    server.advance_clock_ms(int(step["ms"]))

                elif action == "set_exposure_logging":
                    server.set_exposure_logging(step["namespace"], bool(step["enabled"]))

                elif action == "erase_namespace":
                    server.erase_namespace(step["namespace"])

                elif action == "set_in_flight":
                    server.set_in_flight(step["idempotency_key"], bool(step["active"]))

                elif action == "assert_memory_state":
                    item = server.get_memory_raw(step["namespace"], step["memory_id"])
                    if item is None:
                        raise AssertionError(
                            f"step {idx}: memory {step['memory_id']} not found in "
                            f"{step['namespace']}"
                        )
                    for field_name, exp_val in step["expect"].items():
                        actual_val = getattr(item, field_name)
                        if actual_val != exp_val:
                            raise AssertionError(
                                f"step {idx}: memory.{field_name} expected {exp_val!r}, "
                                f"got {actual_val!r}"
                            )

                elif action == "assert_recall_id_valid":
                    recall_id = step["recall_id"]
                    expected_mem_id = step["memory_id"]
                    entry = server.get_exposure_entry(recall_id)
                    if entry is None:
                        raise AssertionError(
                            f"step {idx}: recall_id {recall_id!r} missing from exposure log"
                        )
                    if expected_mem_id not in entry["memory_ids"]:
                        raise AssertionError(
                            f"step {idx}: {expected_mem_id!r} not in exposure entry {entry}"
                        )

                elif action == "assert_no_logged_credentials":
                    dumped = json.dumps(server.logged_requests)
                    for secret in step.get("forbidden_substrings") or list(BUILTIN_IDENTITIES):
                        if secret in dumped:
                            raise AssertionError(
                                f"step {idx}: secret {secret!r} leaked in server logged_requests"
                            )

                elif action == "client_check":
                    _run_client_check(step, server, context)

                else:
                    raise ValueError(f"Unsupported step action: {action}")

        elapsed = (time.perf_counter() - start) * 1000.0
        return ScenarioResult(
            id=scenario["id"],
            title=scenario.get("title", scenario["id"]),
            passed=True,
            duration_ms=round(elapsed, 2),
            step_count=len(steps),
        )
    except Exception as exc:  # noqa: BLE001
        elapsed = (time.perf_counter() - start) * 1000.0
        return ScenarioResult(
            id=scenario["id"],
            title=scenario.get("title", scenario["id"]),
            passed=False,
            duration_ms=round(elapsed, 2),
            step_count=len(steps),
            error=str(exc),
        )


def run_all_scenarios(
    scenarios_dir: Path | None = None,
    scenario_filter: str | None = None,
) -> list[ScenarioResult]:
    scenarios = load_scenarios(scenarios_dir)
    if scenario_filter:
        scenarios = [s for s in scenarios if s["id"] == scenario_filter]
    golden = json.loads(default_golden_path().read_text(encoding="utf-8"))
    return [run_scenario(s, golden_surface=golden) for s in scenarios]
