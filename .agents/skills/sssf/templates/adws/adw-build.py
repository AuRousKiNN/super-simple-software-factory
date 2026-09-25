#!/usr/bin/env -S uv run
# /// script
# dependencies = ["openai-codex==0.155.1", "pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""ADW Build — deliver a request, existing specification or ticket.

Usage:
    uv run adws/adw-build.py "Implement the requested change"
    uv run adws/adw-build.py --spec specs/example/spec.md
    uv run adws/adw-build.py --ticket specs/example/spec.tickets/tickets/TICKET-1.md

Phases: code(preflight/input) -> [scout(evidence freshness)] -> builder -> code(checks) -> reviewer -> code(route)
        -> [builder(repair) -> code(checks) -> reviewer] bounded
        -> documenter -> git(commit) -> finish -> ticket acceptance when applicable
"""
import argparse
import sys

from adw_modules import agents, delivery, session, utils
from adw_modules.data_types import BuildInput, PhaseParams

REQUIRED_AGENTS = delivery.REQUIRED_AGENTS


def main(prompt: str | BuildInput, config: str = "adws/adw_sssf_config/sssf.config.yaml", adw_id: str | None = None) -> int:
    target = BuildInput(prompt=prompt) if isinstance(prompt, str) else prompt
    cfg = agents.load_config(config)
    agents.validate(cfg, REQUIRED_AGENTS)
    run = session.ensure(cfg, adw_id)
    with run.phase(PhaseParams(name="request", kind="engineer", owner=run.engineer,
                               description="Record the selected delivery target and request")) as ph:
        ph.log(input=target.model_dump())
    return delivery.launch(run, target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", nargs="?", default="")
    parser.add_argument("--spec")
    parser.add_argument("--ticket")
    parser.add_argument("--ticket-set")
    parser.add_argument("--dependency-evidence")
    parser.add_argument("--config", default="adws/adw_sssf_config/sssf.config.yaml")
    parser.add_argument("--adw-id")
    args = parser.parse_args()
    try:
        target = BuildInput(prompt=utils.resolve_prompt(args.prompt) if args.prompt else "", spec=args.spec,
                            ticket=args.ticket, ticket_set=args.ticket_set, dependency_evidence=args.dependency_evidence)
    except ValueError as error:
        parser.error(str(error))
    sys.exit(main(target, args.config, args.adw_id))
