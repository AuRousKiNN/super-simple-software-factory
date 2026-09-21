"""Strict config loading and Codex-backed agent execution."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional

import yaml

from . import permissions, prompts
from .agent_codex import SDK_VERSION, RuntimeHooks
from .codex_events import ToolCallTracker
from .codex_schema import strict_output_schema
from .data_types import (
    AgentCall,
    AgentConfig,
    AgentRunRequest,
    AgentRunResult,
    EnvelopeBase,
    EventRecord,
    GateCheck,
    GateReport,
    Phase,
    SSSFConfig,
    UsageBreakdown,
)
from .utils import new_id

JSON_FIX_ATTEMPTS = 2
MAX_TURNS_PER_PHASE = 12
_INHERITED_AGENT_FIELDS = (
    "coding_agent", "model", "thinking", "color", "writes",
)


class GateFailure(RuntimeError):
    pass


class AgentRuntimeFailure(RuntimeError):
    pass


def load_config(path: str = "adws/adw_sssf_config/sssf.config.yaml") -> SSSFConfig:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError("SSSF config root must be an object")
    defaults = raw.get("defaults", {}) or {}
    if not isinstance(defaults, dict):
        raise ValueError("defaults must be an object")
    merged_agents = []
    for original in raw.get("agents", []) or []:
        if not isinstance(original, dict):
            raise ValueError("every agents entry must be an object")
        agent = dict(original)
        for key in _INHERITED_AGENT_FIELDS:
            if key not in agent and key in defaults:
                agent[key] = defaults[key]
        inherited_subagents = defaults.get("subagents", {}) or {}
        local_subagents = agent.get("subagents", {}) or {}
        if not isinstance(inherited_subagents, dict) or not isinstance(local_subagents, dict):
            raise ValueError("subagents must be an object")
        agent["subagents"] = {**inherited_subagents, **local_subagents}
        merged_agents.append(agent)
    candidate = dict(raw)
    candidate["agents"] = merged_agents
    return SSSFConfig.model_validate(candidate)


def resolve(cfg: SSSFConfig, name: str) -> AgentConfig:
    for agent in cfg.agents:
        if agent.name == name:
            return agent
    raise SystemExit(
        f"agent {name!r} is not defined in the config — "
        f"available: {[a.name for a in cfg.agents]}"
    )


def validate(cfg: SSSFConfig, required: list[str]) -> None:
    """Fail before a business turn when M1 cannot honor the configuration."""
    problems = []
    if cfg.defaults.subagents.enabled:
        problems.append("defaults.subagents.enabled must remain false until M3")
    if cfg.codex.command_network_access:
        problems.append("codex.command_network_access=true is not supported in M1")
    for name in required:
        try:
            agent = resolve(cfg, name)
        except SystemExit as error:
            problems.append(str(error))
            continue
        if agent.subagents.enabled:
            problems.append(f"agent {name!r}: subagents must remain disabled until M3")
        for label, ref in (
            ("system", agent.prompt_engineering.system),
            ("user", agent.prompt_engineering.user),
        ):
            if not Path(ref).is_file():
                problems.append(f"agent {name!r}: {label} prompt not found: {ref}")
    if problems:
        raise SystemExit("config validation failed:\n- " + "\n- ".join(problems))


def execute(run, phase: Phase, call: AgentCall) -> EnvelopeBase:
    """Render prompts, run bounded Codex turns, validate gates, return envelope."""
    agent = resolve(run.cfg, phase.params.owner)
    agent_dir = run.session_dir / agent.name
    agent_dir.mkdir(parents=True, exist_ok=True)
    invocation_id = f"inv_{new_id(12)}"
    invocation_dir = agent_dir / "invocations" / invocation_id
    invocation_dir.mkdir(parents=True, exist_ok=False)
    report_dir = invocation_dir / "reports"
    report_dir.mkdir()
    run.active_report_dir = report_dir

    variables = {
        "prompt": call.prompt,
        "previous_envelope": (
            call.previous.model_dump_json(indent=2) if call.previous else "(none)"
        ),
        "context_handoff_dir": str(run.context_handoff_dir),
    }
    system_text = prompts.render(agent.prompt_engineering.system, variables)
    user_text = prompts.render(agent.prompt_engineering.user, variables)
    prompts.save(invocation_dir / "prompts", "system.md", system_text)
    prompts.save(invocation_dir / "prompts", "user.md", user_text)
    # Compatibility view for the existing prompt endpoint.
    prompts.save(agent_dir / "prompts", "system.md", system_text)
    prompts.save(agent_dir / "prompts", "user.md", user_text)
    output_schema = strict_output_schema(call.output_type)
    (invocation_dir / "output.schema.json").write_text(
        json.dumps(output_schema, indent=2, sort_keys=True) + "\n"
    )

    fingerprint = _config_fingerprint(run, agent, system_text)
    mapped = _compatible_mapping(run, agent, fingerprint)
    thread_id = str(mapped.get("thread_id") or "") if mapped else ""
    usage_baseline = (
        UsageBreakdown.model_validate(mapped["usage_baseline"])
        if mapped and mapped.get("usage_baseline") else None
    )
    latest: AgentRunResult | None = None
    spent = UsageBreakdown(uncached_input_tokens=0, cost=0.0, cost_kind="reported")
    turn_count = 0
    succeeded = False

    run.tracer.event(EventRecord(
        adw_id=run.adw_id,
        phase_id=phase.phase_id,
        type="agent_start",
        name=agent.name,
        payload={
            "model": agent.model,
            "thinking": agent.thinking,
            "color": agent.color,
            "thread_id": thread_id or None,
            "coding_agent": agent.coding_agent,
            "purpose": agent.purpose,
            "config_fingerprint": fingerprint,
            "invocation_id": invocation_id,
            "subagents_enabled": False,
        },
    ))
    run.console.agent_started(agent.name, agent.model, thread_id or "pending")

    def save_mapping(state: str, *, last_turn_id: str = "", runtime_version: str = SDK_VERSION) -> None:
        if not thread_id:
            return
        entry = {
            "backend": "codex",
            "thread_id": thread_id,
            "model": agent.model,
            "runtime_version": runtime_version or SDK_VERSION,
            "config_fingerprint": fingerprint,
            "repo_root": str(run.repo_root.resolve()),
            "last_turn_id": last_turn_id,
            "state": state,
        }
        if usage_baseline is not None:
            entry["usage_baseline"] = usage_baseline.model_dump()
        run.save_agent_map(agent.name, entry)

    def on_thread_ready(new_thread_id: str, runtime_version: str, resumed: bool) -> None:
        nonlocal thread_id
        if resumed and thread_id and new_thread_id != thread_id:
            raise RuntimeError(
                f"resume returned thread {new_thread_id!r}, expected {thread_id!r}"
            )
        thread_id = new_thread_id
        save_mapping("idle", runtime_version=runtime_version)

    def on_turn_started(turn_id: str) -> None:
        save_mapping("active", last_turn_id=turn_id)

    def on_turn_finished(result: AgentRunResult) -> None:
        nonlocal usage_baseline
        if result.cumulative_usage is not None:
            usage_baseline = result.cumulative_usage
        state = "idle" if result.status == "completed" else "failed"
        save_mapping(
            state,
            last_turn_id=result.turn_id,
            runtime_version=result.runtime_version or SDK_VERSION,
        )

    hooks = RuntimeHooks(
        on_thread_ready=on_thread_ready,
        on_turn_started=on_turn_started,
        on_event=_event_forwarder(run, phase, agent.name, invocation_id),
        on_turn_finished=on_turn_finished,
    )

    def send(prompt_text: str) -> AgentRunResult:
        nonlocal latest, turn_count
        turn_count += 1
        if turn_count > MAX_TURNS_PER_PHASE:
            raise AgentRuntimeFailure(
                f"{agent.name} exceeded the phase limit of {MAX_TURNS_PER_PHASE} Codex turns"
            )
        turn_invocation_id = f"{invocation_id}_turn_{turn_count:02d}"
        records_invocations = all(
            hasattr(run.tracer, name)
            for name in ("agent_invocation_start", "agent_invocation_finish")
        )
        if records_invocations:
            run.tracer.agent_invocation_start(
                turn_invocation_id, phase, agent.name, SDK_VERSION,
            )
        try:
            result = run.runtime.run_turn(
                AgentRunRequest(
                    role=agent.name,
                    cwd=str(run.repo_root),
                    model=agent.model,
                    effort=agent.thinking,
                    developer_instructions=system_text,
                    prompt=prompt_text,
                    output_schema=output_schema,
                    raw_output_path=str((invocation_dir / "raw_output.jsonl").resolve()),
                    thread_id=thread_id or None,
                    usage_baseline=usage_baseline,
                ),
                hooks,
            )
        except BaseException as error:
            interrupted = isinstance(error, (KeyboardInterrupt, SystemExit))
            result = AgentRunResult(
                thread_id=thread_id,
                status="interrupted" if interrupted else "failed",
                error=RuntimeErrorInfo(
                    kind="interrupted" if interrupted else "runtime",
                    message=str(error) or type(error).__name__,
                ),
                runtime_version=SDK_VERSION,
            )
            if records_invocations:
                run.tracer.agent_invocation_finish(
                    turn_invocation_id, run.adw_id, result, result.usage,
                )
            save_mapping("failed", runtime_version=SDK_VERSION)
            raise
        latest = result
        settled = True
        if records_invocations:
            settled = run.tracer.agent_invocation_finish(
                turn_invocation_id, run.adw_id, result, result.usage,
            )
        if settled:
            spent.merge(result.usage)
            try:
                run.add_usage(result.usage, persist=not records_invocations)
            except TypeError:
                # Lightweight fake runs from downstream tests predate the
                # optional persistence flag.
                run.add_usage(result.usage)
        if result.status != "completed":
            detail = result.error.message if result.error else "no structured runtime error"
            kind = result.error.kind if result.error else result.status
            raise AgentRuntimeFailure(
                f"{agent.name} Codex turn {result.turn_id or '(not started)'} "
                f"ended as {result.status} ({kind}): {detail}"
            )
        return result

    tree_before = permissions.snapshot(run)
    permission_breach_recorded = False

    def check_permissions(*, log_paths: bool = False) -> list[str]:
        nonlocal permission_breach_recorded
        try:
            touched = permissions.enforce(run, phase, agent, tree_before)
        except permissions.PermissionBreach as breach:
            if not permission_breach_recorded:
                permission_breach_recorded = True
                run.tracer.event(EventRecord(
                    adw_id=run.adw_id,
                    phase_id=phase.phase_id,
                    type="error",
                    name="permission_breach",
                    payload={
                        "agent": agent.name,
                        "error": str(breach),
                        "writes": agent.writes,
                        "protected_files": run.cfg.defaults.protected_files,
                    },
                ))
            raise
        if touched and log_paths:
            run.tracer.event(EventRecord(
                adw_id=run.adw_id,
                phase_id=phase.phase_id,
                type="log",
                name="paths_touched",
                payload={"agent": agent.name, "paths": touched},
            ))
        return touched

    original_send = send

    def send(prompt_text: str) -> AgentRunResult:
        try:
            return original_send(prompt_text)
        finally:
            # Runtime failures, timeouts, and cancellations are still writes-capable
            # turns.  Verify them before any parse/gate retry can begin.
            check_permissions()

    try:
        result = send(user_text)
        envelope, attempt = _parse_with_retries(run, phase, call, result, send)

        for gate_attempt in range(1, max(1, phase.params.retries + 1) + 1):
            violations = []
            for gate in call.gates:
                report = _as_report(gate(envelope, run))
                found = report.violations
                run.tracer.gate_row(phase, gate.__name__, report, gate_attempt)
                run.tracer.event(EventRecord(
                    adw_id=run.adw_id,
                    phase_id=phase.phase_id,
                    type="gate_fail" if found else "gate_pass",
                    name=gate.__name__,
                    payload={
                        "attempt": gate_attempt,
                        "violations": found,
                        "checks": [check.model_dump() for check in report.checks],
                    },
                ))
                run.console.gate_result(gate.__name__, report)
                violations.extend(found)
            if not violations:
                break
            if gate_attempt > phase.params.retries:
                raise GateFailure(
                    f"{agent.name} failed gates after {gate_attempt} attempt(s):\n- "
                    + "\n- ".join(violations)
                )
            phase.attempt = gate_attempt
            run.console.retry(
                agent.name,
                gate_attempt,
                phase.params.retries,
                f"{len(violations)} gate violation(s)",
            )
            result = send(
                "Your previous response failed validation:\n- "
                + "\n- ".join(violations)
                + "\n\nFix these problems, then re-emit ONLY your Report JSON."
            )
            envelope, attempt = _parse_with_retries(run, phase, call, result, send)

        check_permissions(log_paths=True)

        _persist_envelope(run, phase, agent.name, call, envelope, attempt, valid=True)
        run.console.envelope_summary(envelope)
        if envelope.status != "success":
            raise RuntimeError(
                f"{agent.name} reported status={envelope.status!r}: {envelope.summary}"
            )
        run.tracer.event(EventRecord(
            adw_id=run.adw_id,
            phase_id=phase.phase_id,
            type="handoff",
            name=agent.name,
            payload={"artifacts": envelope.artifacts, "summary": envelope.summary},
        ))
        succeeded = True
        return envelope
    finally:
        final_permission_error: BaseException | None = None
        try:
            # Catch host exceptions between turns as well as the final successful
            # turn, then destroy the out-of-repo recovery copy.
            check_permissions()
        except BaseException as error:
            final_permission_error = error
        finally:
            cleanup_snapshot = getattr(tree_before, "cleanup", None)
            if cleanup_snapshot:
                cleanup_snapshot()
            run.active_report_dir = None
        context = latest or AgentRunResult(thread_id=thread_id)
        if thread_id:
            run.tracer.agent_session_row(
                run.adw_id,
                agent,
                thread_id,
                context_tokens=context.context_tokens,
                context_window=context.context_window,
            )
        run.tracer.event(EventRecord(
            adw_id=run.adw_id,
            phase_id=phase.phase_id,
            type="agent_end",
            name=agent.name,
            tokens=spent.total_tokens,
            payload={
                "status": "success" if succeeded else "failed",
                "cost": spent.cost,
                "cost_kind": spent.cost_kind,
                "usage": spent.model_dump(),
                "thread_id": thread_id or None,
                "turn_id": context.turn_id or None,
                "context_tokens": context.context_tokens,
                "context_window": context.context_window,
            },
        ))
        run.console.agent_finished(agent.name, spent.total_tokens, spent.cost, succeeded)
        if final_permission_error is not None:
            raise final_permission_error


def _as_report(result) -> GateReport:
    if isinstance(result, GateReport):
        return result
    return GateReport(
        checks=[GateCheck(item=str(value), ok=False) for value in (result or [])]
    )


def _config_fingerprint(run, agent: AgentConfig, system_text: str) -> str:
    payload = {
        "backend": "codex",
        "repo_root": str(run.repo_root.resolve()),
        "model": agent.model,
        "thinking": agent.thinking,
        "developer_instructions_sha256": hashlib.sha256(system_text.encode()).hexdigest(),
        "writes": agent.writes,
        "protected_files": run.cfg.defaults.protected_files,
        "subagents": agent.subagents.model_dump(),
        "codex": run.cfg.codex.model_dump(),
        "sdk_version": SDK_VERSION,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _compatible_mapping(run, agent: AgentConfig, fingerprint: str) -> Optional[dict]:
    entry = run.agent_entry(agent.name)
    if entry is None:
        return None
    expected = {
        "backend": "codex",
        "model": agent.model,
        "runtime_version": SDK_VERSION,
        "config_fingerprint": fingerprint,
        "repo_root": str(run.repo_root.resolve()),
    }
    mismatches = [
        f"{key}: saved={entry.get(key)!r}, current={value!r}"
        for key, value in expected.items()
        if entry.get(key) != value
    ]
    if not entry.get("thread_id"):
        mismatches.append("thread_id is missing")
    if entry.get("state") != "idle":
        mismatches.append(
            f"state: saved={entry.get('state')!r}, only an idle thread can be resumed"
        )
    if mismatches:
        raise RuntimeError(
            f"saved Codex thread for agent {agent.name!r} is incompatible; "
            "start with a new adw_id or explicitly reset its mapping:\n- "
            + "\n- ".join(mismatches)
        )
    return entry


def _event_forwarder(run, phase: Phase, agent_name: str, invocation_id: str = ""):
    tracker = ToolCallTracker(invocation_id)

    def forward(event: dict) -> None:
        for record in tracker.observe(event):
            run.tracer.event(EventRecord(
                adw_id=run.adw_id,
                phase_id=phase.phase_id,
                type="tool_call",
                name=record.pop("label"),
                started_at=record.pop("started_at", None),
                ended_at=record.pop("ended_at", None),
                payload={**record, "agent": agent_name},
            ))
    return forward


def _parse_with_retries(run, phase: Phase, call: AgentCall, result, send):
    for attempt in range(1, JSON_FIX_ATTEMPTS + 2):
        try:
            payload = json.loads(result.text)
            if not isinstance(payload, dict):
                raise ValueError("the final JSON value is not an object")
            return call.output_type.model_validate(payload), attempt
        except Exception as error:
            _persist_envelope(
                run, phase, phase.params.owner, call, None, attempt,
                valid=False, raw=result.text,
            )
            if attempt > JSON_FIX_ATTEMPTS:
                raise RuntimeError(
                    f"{phase.params.owner} never produced valid "
                    f"{call.output_type.__name__} JSON: {error}"
                ) from error
            run.console.retry(
                phase.params.owner,
                attempt,
                JSON_FIX_ATTEMPTS,
                f"invalid {call.output_type.__name__} JSON: {error}",
            )
            result = send(
                f"Your response was not valid JSON for the required "
                f"{call.output_type.__name__} structure ({error}). Respond again "
                "with ONLY a JSON object that satisfies the same schema. No prose "
                "and no code fences."
            )


def _persist_envelope(
    run,
    phase: Phase,
    agent_name: str,
    call: AgentCall,
    envelope: Optional[EnvelopeBase],
    attempt: int,
    valid: bool,
    raw: str = "",
) -> None:
    payload_json = (
        envelope.model_dump_json(indent=2)
        if envelope
        else json.dumps({"raw": raw[-2000:]})
    )
    run.tracer.envelope_row(
        phase, agent_name, call.output_type.__name__, payload_json, valid, attempt,
    )
    if envelope:
        record = {
            "agent_name": agent_name,
            "purpose": resolve(run.cfg, agent_name).purpose,
            "output_type": call.output_type.__name__,
            "attempt": attempt,
            **envelope.model_dump(),
        }
        (run.session_dir / agent_name / "envelope.json").write_text(
            json.dumps(record, indent=2) + "\n"
        )
        active_report_dir = getattr(run, "active_report_dir", None)
        if active_report_dir:
            (Path(active_report_dir).parent / "envelope.json").write_text(
                json.dumps(record, indent=2) + "\n"
            )
