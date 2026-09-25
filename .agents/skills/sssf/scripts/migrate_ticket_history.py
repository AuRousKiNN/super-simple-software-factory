#!/usr/bin/env python3
"""Explicitly add host chronology to existing schema-v2 ticket acceptance history."""
import argparse
import sys
from pathlib import Path
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--config", default="adws/adw_sssf_config/sssf.config.yaml")
    args = parser.parse_args()
    root = args.root.resolve()
    sys.path.insert(0, str(root / "adws"))
    from adw_modules import acceptance_history, agents
    cfg = agents.load_config(str(root / args.config))
    database = Path(cfg.observability.db)
    if not database.is_absolute():
        database = root / database
    run = SimpleNamespace(repo_root=root, cfg=cfg)
    count = acceptance_history.migrate(run, database)
    print(f"Added host ordering metadata for {count} acceptance records; original receipts unchanged.")


if __name__ == "__main__":
    main()
