#!/usr/bin/env -S uv run
# /// script
# dependencies = ["openai-codex==0.155.1", "pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""ADW Build Review — implement, then confirm it is what was asked for.

Usage:
    uv run adws/adw_build_review.py "<prompt or path/to/prompt.md>" [--config adws/adw_sssf_config/sssf.config.yaml] [--adw-id a1b2c3d4]

Phases: engineer(request) -> builder -> [code(changes) -> reviewer -> builder(revise)] bounded

Review is not testing. Tests answer "does it run"; the reviewer answers "is this
the thing that was asked for" — it reads the spec (`plan.md` from a prior plan
phase if the session has one, else the prompt verbatim), reads the code that was
written, and rules on each requirement.

Like the tester, the reviewer's phase succeeds when it RUNS and REPORTS. A
rejection does not fail the phase; it fails the run, checked at the end, after
the bounded revise loop has had its chances.
"""

import argparse
import sys

from adw_modules import agents, changes, gates, git_helper, session, utils
from adw_modules.data_types import (AgentCall, BuildOutput, ChangeCapture,
                                    PhaseParams, ReviewOutput)

REQUIRED_AGENTS = ["builder", "reviewer"]
MAX_REVISION_LOOPS = 3


def main(prompt: str, config: str = "adws/adw_sssf_config/sssf.config.yaml", adw_id: str | None = None) -> int:
    cfg = agents.load_config(config)
    agents.validate(cfg, REQUIRED_AGENTS)
    run = session.ensure(cfg, adw_id)
    build_base = git_helper.rev("HEAD")

    with run.phase(PhaseParams(name="request", kind="engineer", owner=run.engineer,
                               description="Capture the incoming ask")) as ph:
        ph.log(input=prompt, build_base=git_helper.short_sha(build_base))

    with run.phase(PhaseParams(name="build", kind="agent", owner="builder",
                               description="Implement the request")) as ph:
        ph.call(AgentCall(output_type=BuildOutput, prompt=prompt))

    review = None
    for i in range(1, MAX_REVISION_LOOPS + 1):
        with run.phase(PhaseParams(name=f"changes_{i}", kind="code", owner="git",
                                   description="Capture the cumulative code diff so review uses Git facts")) as ph:
            changeset = changes.capture(run, ChangeCapture(base=build_base))
            ph.log(files=len(changeset.files) + len(changeset.untracked),
                   lines=f"+{changeset.insertions} -{changeset.deletions}",
                   diff=changeset.diff_path)
            if changeset.empty:
                raise RuntimeError("builder produced no reviewable changes")

        with run.phase(PhaseParams(name=f"review_{i}", kind="agent", owner="reviewer",
                                   description="Rule on every requirement in the spec, against the code on disk")) as ph:
            review = ph.call(AgentCall(output_type=ReviewOutput, prompt=prompt,
                                       previous=changes.as_envelope(changeset, "review"),
                                       gates=[gates.artifacts_exist,
                                              gates.verdict_consistent]))

        if review.approved:
            break
        if i == MAX_REVISION_LOOPS:
            break

        with run.phase(PhaseParams(name=f"revise_{i}", kind="agent", owner="builder", retries=1,
                                   description="Close every blocking finding the reviewer named")) as ph:
            ph.call(AgentCall(output_type=BuildOutput, prompt=prompt, previous=review))

    return run.finish(accepted=review is not None and review.approved,
                      reason=f"the reviewer never approved after {MAX_REVISION_LOOPS} revision(s)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", help="inline text or a path to a prompt file")
    parser.add_argument("--config", default="adws/adw_sssf_config/sssf.config.yaml")
    parser.add_argument("--adw-id", default=None, help="join or pin an existing session")
    args = parser.parse_args()
    sys.exit(main(utils.resolve_prompt(args.prompt), args.config, args.adw_id))
