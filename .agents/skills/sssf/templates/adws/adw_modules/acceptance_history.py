"""Host-owned chronological receipts; historical acceptance files stay immutable."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .data_types import ArtifactRef, AcceptanceRecord


class AcceptanceOrder(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: int = 1
    ticket_id: str
    definition_sha256: str
    accepted_at: str
    sequence: int = Field(gt=0)
    receipt: ArtifactRef


def sessions(run):
    path = Path(run.cfg.defaults.data_dir) / "sessions"
    return path if path.is_absolute() else run.repo_root / path


def orders(run):
    from . import tickets
    result = []
    for path in sorted(sessions(run).glob("*/ticket-acceptance-order.json")):
        try:
            value = AcceptanceOrder.model_validate_json(path.read_text())
            stamp = datetime.fromisoformat(value.accepted_at)
            if value.schema_version != 1 or stamp.tzinfo is None:
                raise ValueError("unsupported version or missing timezone")
            expected = path.parent / "ticket-acceptance.json"
            if tickets.repo_path(run.repo_root, value.receipt.path, ".json") != expected:
                raise ValueError("receipt is outside its host session")
        except (ValueError, OSError) as error:
            raise tickets.TicketError(f"invalid acceptance ordering metadata: {path}: {error}") from error
        result.append(value)
    if len({r.sequence for r in result}) != len(result):
        raise tickets.TicketError("duplicate host acceptance sequence")
    return result


def publish(run, record, receipt, *, accepted_at=None):
    """Caller holds the workspace lock. Timestamp and tie-breaker are host issued."""
    from . import tickets
    existing = orders(run)
    path = run.session_dir / "ticket-acceptance-order.json"
    if path.exists():
        saved = AcceptanceOrder.model_validate_json(path.read_text())
        if saved.receipt != receipt:
            raise tickets.TicketError("immutable acceptance ordering differs from receipt")
        return
    value = AcceptanceOrder(ticket_id=record.ticket_id,
        definition_sha256=record.definition_sha256,
        accepted_at=accepted_at or datetime.now(timezone.utc).isoformat(),
        sequence=max((r.sequence for r in existing), default=0) + 1, receipt=receipt)
    tickets.atomic_json(path, value.model_dump())


def latest(run, payload, blockers):
    from . import tickets
    if not blockers:
        return []
    history = orders(run)
    indexed = {r.receipt.path for r in history}
    # Do not silently omit old successful records whose chronology is unknown.
    for path in sorted(sessions(run).glob("*/ticket-acceptance.json")):
        relative = path.relative_to(run.repo_root).as_posix()
        if relative in indexed:
            continue
        try:
            raw = json.loads(path.read_text())
            if raw.get("ticket_id") not in blockers or raw.get("definition_sha256") != payload["definition_sha256"]:
                continue
            AcceptanceRecord.model_validate(raw)
        except (ValueError, OSError, AttributeError) as error:
            raise tickets.TicketError(f"unreadable unindexed acceptance: {relative}") from error
        raise tickets.TicketError(f"acceptance ordering migration required: {relative}; run migrate_ticket_history.py")
    refs = []
    for blocker in blockers:
        candidates = [r for r in history if r.ticket_id == blocker
                      and r.definition_sha256 == payload["definition_sha256"]]
        if not candidates:
            raise tickets.TicketError(f"dependency evidence required: no successful current-definition acceptance for {blocker}")
        selected = max(candidates, key=lambda r: (datetime.fromisoformat(r.accepted_at), r.sequence))
        tickets.verify_ref(run.repo_root, selected.receipt)
        record = AcceptanceRecord.model_validate_json((run.repo_root / selected.receipt.path).read_text())
        if record.ticket_id != blocker or record.definition_sha256 != selected.definition_sha256:
            raise tickets.TicketError("acceptance ordering identity differs from receipt")
        refs.append(selected.receipt)
    return refs


def migrate(run, database):
    """Explicit current-schema upgrade: preserve receipts, derive order from host trace."""
    import sqlite3
    from types import SimpleNamespace
    from . import permissions, tickets
    lock = permissions.acquire_workspace_lock(run.repo_root)
    try:
        with sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True) as connection:
            schema = connection.execute("SELECT schema_version FROM schema_meta WHERE singleton=1").fetchone()
            if schema != (2,):
                raise tickets.TicketError("acceptance chronology migration requires trace schema v2")
            pending = []
            for path in sorted(sessions(run).glob("*/ticket-acceptance.json")):
                if path.with_name("ticket-acceptance-order.json").exists():
                    continue
                record = AcceptanceRecord.model_validate_json(path.read_text())
                row = connection.execute("SELECT rowid, ended_at, status FROM sessions WHERE adw_id=?",
                                         (record.adw_id,)).fetchone()
                if not row or row[2] != "success" or not row[1]:
                    raise tickets.TicketError(f"missing successful host completion time: {path}")
                stamp = datetime.fromisoformat(row[1])
                if stamp.tzinfo is None:
                    raise tickets.TicketError(f"ambiguous host completion timezone: {path}")
                if path.parent.name != record.adw_id:
                    raise tickets.TicketError(f"acceptance session identity mismatch: {path}")
                for ref in record.checks + record.reviews + record.manual_validation:
                    tickets.verify_ref(run.repo_root, ref)
                ref = tickets.artifact(run.repo_root, path.relative_to(run.repo_root).as_posix(), ".json")
                pending.append((stamp, row[0], record, ref, path.parent))
            # Validate every source before writing any new ordering metadata.
            orders(run)
            for stamp, _, record, ref, directory in sorted(pending, key=lambda value: value[:2]):
                target = SimpleNamespace(repo_root=run.repo_root, cfg=run.cfg, session_dir=directory)
                publish(target, record, ref, accepted_at=stamp.astimezone(timezone.utc).isoformat())
            return len(pending)
    finally:
        lock.release()
