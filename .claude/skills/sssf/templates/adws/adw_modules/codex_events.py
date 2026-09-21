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

from .data_types import RuntimeErrorInfo, UsageBreakdown


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


class CodexEventCollector:
    """Collect final text, terminal status and latest per-turn usage."""

    def __init__(
        self,
        raw_output_path: str | Path,
        usage_baseline: Optional[UsageBreakdown] = None,
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
            turn = params.get("turn") or {}
            self.status = str(turn.get("status") or "outcome_unknown")
            error = turn.get("error") or {}
            if error:
                self.error = RuntimeErrorInfo(
                    kind=classify_error(str(error.get("message") or error)),
                    message=str(error.get("message") or error),
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
