"""Session lifecycle: pin-or-create an adw_id, build the Run object.

`ensure(cfg, adw_id)` joins the session if it exists or creates it under
exactly that id (pinned ids for repeatable runs); omitted, a fresh id is
minted and printed so the next ADW can pick it up.
"""

from __future__ import annotations

import atexit
import os
import signal
import sqlite3
import sys
from pathlib import Path

from .agent_codex import process_start_marker
from .data_types import SSSFConfig
from .runner import Run
from .tracer import Tracer
from .utils import engineer_name, new_id


def _finalize_when_killed(run: Run) -> None:
    """A killed run still closes its own trace.

    Python's default SIGTERM handling exits without unwinding, so `just kill`
    (or any `kill <pid>`) would leave the session reading `running` forever and
    its process rows open — the trace would claim work is in flight that is
    already dead. Turning the signal into SystemExit both finalizes here and
    lets the phase context manager record the phase as failed on the way out.
    """
    # Keep the workspace lock until agent permission restoration has unwound.
    # A signal outside a phase still gets final process cleanup at interpreter exit.
    atexit.register(run.close)

    def handler(signum, _frame):
        run.tracer.session_finish(run.adw_id, ok=False)   # also closes process rows
        raise SystemExit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, handler)


def ensure(cfg: SSSFConfig, adw_id: str | None = None) -> Run:
    adw_id = adw_id or new_id(8)
    tracer = Tracer(cfg.observability.db,
                    f"{cfg.defaults.data_dir}/sessions/{adw_id}/events.jsonl")
    run = Run(cfg=cfg, adw_id=adw_id, tracer=tracer, engineer=engineer_name())
    tracer.session_start(adw_id, run.engineer, adw_name=Path(sys.argv[0]).stem)
    # This process is the run. Record it before any phase opens, so a run that
    # hangs in its first agent call is still killable by adw_id.
    tracer.process_start(adw_id, "adw", "", os.getpid(),
                         " ".join([Path(sys.argv[0]).name, *sys.argv[1:]]),
                         start_marker=process_start_marker(os.getpid()))
    _finalize_when_killed(run)
    run.console.session_started(adw_id, run.engineer)
    return run


def new_attempt(cfg: SSSFConfig, adw_id: str | None = None) -> Run:
    """Reject a reused recovery destination before session_start can rewrite it."""
    if adw_id:
        if Path(adw_id).name != adw_id or adw_id in {".", ".."}:
            raise ValueError("recovery destination must be an adw_id, not a path")
        if (Path(cfg.defaults.data_dir) / "sessions" / adw_id).exists():
            raise ValueError("recovery requires a fresh destination adw_id")
        database = Path(cfg.observability.db)
        if database.exists():
            with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
                if connection.execute("SELECT 1 FROM sessions WHERE adw_id=?", (adw_id,)).fetchone():
                    raise ValueError("recovery destination already exists in trace history")
    return ensure(cfg, adw_id)
