#!/usr/bin/env -S uv run
# /// script
# dependencies = ["openai-codex==0.155.1", "pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""ADW Build — one-shot implementation workflow.

Usage:
    uv run adws/adw_build.py "<prompt or path/to/prompt.md>" [--config adws/adw_sssf_config/sssf.config.yaml] [--adw-id a1b2c3d4]

    uv run adws/adw_build.py --spec specs/example.md
    uv run adws/adw_build.py --ticket specs/example.tickets/r1/tickets/TICKET-QUERY.md --ticket-set specs/example.tickets/r1/ticket-set.md

Phases: engineer(request) -> code(input) -> builder

This entry reports implementation only; acceptance records require a checking ADW.
"""

import argparse
import sys

from adw_modules import agents, session, tickets, utils
from adw_modules.data_types import AgentCall, BuildInput, BuildOutput, PhaseParams

REQUIRED_AGENTS = ["builder"]


def main(prompt: str | BuildInput, config: str = "adws/adw_sssf_config/sssf.config.yaml", adw_id: str | None = None) -> int:
    target = BuildInput(prompt=prompt) if isinstance(prompt, str) else prompt
    cfg = agents.load_config(config)
    agents.validate(cfg, REQUIRED_AGENTS)
    run = session.ensure(cfg, adw_id)

    with run.phase(PhaseParams(name="request", kind="engineer", owner=run.engineer,
                               description="Capture the incoming ask")) as ph:
        ph.log(input=target.model_dump())
    with run.phase(PhaseParams(name="build_input", kind="code", owner="tickets",
                              description="Validate and bind the explicit implementation target")) as ph:
        item = (tickets.spec_work_item(run.repo_root, target.spec) if target.spec else
                tickets.ticket_work_item(run, target.ticket_set, target.ticket, target.dependency_evidence)
                if target.ticket else None)
        ph.log(work_item=item.model_dump() if item else None)

    with run.phase(PhaseParams(name="build", kind="agent", owner="builder", retries=1,
                               description="Implement the request")) as ph:
        ph.call(AgentCall(output_type=BuildOutput, prompt=target.prompt or "Implement the bound work item.", work_item=item))

    return run.finish()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", nargs="?", default="", help="inline text or a path to a prompt file")
    parser.add_argument("--spec")
    parser.add_argument("--ticket")
    parser.add_argument("--ticket-set")
    parser.add_argument("--dependency-evidence")
    parser.add_argument("--config", default="adws/adw_sssf_config/sssf.config.yaml")
    parser.add_argument("--adw-id", default=None, help="join or pin an existing session")
    args = parser.parse_args()
    try:
        target = BuildInput(prompt=utils.resolve_prompt(args.prompt) if args.prompt else "",
                            spec=args.spec, ticket=args.ticket, ticket_set=args.ticket_set,
                            dependency_evidence=args.dependency_evidence)
    except ValueError as error:
        parser.error(str(error))
    sys.exit(main(target, args.config, args.adw_id))
