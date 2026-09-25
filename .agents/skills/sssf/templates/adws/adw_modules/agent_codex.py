"""Thin production adapter over the pinned OpenAI Codex Python SDK."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any, Callable, Optional

from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox

from .codex_events import CodexEventCollector, classify_error
from .data_types import (
    AgentRunRequest,
    AgentRunResult,
    CodexRuntimeConfig,
    RuntimeCapabilities,
    RuntimeErrorInfo,
)

SDK_VERSION = "0.155.1"


@dataclass(slots=True)
class RuntimeHooks:
    on_thread_ready: Optional[Callable[[str, str, bool], None]] = None
    on_turn_started: Optional[Callable[[str], None]] = None
    on_event: Optional[Callable[[dict[str, Any]], None]] = None
    on_turn_finished: Optional[Callable[[AgentRunResult], None]] = None


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    start_marker: str
    command: str


def process_start_marker(pid: int) -> str:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart="],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _process_command(pid: int) -> str:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


class ProcessSupervisor:
    """Own only app-server children created by this SDK client instance."""

    def __init__(
        self,
        *,
        on_start: Optional[Callable[[int, str, str], None]] = None,
        on_exit: Optional[Callable[[int, str], None]] = None,
    ) -> None:
        self.on_start = on_start
        self.on_exit = on_exit
        self._owned: dict[int, ProcessIdentity] = {}

    def direct_children(self) -> dict[int, ProcessIdentity]:
        rows: dict[int, ProcessIdentity] = {}
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return rows
        parent = os.getpid()
        for line in result.stdout.splitlines():
            match = re.match(r"\s*(\d+)\s+(\d+)\s+(.*)", line)
            if not match or int(match.group(2)) != parent:
                continue
            pid = int(match.group(1))
            command = match.group(3).strip()
            if "codex" not in command.casefold():
                continue
            marker = self._start_marker(pid)
            if marker:
                rows[pid] = ProcessIdentity(pid, marker, command)
        return rows

    @staticmethod
    def _start_marker(pid: int) -> str:
        return process_start_marker(pid)

    def claim_new(self, before: set[int]) -> None:
        for pid, identity in self.direct_children().items():
            if pid in before or pid in self._owned:
                continue
            self._owned[pid] = identity
            if self.on_start:
                self.on_start(pid, identity.start_marker, identity.command)

    def _alive(self, identity: ProcessIdentity) -> bool:
        return (
            self._start_marker(identity.pid) == identity.start_marker
            and _process_command(identity.pid) == identity.command
        )

    def reap(self) -> None:
        for pid, identity in list(self._owned.items()):
            if self._alive(identity):
                continue
            self._owned.pop(pid, None)
            if self.on_exit:
                self.on_exit(pid, identity.start_marker)

    def terminate_all(self, grace_s: float) -> None:
        self.reap()
        for identity in list(self._owned.values()):
            if self._alive(identity):
                try:
                    os.kill(identity.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + max(0.0, grace_s)
        while self._owned and time.monotonic() < deadline:
            self.reap()
            if self._owned:
                time.sleep(0.05)
        for identity in list(self._owned.values()):
            if self._alive(identity):
                try:
                    os.kill(identity.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        kill_deadline = time.monotonic() + 1.0
        while self._owned and time.monotonic() < kill_deadline:
            self.reap()
            if self._owned:
                time.sleep(0.05)


class CodexRuntime:
    """Own one SDK connection for an ADW process and run synchronous turns."""

    def __init__(
        self,
        config: CodexRuntimeConfig,
        repo_root: str | Path,
        *,
        codex_factory: Callable[[CodexConfig], Any] = Codex,
        version_provider: Callable[[str], str] = version,
        on_process_start: Optional[Callable[[int, str, str], None]] = None,
        on_process_exit: Optional[Callable[[int, str], None]] = None,
        process_supervisor: Optional[ProcessSupervisor] = None,
    ) -> None:
        self.config = config
        self.repo_root = str(Path(repo_root).resolve())
        self._codex_factory = codex_factory
        self._version_provider = version_provider
        self._codex: Any = None
        self._threads: dict[str, Any] = {}
        self._preflight: dict[tuple[str, str], RuntimeCapabilities] = {}
        self._active_threads: set[str] = set()
        self._lock = threading.Lock()
        self._closed = False
        self._processes = process_supervisor or ProcessSupervisor(
            on_start=on_process_start,
            on_exit=on_process_exit,
        )
        if self.config.command_network_access:
            raise ValueError(
                "command_network_access=true is unsupported; "
                "use auto_review for approval of sandbox-boundary requests"
            )

    def _ensure_client(self) -> Any:
        if self._closed:
            raise RuntimeError("Codex runtime is already closed")
        if self._codex is not None:
            return self._codex
        installed = self._version_provider("openai-codex")
        if installed != SDK_VERSION:
            raise RuntimeError(
                f"openai-codex version mismatch: expected {SDK_VERSION}, got {installed}"
            )
        codex_path = os.environ.get("CODEX_PATH") or None
        launch = CodexConfig(
            codex_bin=codex_path,
            cwd=self.repo_root,
            experimental_api=True,
        )
        before = set(self._processes.direct_children())
        outcome: dict[str, Any] = {}

        def launch_client() -> None:
            client: Any = None
            try:
                client = self._codex_factory(launch)
                if self.config.auth == "api_key":
                    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
                    if not api_key:
                        raise RuntimeError("codex.auth=api_key requires OPENAI_API_KEY")
                    client.login_api_key(api_key)
                outcome["client"] = client
            except BaseException as error:
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass
                outcome["error"] = error

        launcher = threading.Thread(target=launch_client, daemon=True)
        launcher.start()
        deadline = time.monotonic() + self.config.startup_timeout_s
        while launcher.is_alive() and time.monotonic() < deadline:
            launcher.join(0.05)
            self._processes.claim_new(before)
        self._processes.claim_new(before)
        if launcher.is_alive():
            self._processes.terminate_all(self.config.shutdown_grace_s)
            raise RuntimeError(
                f"Codex runtime startup exceeded {self.config.startup_timeout_s}s"
            )
        if "error" in outcome:
            self._processes.terminate_all(self.config.shutdown_grace_s)
            raise outcome["error"]
        client = outcome["client"]
        self._codex = client
        return self._codex

    def preflight(self, model: str, effort: str) -> RuntimeCapabilities:
        key = (model, effort)
        if key in self._preflight:
            return self._preflight[key]
        codex = self._ensure_client()
        runtime_version = _runtime_version(codex.metadata)
        if runtime_version != SDK_VERSION:
            source = "CODEX_PATH" if os.environ.get("CODEX_PATH") else "bundled runtime"
            raise RuntimeError(
                f"{source} version mismatch: expected {SDK_VERSION}, got {runtime_version or 'unknown'}"
            )
        selected = next((entry for entry in codex.models().data if entry.id == model), None)
        if selected is None:
            raise RuntimeError(f"model {model!r} is not available to the current Codex account")
        efforts = {
            str(getattr(option, "reasoning_effort", ""))
            for option in selected.supported_reasoning_efforts
        }
        efforts |= {
            str(getattr(getattr(option, "reasoning_effort", None), "value", ""))
            for option in selected.supported_reasoning_efforts
        }
        if effort not in efforts:
            raise RuntimeError(
                f"model {model!r} does not support reasoning effort {effort!r}"
            )
        capabilities = RuntimeCapabilities(
            sdk_version=SDK_VERSION,
            runtime_version=runtime_version,
            model=model,
            effort=effort,
        )
        self._preflight[key] = capabilities
        return capabilities

    def run_turn(self, request: AgentRunRequest, hooks: RuntimeHooks) -> AgentRunResult:
        collector = CodexEventCollector(
            request.raw_output_path,
            request.usage_baseline,
            request.subagents,
        )
        thread_id = request.thread_id or ""
        turn_id = ""
        runtime_version = ""
        acquired = False
        try:
            capabilities = self.preflight(request.model, request.effort)
            runtime_version = capabilities.runtime_version
            codex = self._ensure_client()
            resumed = bool(request.thread_id)
            if request.thread_id and request.thread_id in self._threads:
                thread = self._threads[request.thread_id]
            elif request.thread_id:
                thread = codex.thread_resume(
                    request.thread_id,
                    approval_mode=ApprovalMode(self.config.approval_mode),
                    config=_thread_config(request.subagents),
                    cwd=request.cwd,
                    developer_instructions=request.developer_instructions,
                    model=request.model,
                    sandbox=Sandbox.workspace_write,
                )
                self._threads[thread.id] = thread
            else:
                thread = codex.thread_start(
                    approval_mode=ApprovalMode(self.config.approval_mode),
                    config=_thread_config(request.subagents),
                    cwd=request.cwd,
                    developer_instructions=request.developer_instructions,
                    model=request.model,
                    sandbox=Sandbox.workspace_write,
                    service_name="sssf",
                )
                self._threads[thread.id] = thread
            thread_id = thread.id
            if hooks.on_thread_ready:
                hooks.on_thread_ready(thread_id, runtime_version, resumed)

            with self._lock:
                if thread_id in self._active_threads:
                    raise RuntimeError(f"thread {thread_id} already has an active turn")
                self._active_threads.add(thread_id)
                acquired = True

            handle = thread.turn(
                request.prompt,
                approval_mode=ApprovalMode(self.config.approval_mode),
                effort=request.effort,
                output_schema=request.output_schema,
                sandbox=Sandbox.workspace_write,
            )
            turn_id = handle.id
            collector.bind_parent(thread_id, turn_id)
            if hooks.on_turn_started:
                hooks.on_turn_started(turn_id)

            timed_out = threading.Event()
            force_timer: Optional[threading.Timer] = None

            def interrupt_for_timeout() -> None:
                nonlocal force_timer
                timed_out.set()
                try:
                    handle.interrupt()
                except Exception:
                    pass
                force_timer = threading.Timer(
                    self.config.shutdown_grace_s,
                    lambda: self._processes.terminate_all(0),
                )
                force_timer.daemon = True
                force_timer.start()

            timer = threading.Timer(self.config.turn_timeout_s, interrupt_for_timeout)
            timer.daemon = True
            timer.start()
            try:
                for notification in handle.stream():
                    record = collector.observe(notification)
                    if hooks.on_event:
                        hooks.on_event(record)
            finally:
                timer.cancel()
                if force_timer is not None:
                    force_timer.cancel()

            status = collector.status
            error = collector.error
            if timed_out.is_set():
                status = "interrupted"
                error = RuntimeErrorInfo(
                    kind="timeout",
                    message=f"turn exceeded {self.config.turn_timeout_s}s and was interrupted",
                )
            elif status == "interrupted" and error is None:
                error = RuntimeErrorInfo(kind="interrupted", message="turn was interrupted")
            elif status not in ("completed", "failed", "interrupted"):
                status = "outcome_unknown"
                error = error or RuntimeErrorInfo(
                    kind="outcome_unknown", message="turn ended without a recognized terminal state"
                )
            cleanup_forced = bool(collector.subagent_tracker.active_thread_ids)
            if cleanup_forced:
                # A parent terminal event with live children cannot be accepted.
                # Closing the owning app-server is the only public-SDK boundary
                # that guarantees those child turns do not outlive the phase.
                self._shutdown_client(self.config.shutdown_grace_s)
            result = AgentRunResult(
                thread_id=thread_id,
                turn_id=turn_id,
                text=collector.final_response,
                status=status,
                error=error,
                usage=collector.usage,
                cumulative_usage=collector.cumulative_usage,
                context_tokens=collector.context_tokens,
                context_window=collector.context_window,
                runtime_version=runtime_version,
                raw_event_count=collector.event_count,
                unknown_event_count=collector.unknown_event_count,
                subagents=collector.subagent_tracker.records,
                child_usage_attribution=collector.subagent_tracker.usage_attribution,
                subagent_cleanup_forced=cleanup_forced,
            )
        except Exception as exc:
            message = str(exc) or repr(exc)
            kind = classify_error(message)
            cleanup_forced = bool(collector.subagent_tracker.active_thread_ids)
            if cleanup_forced:
                self._shutdown_client(self.config.shutdown_grace_s)
            result = AgentRunResult(
                thread_id=thread_id,
                turn_id=turn_id,
                text=collector.final_response,
                status="failed",
                error=RuntimeErrorInfo(kind=kind, message=message),
                usage=collector.usage,
                cumulative_usage=collector.cumulative_usage,
                context_tokens=collector.context_tokens,
                context_window=collector.context_window,
                runtime_version=runtime_version,
                raw_event_count=collector.event_count,
                unknown_event_count=collector.unknown_event_count,
                subagents=collector.subagent_tracker.records,
                child_usage_attribution=collector.subagent_tracker.usage_attribution,
                subagent_cleanup_forced=cleanup_forced,
            )
        finally:
            if acquired:
                with self._lock:
                    self._active_threads.discard(thread_id)
        if hooks.on_turn_finished:
            hooks.on_turn_finished(result)
        return result

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._shutdown_client(self.config.shutdown_grace_s)

    def _shutdown_client(self, grace_s: float) -> None:
        """Close the current SDK connection and its owned app-server processes."""
        client = self._codex
        self._codex = None
        self._threads.clear()
        self._preflight.clear()
        if client is not None:
            finished = threading.Event()

            def close_client() -> None:
                try:
                    client.close()
                finally:
                    finished.set()

            closer = threading.Thread(target=close_client, daemon=True)
            closer.start()
            finished.wait(grace_s)
        self._processes.terminate_all(grace_s)


def _runtime_version(metadata: Any) -> str:
    server_info = getattr(metadata, "serverInfo", None) or getattr(metadata, "server_info", None)
    raw = str(getattr(server_info, "version", "") or "")
    # Runtime 0.155.1 may append platform and SDK transport metadata to the
    # version field. The release identity is still the leading exact semver.
    match = re.match(r"^(\d+\.\d+\.\d+)(?=$|[\s(])", raw)
    return match.group(1) if match else raw


def _thread_config(subagents) -> dict[str, Any]:
    """Build the per-role Codex config; disabled roles expose no agent tools."""
    return {
        "agents": {
            "enabled": subagents.enabled,
            "max_concurrent_threads_per_session": subagents.max_concurrent,
            "interrupt_message": True,
        },
        "web_search": "disabled",
    }
