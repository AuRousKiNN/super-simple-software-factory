#!/usr/bin/env -S uv run
# /// script
# dependencies = ["openai-codex==0.155.1", "pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""ADW Simple SDLC — plan and enter the shared complete delivery chain.

Usage:
    uv run adws/adw-simple-sdlc.py "Plan and deliver export" --spec-dir specs/export
    uv run adws/adw-simple-sdlc.py "Revise and deliver export" --spec specs/export/spec.md

Phases: code(preflight) -> planner -> git(commit_plan) -> shared delivery
        -> [scout(evidence freshness)] -> builder -> code(checks) -> reviewer -> bounded repair -> documenter -> commit -> finish
Existing tickets enter the identical delivery chain through adw-build --ticket.
"""
import argparse
import sys
from pathlib import Path

from adw_modules import agents, delivery, gates, git_helper, session, tickets, utils
from adw_modules.data_types import AgentCall, PhaseParams, PlanningTarget, PlanOutput

REQUIRED_AGENTS = ["planner", *delivery.REQUIRED_AGENTS]


def main(prompt: str, config: str = "adws/adw_sssf_config/sssf.config.yaml", adw_id: str | None = None, target: PlanningTarget | None = None) -> int:
    target = target or PlanningTarget()
    cfg = agents.load_config(config)
    agents.validate(cfg, REQUIRED_AGENTS)
    run = session.ensure(cfg, adw_id)
    with run.phase(PhaseParams(name="request", kind="engineer", owner=run.engineer,
                               description="Record the request and explicit planning target")) as ph:
        ph.log(input=prompt, target=target.model_dump())
    with run.phase(PhaseParams(name="preflight", kind="code", owner="delivery",
                               description="Reject missing quality configuration before spending planning or builder turns")) as ph:
        ph.log(mandatory_checks=delivery.preflight(run))
    with run.phase(PhaseParams(name="plan", kind="agent", owner="planner",
                               description="Turn the request into an explicit implementation specification")) as ph:
        plan = ph.call(AgentCall(output_type=PlanOutput, prompt=prompt, planning_target=target,
                                 gates=[gates.artifacts_exist, gates.files_non_empty]))
    with run.phase(PhaseParams(name="commit_plan", kind="code", owner="git",
                               description="Record the specification before implementation begins")) as ph:
        build_base = git_helper.commit_paths(plan.commit_message or "记录规格规划",
            [plan.spec_path, str(Path(plan.spec_path).parent / "README.md"), "specs/README.md"])
        item = tickets.spec_work_item(run.repo_root, plan.spec_path)
        ph.log(sha=build_base, work_item=item.model_dump())
    return delivery.execute(run, delivery.DeliveryRequest(prompt, item, build_base, plan))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt")
    parser.add_argument("--config", default="adws/adw_sssf_config/sssf.config.yaml")
    parser.add_argument("--adw-id")
    goal = parser.add_mutually_exclusive_group(required=True)
    goal.add_argument("--spec-dir")
    goal.add_argument("--spec")
    args = parser.parse_args()
    sys.exit(main(utils.resolve_prompt(args.prompt), args.config, args.adw_id,
                  PlanningTarget(spec_dir=args.spec_dir, spec=args.spec)))
