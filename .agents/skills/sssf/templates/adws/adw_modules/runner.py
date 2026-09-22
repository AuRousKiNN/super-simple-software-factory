"""The Run object: config + adw_id + agent_map + tracer + console, bound once.

`run.phase(PhaseParams(...))` is the ONE phase primitive — a context manager
for all three kinds (engineer, agent, code). Success must be earned: every
phase defaults to fail; only a clean exit flips it (agent phases additionally
require a parsed envelope + green gates, enforced inside ph.call).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from . import agents, git_helper, permissions
from .agent_codex import CodexRuntime
from .console import Console
from .data_types import (AgentCall, EnvelopeBase, EventRecord, Phase, PhaseParams,
                         UsageBreakdown)
from .utils import ensure_dir, now_iso


class PhaseHandle:
    def __init__(self, run: "Run", phase: Phase):
        self.run = run
        self.phase = phase

    def log(self, **payload) -> None:
        self.run.tracer.event(EventRecord(adw_id=self.run.adw_id,
                                          phase_id=self.phase.phase_id,
                                          type="log", name=self.phase.params.name,
                                          payload=payload))
        self.run.console.note(", ".join(f"{k}: {v}" for k, v in payload.items()))
        if self.phase.params.kind == "engineer" and "input" in payload:
            self.run.tracer.session_request(self.run.adw_id, str(payload["input"]))

    def call(self, call: AgentCall) -> EnvelopeBase:
        if self.phase.params.kind != "agent":
            raise RuntimeError("ph.call() is only valid inside an agent phase")
        return agents.execute(self.run, self.phase, call)


class Run:
    def __init__(self, cfg, adw_id: str, tracer, engineer: str):
        self.cfg = cfg
        self.adw_id = adw_id
        self.tracer = tracer
        self.console = Console(tracer, adw_id)
        self.engineer = engineer
        self.phases: list[Phase] = []
        self.tokens = 0
        self.cost: float | None = 0.0
        self._seq = tracer.max_phase_seq(adw_id)   # a joined run continues the sequence
        self.repo_root = git_helper.repo_root()    # where every agent is spawned to work
        self.workspace_lock = permissions.acquire_workspace_lock(self.repo_root)
        self.session_dir = ensure_dir(Path(cfg.defaults.data_dir) / "sessions" / adw_id)
        self.context_handoff_dir = ensure_dir(self.session_dir / "context_handoff")
        self._agent_map_path = self.session_dir / "agent_map.json"
        self.agent_map = self._load_agent_map()
        try:
            self.runtime = CodexRuntime(
                cfg.codex,
                self.repo_root,
                on_process_start=lambda pid, marker, command: self.tracer.process_start(
                    self.adw_id, "agent", "codex-app-server", pid, command,
                    start_marker=marker,
                ),
                on_process_exit=lambda pid, marker: self.tracer.process_end(
                    self.adw_id, pid, start_marker=marker,
                ),
            )
        except BaseException:
            self.workspace_lock.release()
            raise
        self._closed = False

    # ── agent map (adw_id -> per-agent Codex thread ids) ──────────────────
    def _load_agent_map(self) -> dict:
        if not self._agent_map_path.exists():
            return {"schema_version": 2, "agents": {}}
        try:
            mapping = json.loads(self._agent_map_path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"cannot read {self._agent_map_path}: {error}") from error
        if (not isinstance(mapping, dict)
                or mapping.get("schema_version") != 2
                or not isinstance(mapping.get("agents"), dict)):
            raise RuntimeError(
                f"unsupported agent map at {self._agent_map_path}; expected schema_version=2. "
                "Use a new adw_id or explicitly remove the incompatible runtime mapping."
            )
        return mapping

    def agent_entry(self, agent: str) -> dict | None:
        entry = self.agent_map["agents"].get(agent)
        return dict(entry) if isinstance(entry, dict) else None

    def save_agent_map(self, agent: str, entry: dict) -> None:
        self.agent_map["agents"][agent] = entry
        self._agent_map_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=".agent_map.", suffix=".tmp", dir=self._agent_map_path.parent,
        )
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(self.agent_map, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._agent_map_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    # ── usage (run totals mirror what the tracer accumulates in sqlite) ─────
    def add_usage(self, usage: UsageBreakdown, *, persist: bool = True) -> None:
        self.tokens += usage.total_tokens
        if usage.cost is None:
            self.cost = None
        elif self.cost is not None:
            self.cost += usage.cost
        if persist:
            self.tracer.session_add_usage(self.adw_id, usage)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.runtime.close()
        finally:
            self.workspace_lock.release()

    # ── the phase primitive ─────────────────────────────────────────────────
    @contextmanager
    def phase(self, params: PhaseParams):
        self._seq += 1
        phase = Phase(phase_id=f"{self.adw_id}_{self._seq:02d}_{params.name}",
                      adw_id=self.adw_id, seq=self._seq, params=params,
                      status="running", started_at=now_iso())
        self.phases.append(phase)
        self.tracer.phase_upsert(phase)
        self.tracer.event(EventRecord(adw_id=self.adw_id, phase_id=phase.phase_id,
                                      type="phase_start", name=params.name,
                                      payload={"kind": params.kind, "owner": params.owner,
                                               "description": params.description}))
        self.console.phase_started(phase)
        clock = time.monotonic()
        try:
            yield PhaseHandle(self, phase)
        except BaseException as error:
            phase.status = "fail"                      # success must be earned
            phase.error = str(error)[:1000]
            phase.ended_at = now_iso()
            self.tracer.event(EventRecord(adw_id=self.adw_id, phase_id=phase.phase_id,
                                          type="error", name=params.name,
                                          payload={"error": phase.error}))
            self.tracer.event(EventRecord(adw_id=self.adw_id, phase_id=phase.phase_id,
                                          type="phase_end", name=params.name,
                                          payload={"status": "fail"}))
            self.tracer.phase_upsert(phase)
            self.tracer.session_finish(self.adw_id, ok=False)
            self.console.phase_ended(phase, time.monotonic() - clock)
            self.console.session_finished(False, self.tokens, self.cost,
                                          self.cfg.observability.db)
            self.close()
            raise
        else:
            phase.status = "success"
            phase.ended_at = now_iso()
            self.tracer.event(EventRecord(adw_id=self.adw_id, phase_id=phase.phase_id,
                                          type="phase_end", name=params.name,
                                          payload={"status": "success"}))
            self.tracer.phase_upsert(phase)
            self.console.phase_ended(phase, time.monotonic() - clock)

    # ── run outcome ─────────────────────────────────────────────────────────
    def finish(self, accepted: bool = True, reason: str = "") -> int:
        """Finalize the run and return its exit code. Call this exactly once.

        Two criteria, not one. Every phase must have passed, AND the ADW's own
        acceptance test must hold. They are different questions on purpose: a
        test phase that ran the suite did its job even when the suite came back
        red, so the PHASE succeeds while the RUN must not.

        This replaces a `succeeded` property that answered only the first
        question — and, being a property with side effects, wrote the session
        status and printed the banner before the caller's `and test.passed` was
        ever evaluated. A run whose suite never passed was recorded green in the
        db, on the terminal, and in the UI while exiting 1. Anyone reading the
        trace saw success; only a CI job checking `$?` saw the truth. One call
        now settles the db, the banner, and the exit code together, so the three
        cannot disagree.
        """
        phases_ok = bool(self.phases) and all(p.status == "success" for p in self.phases)
        ok = phases_ok and accepted
        self.accepted = ok
        if phases_ok and not accepted:
            note = reason or "the run's acceptance criterion was not met"
            self.tracer.event(EventRecord(
                adw_id=self.adw_id,
                phase_id=self.phases[-1].phase_id if self.phases else "",
                type="error", name="not_accepted", payload={"reason": note}))
            self.console.note(f"not accepted: {note}")
        try:
            self.tracer.session_finish(self.adw_id, ok=ok)
            self.console.session_finished(ok, self.tokens, self.cost,
                                          self.cfg.observability.db)
            return 0 if ok else 1
        finally:
            self.close()
