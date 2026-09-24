#!/usr/bin/env -S uv run
# /// script
# dependencies = ["openai-codex==0.155.1", "pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""ADW Plan — one-shot planning workflow.

Usage:
    uv run adws/adw_plan.py "<prompt or path/to/prompt.md>" --spec-dir specs/example [--config adws/adw_sssf_config/sssf.config.yaml] [--adw-id a1b2c3d4]
    uv run adws/adw_plan.py "<revision request>" --spec specs/example/spec.md

Phases: engineer(request) -> planner
"""

import argparse
import sys

from adw_modules import agents, gates, session, utils
from adw_modules.data_types import PlanningTarget
from adw_modules.data_types import AgentCall, PhaseParams, PlanOutput

REQUIRED_AGENTS = ["planner"]


def main(prompt: str, config: str = "adws/adw_sssf_config/sssf.config.yaml", adw_id: str | None = None, target: PlanningTarget | None = None) -> int:
    target = target or PlanningTarget()
    cfg = agents.load_config(config)
    agents.validate(cfg, REQUIRED_AGENTS)
    run = session.ensure(cfg, adw_id)

    with run.phase(PhaseParams(name="request", kind="engineer", owner=run.engineer,
                               description="Capture the incoming ask")) as ph:
        ph.log(input=prompt)

    with run.phase(PhaseParams(name="plan", kind="agent", owner="planner",
                               description="Turn the request into an implementable plan")) as ph:
        ph.call(AgentCall(output_type=PlanOutput, prompt=prompt, planning_target=target,
                          gates=[gates.artifacts_exist, gates.files_non_empty]))

    return run.finish()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", help="inline text or a path to a prompt file")
    parser.add_argument("--config", default="adws/adw_sssf_config/sssf.config.yaml")
    parser.add_argument("--adw-id", default=None, help="join or pin an existing session")
    goal = parser.add_mutually_exclusive_group(required=True)
    goal.add_argument("--spec-dir", help="new specs/<spec_key> directory chosen by the launching agent")
    goal.add_argument("--spec", help="existing specs/<spec_key>/spec.md to revise")
    args = parser.parse_args()
    sys.exit(main(utils.resolve_prompt(args.prompt), args.config, args.adw_id, PlanningTarget(spec_dir=args.spec_dir, spec=args.spec)))
