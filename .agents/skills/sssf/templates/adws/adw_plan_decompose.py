#!/usr/bin/env -S uv run
# /// script
# dependencies = ["openai-codex==0.155.1", "pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""ADW Plan Decompose — produce a complete spec and validated ticket set.

Usage:
    uv run adws/adw_plan_decompose.py "<prompt or path/to/prompt.md>" --spec-dir specs/example
    uv run adws/adw_plan_decompose.py "<revision request>" --spec specs/example/spec.md
Phases: engineer(request) -> planner -> code(input) -> decomposer -> code(index)
"""
import argparse
import sys

from adw_modules import agents, gates, session, tickets, utils
from adw_modules.data_types import PlanningTarget
from adw_modules.data_types import AgentCall, PhaseParams, PlanOutput

REQUIRED_AGENTS = ["planner", "decomposer"]


def main(prompt: str, config: str = "adws/adw_sssf_config/sssf.config.yaml", adw_id: str | None = None, target: PlanningTarget | None = None) -> int:
    target = target or PlanningTarget()
    cfg = agents.load_config(config)
    agents.validate(cfg, REQUIRED_AGENTS)
    run = session.ensure(cfg, adw_id)
    with run.phase(PhaseParams(name="request", kind="engineer", owner=run.engineer,
                              description="Record the full requested outcome before planning")) as ph:
        ph.log(input=prompt)
    with run.phase(PhaseParams(name="plan", kind="agent", owner="planner", retries=2,
                              description="Define complete public semantics and global acceptance")) as ph:
        plan = ph.call(AgentCall(output_type=PlanOutput, prompt=prompt, planning_target=target,
                                 gates=[gates.artifacts_exist, gates.files_non_empty]))
    tickets.decompose(run, plan.spec_path)
    return run.finish()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt")
    parser.add_argument("--config", default="adws/adw_sssf_config/sssf.config.yaml")
    parser.add_argument("--adw-id", default=None)
    goal = parser.add_mutually_exclusive_group(required=True)
    goal.add_argument("--spec-dir", help="new specs/<spec_key> directory chosen by the launching agent")
    goal.add_argument("--spec", help="existing specs/<spec_key>/spec.md to revise")
    args = parser.parse_args()
    sys.exit(main(utils.resolve_prompt(args.prompt), args.config, args.adw_id, PlanningTarget(spec_dir=args.spec_dir, spec=args.spec)))
