#!/usr/bin/env -S uv run
# /// script
# dependencies = ["openai-codex==0.155.1", "pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""Deterministic artifact recovery. Never dispatch an agent or reissue finish.

uv run adws/spec_artifacts_cli.py index
uv run adws/spec_artifacts_cli.py recover --adw-id ID --execution-id ID
uv run adws/spec_artifacts_cli.py sync --adw-id ID
"""
import argparse
from pathlib import Path
from types import SimpleNamespace

from adw_modules import agents, git_helper, permissions, spec_artifacts
from adw_modules.tracer import Tracer


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["index", "recover", "sync"])
    parser.add_argument("--config", default="adws/adw_sssf_config/sssf.config.yaml")
    parser.add_argument("--adw-id")
    parser.add_argument("--execution-id")
    args = parser.parse_args()
    if args.action != "index" and not args.adw_id:
        parser.error("recovery requires --adw-id")
    if args.action == "recover" and not args.execution_id:
        parser.error("recover requires --execution-id")
    if args.adw_id and (Path(args.adw_id).name != args.adw_id or args.adw_id in {".", ".."}):
        parser.error("invalid session identity")
    cfg = agents.load_config(args.config)
    root = git_helper.repo_root()
    session_dir = Path(cfg.defaults.data_dir) / "sessions" / (args.adw_id or "index-refresh")
    if args.action != "index" and not session_dir.is_dir():
        parser.error("session does not exist")
    run = SimpleNamespace(repo_root=root, cfg=cfg, adw_id=args.adw_id or "index-refresh",
        session_dir=session_dir, context_handoff_dir=session_dir / "context_handoff",
        tracer=Tracer(cfg.observability.db, str(session_dir / "events.jsonl")), workspace_lock=None)
    if args.action == "sync":
        spec_artifacts.sync_finished(run)
        run.workspace_lock = permissions.acquire_workspace_lock(root)
        try:
            spec_artifacts.sync_ticket(run)
        finally:
            run.workspace_lock.release()
    else:
        run.workspace_lock = permissions.acquire_workspace_lock(root)
        try:
            if args.action == "index":
                spec_artifacts.rebuild_index(run)
            else:
                output = spec_artifacts.recover(run, args.execution_id)
                print(output.model_dump_json(indent=2))
        finally:
            run.workspace_lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
