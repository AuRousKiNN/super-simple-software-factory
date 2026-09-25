#!/usr/bin/env -S uv run
# /// script
# dependencies = ["openai-codex==0.155.1", "pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""ADW Decompose — validate and decompose one explicit current spec.

Usage: uv run adws/adw-decompose.py --spec specs/request/spec.md
Phases: code(input) -> decomposer -> code(index)
"""
import argparse
import sys

from adw_modules import agents, session, tickets

REQUIRED_AGENTS = ["decomposer"]


def main(spec: str, config: str = "adws/adw_sssf_config/sssf.config.yaml", adw_id: str | None = None) -> int:
    cfg = agents.load_config(config)
    agents.validate(cfg, REQUIRED_AGENTS)
    run = session.ensure(cfg, adw_id)
    tickets.decompose(run, spec)
    return run.finish()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--config", default="adws/adw_sssf_config/sssf.config.yaml")
    parser.add_argument("--adw-id", default=None)
    args = parser.parse_args()
    sys.exit(main(args.spec, args.config, args.adw_id))
