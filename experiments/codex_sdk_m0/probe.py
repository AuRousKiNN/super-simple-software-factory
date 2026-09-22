"""SSSF Pi -> Codex M0 capability probe.

The probe deliberately lives outside the production templates. It exercises
the public Python SDK against its bundled app-server and writes raw evidence to
an operator-selected directory (normally below /tmp). Committed samples are
produced separately after IDs and local paths have been redacted.
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterable
from enum import Enum
from importlib.metadata import version
from pathlib import Path
from typing import Any

from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox
from pydantic import BaseModel

SDK_VERSION = "0.155.1"
DEFAULT_MODEL = "gpt-5.6-terra"
DEFAULT_EFFORT = "low"
ROLE_MARKER = "SSSF_M0_DEVELOPER_INSTRUCTIONS_OK"
CONTINUITY_MARKER = "SSSF_M0_THREAD_CONTINUITY_OK"
STATE_SCHEMA_VERSION = 1


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_envelope_types(repo_root: Path) -> dict[str, type[BaseModel]]:
    template_root = repo_root / ".agents/skills/sssf/templates/adws"
    sys.path.insert(0, str(template_root))
    from adw_modules.data_types import (
        BuildOutput,
        ChangesOutput,
        DocumentOutput,
        GenericOutput,
        PlanOutput,
        ReviewOutput,
        ScoutOutput,
        VerifyOutput,
    )

    return {
        cls.__name__: cls
        for cls in (
            GenericOutput,
            PlanOutput,
            BuildOutput,
            ScoutOutput,
            ReviewOutput,
            DocumentOutput,
            ChangesOutput,
            VerifyOutput,
        )
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _jsonable(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_jsonable(value), indent=2, sort_keys=True) + "\n")


def _strict_output_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert Pydantic JSON Schema to the strict object subset Codex accepts.

    The SDK forwards ``output_schema`` as provided. The response-format API
    requires every object to reject extra keys and to list every property as
    required. Pydantic defaults describe host-side validation behavior, not a
    response-format constraint, so they are removed from the wire schema.
    """

    def visit(value: Any) -> Any:
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        converted = {
            key: visit(item) for key, item in value.items() if key != "default"
        }
        properties = converted.get("properties")
        if converted.get("type") == "object" or isinstance(properties, dict):
            converted["additionalProperties"] = False
            converted["required"] = list((properties or {}).keys())
        return converted

    return visit(copy.deepcopy(schema))


def _output_schema(output_type: type[BaseModel]) -> dict[str, Any]:
    return _strict_output_schema(output_type.model_json_schema())


def _serialize_result(result: Any) -> dict[str, Any]:
    return {
        "id": result.id,
        "status": _jsonable(result.status),
        "error": _jsonable(result.error),
        "started_at": result.started_at,
        "completed_at": result.completed_at,
        "duration_ms": result.duration_ms,
        "final_response": result.final_response,
        "items": _jsonable(result.items),
        "usage": _jsonable(result.usage),
    }


def _notification_record(notification: Any) -> dict[str, Any]:
    return {
        "received_at_ns": time.time_ns(),
        "method": notification.method,
        "params": _jsonable(notification.payload),
    }


def _developer_instructions() -> str:
    return (
        "This is the SSSF M0 runtime-validation role. Never use tools unless the user explicitly "
        "asks for one exact command. For every completed structured response, set `summary` to "
        f"exactly `{ROLE_MARKER}`. Keep all other prose minimal."
    )


def _new_codex(workspace: Path) -> Codex:
    if version("openai-codex") != SDK_VERSION:
        raise RuntimeError(
            f"expected openai-codex {SDK_VERSION}, got {version('openai-codex')}"
        )
    return Codex(CodexConfig(cwd=str(workspace), experimental_api=True))


def _start_thread(
    codex: Codex, workspace: Path, model: str, *, config: dict | None = None
):
    return codex.thread_start(
        approval_mode=ApprovalMode.deny_all,
        config=config,
        cwd=str(workspace),
        developer_instructions=_developer_instructions(),
        model=model,
        sandbox=Sandbox.read_only,
        service_name="sssf_m0_probe",
    )


def _assert_structured(
    result: Any,
    output_type: type[BaseModel],
    *,
    require_continuity: bool = False,
) -> BaseModel:
    if str(_jsonable(result.status)) != "completed":
        raise AssertionError(f"turn {result.id} ended as {result.status!r}")
    if not result.final_response:
        raise AssertionError(f"turn {result.id} had no final response")
    envelope = output_type.model_validate_json(result.final_response)
    if envelope.summary != ROLE_MARKER:
        raise AssertionError(
            "developer instructions were not reflected in the structured result: "
            f"{envelope.summary!r}"
        )
    if require_continuity and CONTINUITY_MARKER not in envelope.notes_for_next_agent:
        raise AssertionError(
            "continued turn did not retain the prior continuity marker"
        )
    return envelope


def _schema_prompt(name: str) -> str:
    extra = {
        "ScoutOutput": (
            'Set findings to exactly [{"file":"README.md","note":"M0 nested schema accepted"}].'
        ),
        "ReviewOutput": (
            "Set approved=true, blocking=[], and findings to exactly "
            '[{"requirement":"nested schema","met":true,"evidence":"accepted"}].'
        ),
        "VerifyOutput": "Set passed=true and failures=[].",
        "ChangesOutput": "Set base='m0', changed_files=[], insertions=0, deletions=0.",
        "DocumentOutput": "Set document_path='docs/m0.md' and documented_files=[].",
        "PlanOutput": "Set commit_message='M0 plan probe'.",
    }.get(name, "")
    return (
        f"Return the smallest valid {name} JSON object. Set status='success', artifacts=[], "
        "notes_for_next_agent='schema accepted'. Follow the summary rule from your developer "
        f"instructions. {extra} Do not call tools."
    )


def cmd_preflight(args: argparse.Namespace) -> dict[str, Any]:
    workspace = args.workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    with _new_codex(workspace) as codex:
        models = codex.models()
        selected = next(
            (entry for entry in models.data if entry.id == args.model), None
        )
        if selected is None:
            raise RuntimeError(
                f"model {args.model!r} is not available to the current account"
            )
        record = {
            "sdk_version": version("openai-codex"),
            "runtime": codex.metadata,
            "selected_model": selected,
            "available_model_ids": [entry.id for entry in models.data],
            "python": sys.version,
            "platform": sys.platform,
        }
    _write_json(args.output_dir / "preflight.json", record)
    return _jsonable(record)


def cmd_baseline(args: argparse.Namespace) -> dict[str, Any]:
    envelope_types = _load_envelope_types(args.repo_root)
    workspace = args.workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {}
    with _new_codex(workspace) as codex:
        thread = _start_thread(codex, workspace, args.model)

        review_type = envelope_types["ReviewOutput"]
        first = thread.run(
            _schema_prompt("ReviewOutput")
            + f" Also set notes_for_next_agent='{CONTINUITY_MARKER}'.",
            effort=args.effort,
            output_schema=_output_schema(review_type),
        )
        _assert_structured(first, review_type)
        results["ReviewOutput"] = _serialize_result(first)

        generic_type = envelope_types["GenericOutput"]
        second = thread.run(
            "Return GenericOutput JSON without tools. Repeat the continuity marker from the "
            "previous turn verbatim in notes_for_next_agent and follow the developer summary rule.",
            effort=args.effort,
            output_schema=_output_schema(generic_type),
        )
        _assert_structured(second, generic_type, require_continuity=True)
        results["GenericOutput"] = _serialize_result(second)

        for name in (
            "PlanOutput",
            "ScoutOutput",
            "DocumentOutput",
            "ChangesOutput",
            "VerifyOutput",
        ):
            output_type = envelope_types[name]
            result = thread.run(
                _schema_prompt(name),
                effort=args.effort,
                output_schema=_output_schema(output_type),
            )
            _assert_structured(result, output_type)
            results[name] = _serialize_result(result)

        state = {
            "schema_version": STATE_SCHEMA_VERSION,
            "thread_id": thread.id,
            "model": args.model,
            "effort": args.effort,
            "workspace": str(workspace),
            "developer_instructions": _developer_instructions(),
            "continuity_marker": CONTINUITY_MARKER,
            "runtime": codex.metadata,
        }

    _write_json(args.output_dir / "baseline.json", results)
    _write_json(args.state, state)
    return {"thread_id": state["thread_id"], "validated_schemas": sorted(results)}


def cmd_resume(args: argparse.Namespace) -> dict[str, Any]:
    envelope_types = _load_envelope_types(args.repo_root)
    state = json.loads(args.state.read_text())
    if state.get("schema_version") != STATE_SCHEMA_VERSION:
        raise RuntimeError("unsupported probe state schema")
    workspace = Path(state["workspace"]).resolve()
    output_type = envelope_types["BuildOutput"]
    with _new_codex(workspace) as codex:
        thread = codex.thread_resume(
            state["thread_id"],
            approval_mode=ApprovalMode.deny_all,
            cwd=str(workspace),
            developer_instructions=state["developer_instructions"],
            model=state["model"],
            sandbox=Sandbox.read_only,
        )
        result = thread.run(
            "This process was freshly started. Return BuildOutput JSON, use the developer summary "
            "rule, set changed_files=[], commit_message='M0 resume probe', and repeat the continuity "
            "marker learned in the earlier process in notes_for_next_agent. Do not use tools.",
            effort=state["effort"],
            output_schema=_output_schema(output_type),
        )
        envelope = _assert_structured(result, output_type, require_continuity=True)
        record = {
            "requested_thread_id": state["thread_id"],
            "resumed_thread_id": thread.id,
            "same_thread": thread.id == state["thread_id"],
            "envelope": envelope,
            "result": _serialize_result(result),
            "runtime": codex.metadata,
        }
    if not record["same_thread"]:
        raise AssertionError("thread_resume returned a different thread id")
    _write_json(args.output_dir / "resume.json", record)
    return _jsonable(record)


def _consume_stream(
    handle: Any, raw_path: Path
) -> tuple[list[dict[str, Any]], str | None]:
    records: list[dict[str, Any]] = []
    final_response: str | None = None
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    with raw_path.open("w") as raw:
        for notification in handle.stream():
            record = _notification_record(notification)
            records.append(record)
            raw.write(json.dumps(record, sort_keys=True) + "\n")
            raw.flush()
            if notification.method != "item/completed":
                continue
            item = getattr(notification.payload, "item", None)
            item = getattr(item, "root", item)
            if getattr(item, "type", None) == "agentMessage" and getattr(
                item, "text", None
            ):
                phase = _jsonable(getattr(item, "phase", None))
                if phase in (None, "final_answer"):
                    final_response = item.text
    return records, final_response


def _event_methods(records: Iterable[dict[str, Any]]) -> list[str]:
    return [record["method"] for record in records]


def cmd_stream(args: argparse.Namespace) -> dict[str, Any]:
    output_type = _load_envelope_types(args.repo_root)["GenericOutput"]
    workspace = args.workspace.resolve()
    with _new_codex(workspace) as codex:
        thread = _start_thread(codex, workspace, args.model)
        handle = thread.turn(
            "Run the exact shell command `pwd` once. Then return GenericOutput JSON with "
            "status='success', artifacts=[], notes_for_next_agent='stream probe', and the required "
            "developer-instruction summary.",
            effort=args.effort,
            output_schema=_output_schema(output_type),
        )
        records, final_response = _consume_stream(
            handle, args.output_dir / "stream.events.raw.jsonl"
        )
        methods = _event_methods(records)
        if "turn/started" not in methods or "turn/completed" not in methods:
            raise AssertionError("turn lifecycle notifications were incomplete")
        if "thread/tokenUsage/updated" not in methods:
            raise AssertionError("usage notification was not observed")
        if not any(
            record["method"] == "item/completed"
            and record["params"].get("item", {}).get("type") == "commandExecution"
            for record in records
        ):
            raise AssertionError("completed commandExecution item was not observed")
        if not final_response:
            raise AssertionError("stream had no final agent response")
        envelope = output_type.model_validate_json(final_response)
        if envelope.summary != ROLE_MARKER:
            raise AssertionError(
                "streamed structured response ignored developer instructions"
            )
        record = {
            "thread_id": thread.id,
            "turn_id": handle.id,
            "event_count": len(records),
            "event_methods": sorted(set(methods)),
            "envelope": envelope,
            "runtime": codex.metadata,
        }
    _write_json(args.output_dir / "stream.json", record)
    return _jsonable(record)


def _turn_status(records: Iterable[dict[str, Any]]) -> str | None:
    for record in reversed(list(records)):
        if record["method"] == "turn/completed":
            return record["params"].get("turn", {}).get("status")
    return None


def _matching_processes(marker: str) -> list[str]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,command="],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if marker in line]


def cmd_cancel(args: argparse.Namespace) -> dict[str, Any]:
    workspace = args.workspace.resolve()
    marker = f"sssf-m0-cancel-{uuid.uuid4()}"
    interrupt_sent = threading.Event()
    fallback_error: list[str] = []
    with _new_codex(workspace) as codex:
        thread = _start_thread(codex, workspace, args.model)
        handle = thread.turn(
            "Use the shell tool immediately to run exactly "
            f"`python3 -c 'import time; time.sleep(30)' # {marker}`. "
            "Do not do anything else before the command and do not replace the command.",
            effort=args.effort,
        )

        def fallback_interrupt() -> None:
            if interrupt_sent.wait(timeout=12):
                return
            try:
                handle.interrupt()
                interrupt_sent.set()
            except Exception as error:  # noqa: BLE001 - failure is recorded as probe evidence
                fallback_error.append(repr(error))

        watchdog = threading.Thread(target=fallback_interrupt, daemon=True)
        watchdog.start()
        records: list[dict[str, Any]] = []
        command_started = False
        raw_path = args.output_dir / "cancel.events.raw.jsonl"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        with raw_path.open("w") as raw:
            for notification in handle.stream():
                event = _notification_record(notification)
                records.append(event)
                raw.write(json.dumps(event, sort_keys=True) + "\n")
                raw.flush()
                item = event["params"].get("item", {})
                if (
                    event["method"] == "item/started"
                    and item.get("type") == "commandExecution"
                    and not interrupt_sent.is_set()
                ):
                    command_started = True
                    handle.interrupt()
                    interrupt_sent.set()
        interrupt_sent.set()
        watchdog.join(timeout=1)
        runtime = codex.metadata

    time.sleep(2)
    lingering = _matching_processes(marker)
    status = _turn_status(records)
    record = {
        "thread_id": thread.id,
        "turn_id": handle.id,
        "command_started": command_started,
        "interrupt_sent": interrupt_sent.is_set(),
        "fallback_error": fallback_error,
        "turn_status": status,
        "lingering_processes_after_2s": lingering,
        "event_count": len(records),
        "event_methods": sorted(set(_event_methods(records))),
        "runtime": runtime,
    }
    _write_json(args.output_dir / "cancel.json", record)
    if not command_started:
        raise AssertionError("the long-running child command never started")
    if status != "interrupted":
        raise AssertionError(f"cancelled turn ended as {status!r}, not 'interrupted'")
    if lingering:
        raise AssertionError(f"cancelled command still has live processes: {lingering}")
    return _jsonable(record)


def cmd_subagent(args: argparse.Namespace) -> dict[str, Any]:
    output_type = _load_envelope_types(args.repo_root)["GenericOutput"]
    workspace = args.workspace.resolve()
    config = {
        "agents": {
            "enabled": True,
            "max_concurrent_threads_per_session": 1,
            "default_subagent_model": args.model,
            "default_subagent_reasoning_effort": args.effort,
        }
    }
    with _new_codex(workspace) as codex:
        thread = _start_thread(codex, workspace, args.model, config=config)
        handle = thread.turn(
            "Spawn exactly one explorer subagent with inherited model and effort. Tell it: do not "
            "load any orchestrator skill, do not spawn another agent, do not modify files, and "
            "reply only with `child-ok`. Wait for it to finish. Then return GenericOutput JSON with "
            "status='success', artifacts=[], notes_for_next_agent containing the child's result, and "
            "the required developer-instruction summary.",
            effort=args.effort,
            output_schema=_output_schema(output_type),
        )
        records, final_response = _consume_stream(
            handle, args.output_dir / "subagent.events.raw.jsonl"
        )
        collab_items = []
        activity_items = []
        for event in records:
            item = event["params"].get("item", {})
            if item.get("type") == "collabAgentToolCall":
                collab_items.append(item)
            if item.get("type") == "subAgentActivity":
                activity_items.append(item)
        if not activity_items:
            raise AssertionError("no subAgentActivity event was observed")
        receiver_ids = {
            item["agentThreadId"]
            for item in activity_items
            if item.get("agentThreadId")
        }
        if len(receiver_ids) != 1:
            raise AssertionError(
                f"expected one child thread, got {sorted(receiver_ids)}"
            )
        activity_kinds = {item.get("kind") for item in activity_items}
        if not {"started", "completed"} <= activity_kinds:
            raise AssertionError(
                f"child lifecycle was incomplete: {sorted(str(kind) for kind in activity_kinds)}"
            )
        if not final_response:
            raise AssertionError("subagent parent emitted no final structured response")
        envelope = output_type.model_validate_json(final_response)
        if "child-ok" not in envelope.notes_for_next_agent:
            raise AssertionError("parent did not return the completed child result")
        usage_events = [
            event["params"]
            for event in records
            if event["method"] == "thread/tokenUsage/updated"
        ]
        record = {
            "thread_id": thread.id,
            "turn_id": handle.id,
            "child_thread_ids": sorted(receiver_ids),
            "subagent_activity_items": activity_items,
            "collab_items": collab_items,
            "usage_events": usage_events,
            "child_usage_separately_attributed": any(
                event.get("threadId") in receiver_ids for event in usage_events
            ),
            "envelope": envelope,
            "runtime": codex.metadata,
        }
    _write_json(args.output_dir / "subagent.json", record)
    return _jsonable(record)


def _run_child(args: argparse.Namespace, command: str) -> dict[str, Any]:
    argv = [
        sys.executable,
        str(Path(__file__).resolve()),
        command,
        "--output-dir",
        str(args.output_dir),
        "--workspace",
        str(args.workspace),
        "--repo-root",
        str(args.repo_root),
        "--model",
        args.model,
        "--effort",
        args.effort,
        "--state",
        str(args.state),
    ]
    completed = subprocess.run(argv, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            f"probe command {command!r} failed ({completed.returncode}):\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return json.loads(completed.stdout)


def cmd_all(args: argparse.Namespace) -> dict[str, Any]:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.workspace.mkdir(parents=True, exist_ok=True)
    results = {
        command: _run_child(args, command)
        for command in (
            "preflight",
            "baseline",
            "resume",
            "stream",
            "cancel",
            "subagent",
        )
    }
    _write_json(args.output_dir / "summary.json", results)
    return results


COMMANDS = {
    "all": cmd_all,
    "preflight": cmd_preflight,
    "baseline": cmd_baseline,
    "resume": cmd_resume,
    "stream": cmd_stream,
    "cancel": cmd_cancel,
    "subagent": cmd_subagent,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--repo-root", type=Path, default=_repo_root())
    parser.add_argument(
        "--model", default=os.environ.get("SSSF_M0_MODEL", DEFAULT_MODEL)
    )
    parser.add_argument(
        "--effort", default=os.environ.get("SSSF_M0_EFFORT", DEFAULT_EFFORT)
    )
    parser.add_argument("--state", type=Path)
    args = parser.parse_args()
    args.output_dir = args.output_dir.resolve()
    args.repo_root = args.repo_root.resolve()
    args.workspace = (args.workspace or args.output_dir / "workspace").resolve()
    args.state = (args.state or args.output_dir / "state.json").resolve()
    return args


def main() -> None:
    args = parse_args()
    try:
        result = COMMANDS[args.command](args)
    except Exception as error:
        print(json.dumps({"ok": False, "command": args.command, "error": repr(error)}))
        raise
    print(
        json.dumps(
            {"ok": True, "command": args.command, "result": result}, sort_keys=True
        )
    )


if __name__ == "__main__":
    main()
