"""Codex notification serialization and the small M1 event accumulator."""

from __future__ import annotations

import dataclasses
import json
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel

from .data_types import (
    RuntimeErrorInfo,
    SubagentConfig,
    SubagentRun,
    UsageBreakdown,
)


def jsonable(value: Any) -> Any:
    """Serialize public SDK values without depending on private protocol types."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: jsonable(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


KNOWN_NOTIFICATION_METHODS = {
    "turn/started",
    "turn/completed",
    "item/started",
    "item/completed",
    "item/agentMessage/delta",
    "thread/tokenUsage/updated",
}


def _usage_from_wire(raw: Any) -> UsageBreakdown:
    values = raw if isinstance(raw, dict) else {}
    input_present = "inputTokens" in values
    cached_present = "cachedInputTokens" in values
    input_tokens = int(values.get("inputTokens") or 0)
    cached_tokens = int(values.get("cachedInputTokens") or 0)
    output_tokens = int(values.get("outputTokens") or 0)
    authoritative_total = values.get("totalTokens")
    total_tokens = (
        int(authoritative_total)
        if authoritative_total is not None
        else input_tokens + output_tokens
    )
    return UsageBreakdown(
        input_tokens=input_tokens,
        cached_input_tokens=cached_tokens,
        uncached_input_tokens=(
            max(0, input_tokens - cached_tokens)
            if input_present and cached_present else None
        ),
        output_tokens=output_tokens,
        reasoning_tokens=int(values.get("reasoningOutputTokens") or 0),
        total_tokens=total_tokens,
        cost=None,
        cost_kind="unknown",
    )


def _usage_delta(total: UsageBreakdown, baseline: UsageBreakdown) -> UsageBreakdown:
    def difference(current: int, previous: int) -> int:
        # A counter reset is not a negative bill. Treat the current value as a
        # fresh baseline so a resume never silently subtracts prior usage.
        return current - previous if current >= previous else current

    uncached = None
    if total.uncached_input_tokens is not None and baseline.uncached_input_tokens is not None:
        uncached = difference(total.uncached_input_tokens, baseline.uncached_input_tokens)
    return UsageBreakdown(
        input_tokens=difference(total.input_tokens, baseline.input_tokens),
        cached_input_tokens=difference(
            total.cached_input_tokens, baseline.cached_input_tokens,
        ),
        uncached_input_tokens=uncached,
        output_tokens=difference(total.output_tokens, baseline.output_tokens),
        reasoning_tokens=difference(total.reasoning_tokens, baseline.reasoning_tokens),
        total_tokens=difference(total.total_tokens, baseline.total_tokens),
        cost=None,
        cost_kind="unknown",
    )


class SubagentTracker:
    """Track child lifecycle, policy compliance, results and usage attribution."""

    _TERMINAL = {"completed", "interrupted", "failed", "shutdown", "not_found"}
    _STATUS = {
        "pendingInit": "running",
        "running": "running",
        "interrupted": "interrupted",
        "completed": "completed",
        "errored": "failed",
        "shutdown": "shutdown",
        "notFound": "not_found",
    }

    def __init__(self, policy: Optional[SubagentConfig] = None) -> None:
        self.policy = policy or SubagentConfig()
        self._children: dict[str, dict[str, Any]] = {}
        self._active: set[str] = set()
        self._seen_activity: set[tuple[str, str]] = set()
        self._seen_collab: set[tuple[str, str]] = set()
        self.violations: list[str] = []
        self.peak_concurrent = 0

    def _child(
        self,
        thread_id: str,
        *,
        parent_thread_id: str = "",
        parent_turn_id: str = "",
        role: str = "",
        agent_path: str = "",
    ) -> dict[str, Any]:
        child = self._children.setdefault(thread_id, {
            "thread_id": thread_id,
            "parent_thread_id": parent_thread_id,
            "parent_turn_id": parent_turn_id,
            "role": role,
            "agent_path": agent_path,
            "status": "unknown",
            "task": "",
            "model": None,
            "reasoning_effort": None,
            "result": "",
            "error": "",
            "usage": None,
        })
        if parent_thread_id:
            child["parent_thread_id"] = parent_thread_id
        if parent_turn_id:
            child["parent_turn_id"] = parent_turn_id
        if role:
            child["role"] = role
        if agent_path:
            child["agent_path"] = agent_path
        return child

    def _violate(self, message: str) -> None:
        if message not in self.violations:
            self.violations.append(message)

    def _activate(self, thread_id: str, child: dict[str, Any]) -> None:
        already_active = thread_id in self._active
        child["status"] = "running"
        self._active.add(thread_id)
        if already_active:
            return
        self.peak_concurrent = max(self.peak_concurrent, len(self._active))
        if not self.policy.enabled:
            self._violate(f"subagent {thread_id} spawned while subagents are disabled")
        if len(self._active) > self.policy.max_concurrent:
            self._violate(
                f"subagent concurrency {len(self._active)} exceeded "
                f"configured maximum {self.policy.max_concurrent}"
            )

    def observe(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        method = str(record.get("method") or "")
        if method not in {"item/started", "item/completed"}:
            return []
        params = record.get("params") or {}
        item = params.get("item") or {}
        item_type = item.get("type")
        if item_type == "subAgentActivity":
            return self._observe_activity(params, item)
        if item_type == "collabAgentToolCall":
            return self._observe_collab(method, params, item)
        return []

    def _observe_activity(
        self, params: dict[str, Any], item: dict[str, Any],
    ) -> list[dict[str, Any]]:
        thread_id = str(item.get("agentThreadId") or "")
        kind = str(item.get("kind") or "")
        if not thread_id or not kind:
            return []
        key = (thread_id, kind)
        if key in self._seen_activity:
            return []
        self._seen_activity.add(key)
        agent_path = str(item.get("agentPath") or "")
        child = self._child(
            thread_id,
            parent_thread_id=str(params.get("threadId") or ""),
            parent_turn_id=str(params.get("turnId") or ""),
            role=self.policy.role,
            agent_path=agent_path,
        )
        if kind == "started":
            self._activate(thread_id, child)
            event = "started"
        elif kind in {"completed", "interrupted"}:
            child["status"] = kind
            self._active.discard(thread_id)
            event = kind
        else:
            event = "interacted"
        return [{"event": event, **child}]

    def _observe_collab(
        self, method: str, params: dict[str, Any], item: dict[str, Any],
    ) -> list[dict[str, Any]]:
        item_id = str(item.get("id") or "")
        key = (item_id, method)
        if key in self._seen_collab:
            return []
        self._seen_collab.add(key)
        parent_thread_id = str(item.get("senderThreadId") or params.get("threadId") or "")
        parent_turn_id = str(params.get("turnId") or "")
        receiver_ids = [str(value) for value in item.get("receiverThreadIds") or []]
        if str(item.get("tool") or "") == "spawnAgent":
            for thread_id in receiver_ids:
                child = self._child(
                    thread_id,
                    parent_thread_id=parent_thread_id,
                    parent_turn_id=parent_turn_id,
                    role=self.policy.role,
                )
                child["task"] = str(item.get("prompt") or "")
                child["model"] = item.get("model")
                child["reasoning_effort"] = item.get("reasoningEffort")
                if child["status"] not in self._TERMINAL:
                    self._activate(thread_id, child)

        updates: list[dict[str, Any]] = []
        for thread_id, state in (item.get("agentsStates") or {}).items():
            if not isinstance(state, dict):
                continue
            child = self._child(
                str(thread_id),
                parent_thread_id=parent_thread_id,
                parent_turn_id=parent_turn_id,
            )
            status = self._STATUS.get(str(state.get("status") or ""), "unknown")
            message = str(state.get("message") or "")
            child["status"] = status
            if status in self._TERMINAL:
                self._active.discard(str(thread_id))
            if status == "completed":
                child["result"] = message
            elif status in {"interrupted", "failed", "not_found"}:
                child["error"] = message
            updates.append({"event": "result", **child})
        return updates

    def observe_usage(self, record: dict[str, Any]) -> bool:
        params = record.get("params") or {}
        thread_id = str(params.get("threadId") or "")
        if not thread_id or thread_id not in self._children:
            return False
        token_usage = params.get("tokenUsage") or {}
        raw_usage = token_usage.get("last") or token_usage.get("total") or {}
        if raw_usage:
            self._children[thread_id]["usage"] = _usage_from_wire(raw_usage).model_dump()
        return True

    def finish_parent(self) -> None:
        if self._active:
            self._violate(
                "parent turn ended before child threads settled: "
                + ", ".join(sorted(self._active))
            )

    @property
    def active_thread_ids(self) -> list[str]:
        return sorted(self._active)

    @property
    def records(self) -> list[SubagentRun]:
        return [
            SubagentRun.model_validate(self._children[key])
            for key in sorted(self._children)
        ]

    @property
    def usage_attribution(self) -> str:
        records = self.records
        if not records:
            return "not_applicable"
        if all(record.usage is not None for record in records):
            return "separate"
        return "unknown"


class CodexEventCollector:
    """Collect final text, terminal status and latest per-turn usage."""

    def __init__(
        self,
        raw_output_path: str | Path,
        usage_baseline: Optional[UsageBreakdown] = None,
        subagents: Optional[SubagentConfig] = None,
    ) -> None:
        self.raw_output_path = Path(raw_output_path)
        self.final_response = ""
        self.status = "outcome_unknown"
        self.error: Optional[RuntimeErrorInfo] = None
        self.usage = UsageBreakdown()
        self.cumulative_usage: Optional[UsageBreakdown] = None
        self.usage_baseline = usage_baseline
        self.context_tokens: Optional[int] = None
        self.context_window: Optional[int] = None
        self.event_count = 0
        self.unknown_event_count = 0
        self.parent_thread_id = ""
        self.parent_turn_id = ""
        self.subagent_tracker = SubagentTracker(subagents)

    def bind_parent(self, thread_id: str, turn_id: str) -> None:
        self.parent_thread_id = thread_id
        self.parent_turn_id = turn_id

    def observe(self, notification: Any) -> dict[str, Any]:
        method = str(getattr(notification, "method", "unknown"))
        payload_object = getattr(notification, "payload", {})
        params = jsonable(payload_object)
        if not isinstance(params, dict):
            params = {"value": params}
        record = {
            "received_at_ns": time.time_ns(),
            "source": "openai-codex-sdk-notification",
            "method": method,
            "params": params,
        }
        self.raw_output_path.parent.mkdir(parents=True, exist_ok=True)
        with self.raw_output_path.open("a") as raw:
            raw.write(json.dumps(record, sort_keys=True) + "\n")
            raw.flush()
        self.event_count += 1
        self.subagent_tracker.observe(record)

        if (
            type(payload_object).__name__ == "UnknownNotification"
            or method not in KNOWN_NOTIFICATION_METHODS
        ):
            self.unknown_event_count += 1
        if method == "item/completed":
            item = params.get("item") or {}
            if (
                item.get("type") == "agentMessage"
                and item.get("text")
                and item.get("phase") == "final_answer"
            ):
                self.final_response = str(item["text"])
        elif method == "thread/tokenUsage/updated":
            event_thread_id = str(params.get("threadId") or "")
            if (
                event_thread_id
                and event_thread_id != self.parent_thread_id
                and self.subagent_tracker.observe_usage(record)
            ):
                return record
            token_usage = params.get("tokenUsage") or {}
            last = token_usage.get("last") or {}
            cumulative = token_usage.get("total") or {}
            if cumulative:
                self.cumulative_usage = _usage_from_wire(cumulative)
            if last:
                self.usage = _usage_from_wire(last)
            elif self.cumulative_usage is not None and self.usage_baseline is not None:
                self.usage = _usage_delta(self.cumulative_usage, self.usage_baseline)
            context_window = token_usage.get("modelContextWindow")
            self.context_window = int(context_window) if context_window else None
        elif method == "turn/completed":
            event_thread_id = str(params.get("threadId") or "")
            if event_thread_id and event_thread_id != self.parent_thread_id:
                return record
            turn = params.get("turn") or {}
            self.status = str(turn.get("status") or "outcome_unknown")
            error = turn.get("error") or {}
            if error:
                self.error = RuntimeErrorInfo(
                    kind=classify_error(str(error.get("message") or error)),
                    message=str(error.get("message") or error),
                )
            self.subagent_tracker.finish_parent()
            if self.subagent_tracker.violations:
                self.status = "failed"
                self.error = RuntimeErrorInfo(
                    kind="subagent_policy",
                    message="; ".join(self.subagent_tracker.violations),
                )
        return record


class ToolCallTracker:
    """Coalesce started/completed tool notifications and suppress duplicates."""

    _TOOL_TYPES = {
        "commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall",
    }

    def __init__(self, invocation_id: str = "") -> None:
        self.invocation_id = invocation_id
        self._started: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._emitted: set[tuple[str, str, str]] = set()

    @staticmethod
    def _key(params: dict[str, Any], item: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(params.get("threadId") or ""),
            str(params.get("turnId") or ""),
            str(item.get("id") or ""),
        )

    def observe(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        method = record.get("method")
        params = record.get("params") or {}
        item = params.get("item") or {}
        if item.get("type") not in self._TOOL_TYPES:
            if method == "turn/completed":
                turn = params.get("turn") or {}
                status = str(turn.get("status") or "outcome_unknown")
                if status != "completed":
                    return self._flush_interrupted(
                        str(params.get("threadId") or ""),
                        str(turn.get("id") or params.get("turnId") or ""),
                        status,
                    )
            return []

        key = self._key(params, item)
        if method == "item/started":
            self._started.setdefault(key, record)
            return []
        if method != "item/completed" or key in self._emitted:
            return []
        self._emitted.add(key)
        normalized = completed_tool_record(record)
        if normalized is None:
            return []
        started = self._started.pop(key, None)
        normalized["started_at"] = _record_time(started) if started else None
        normalized["ended_at"] = _record_time(record)
        normalized["invocation_id"] = self.invocation_id
        return [normalized]

    def _flush_interrupted(
        self, thread_id: str, turn_id: str, status: str,
    ) -> list[dict[str, Any]]:
        pending: list[dict[str, Any]] = []
        for key, started in list(self._started.items()):
            if thread_id and key[0] != thread_id:
                continue
            if turn_id and key[1] != turn_id:
                continue
            if key in self._emitted:
                continue
            params = started.get("params") or {}
            item = params.get("item") or {}
            synthetic = completed_tool_record({
                "method": "item/completed",
                "params": {
                    **params,
                    "item": {**item, "status": status, "aggregatedOutput": ""},
                },
            })
            if synthetic is not None:
                synthetic.update({
                    "ok": False,
                    "status": status,
                    "started_at": _record_time(started),
                    "ended_at": None,
                    "invocation_id": self.invocation_id,
                })
                pending.append(synthetic)
            self._emitted.add(key)
            self._started.pop(key, None)
        return pending


def _record_time(record: Optional[dict[str, Any]]) -> Optional[str]:
    if not record:
        return None
    received = record.get("received_at_ns")
    if not isinstance(received, int):
        return None
    return datetime.fromtimestamp(received / 1_000_000_000, timezone.utc).isoformat()


def classify_error(message: str) -> str:
    text = message.casefold()
    if any(word in text for word in ("auth", "login", "credential", "api key")):
        return "authentication"
    if "model" in text and any(word in text for word in ("not found", "unavailable", "unsupported")):
        return "model_unavailable"
    if "reasoning effort" in text and any(word in text for word in ("not support", "unsupported")):
        return "unsupported_effort"
    if "approval" in text or "permission" in text:
        return "approval_required"
    if "thread" in text and any(word in text for word in ("not found", "missing", "unknown")):
        return "thread_unavailable"
    return "runtime"


def completed_tool_record(record: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Normalize a completed Codex tool item for the existing trace UI."""
    if record.get("method") != "item/completed":
        return None
    params = record.get("params") or {}
    item = params.get("item") or {}
    item_type = item.get("type")
    if item_type not in {
        "commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall",
    }:
        return None

    if item_type == "commandExecution":
        detail = " ".join(str(item.get("command") or "").split())
        tool = "command"
        args = {"command": item.get("command"), "cwd": item.get("cwd")}
        result = item.get("aggregatedOutput") or ""
        ok = item.get("status") == "completed" and item.get("exitCode") in (0, None)
    elif item_type == "fileChange":
        changes = item.get("changes") or []
        detail = ", ".join(
            str(change.get("path") or "") for change in changes if isinstance(change, dict)
        )
        tool = "file change"
        args = {"changes": changes}
        result = item.get("status") or ""
        ok = item.get("status") == "completed"
    else:
        detail = str(item.get("tool") or item.get("name") or item.get("server") or "")
        tool = "MCP" if item_type == "mcpToolCall" else "tool"
        args = item.get("arguments") or item.get("args") or {}
        result = item.get("result") or item.get("output") or ""
        ok = item.get("status") == "completed"

    label = f"{tool}: {detail[:80]}" if detail else tool
    result_text = result if isinstance(result, str) else json.dumps(result, sort_keys=True)
    return {
        "label": label,
        "tool": tool,
        "item_type": item_type,
        "item_id": str(item.get("id") or ""),
        "thread_id": str(params.get("threadId") or ""),
        "turn_id": str(params.get("turnId") or ""),
        "status": str(item.get("status") or "unknown"),
        "args": args,
        "ok": ok,
        "result_snippet": result_text[:20_000],
        "duration_ms": item.get("durationMs"),
        "ended_at": None,
    }
