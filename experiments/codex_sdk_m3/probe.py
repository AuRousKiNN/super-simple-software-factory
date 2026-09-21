#!/usr/bin/env -S uv run
# /// script
# dependencies = ["openai-codex==0.155.1", "pydantic"]
# ///
"""Real M3 smoke against a synthetic workspace; no repository source is sent."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_ADWS = ROOT / ".claude/skills/sssf/templates/adws"
sys.path.insert(0, str(TEMPLATE_ADWS))

from adw_modules.agent_codex import CodexRuntime, RuntimeHooks  # noqa: E402
from adw_modules.codex_schema import strict_output_schema  # noqa: E402
from adw_modules.data_types import (  # noqa: E402
    AgentRunRequest,
    CodexRuntimeConfig,
    GenericOutput,
    SubagentConfig,
)


def _snapshot(workspace: Path) -> dict[str, str]:
    result = {}
    for path in sorted(workspace.rglob("*")):
        if not path.is_file() or ".codex" in path.parts:
            continue
        relative = path.relative_to(workspace).as_posix()
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _install_role(workspace: Path) -> None:
    source = ROOT / ".claude/skills/sssf/templates/codex_agents/sssf_recon.toml"
    destination = workspace / ".codex/agents/sssf_recon.toml"
    destination.parent.mkdir(parents=True)
    destination.write_text(source.read_text().replace(
        "{{sssf_skill_path_toml}}",
        json.dumps(str(ROOT / ".claude/skills/sssf/SKILL.md")),
    ))
    shutil.copymode(source, destination)


def main(output_dir: Path) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sssf-m3-smoke-") as temporary:
        workspace = Path(temporary)
        (workspace / "alpha.txt").write_text("alpha-marker\n")
        (workspace / "beta.txt").write_text("beta-marker\n")
        _install_role(workspace)
        before = _snapshot(workspace)
        policy = SubagentConfig(
            enabled=True,
            max_concurrent=2,
            role="sssf_recon",
            config_file=".codex/agents/sssf_recon.toml",
        )
        runtime = CodexRuntime(CodexRuntimeConfig(), workspace)
        events: list[dict] = []
        try:
            result = runtime.run_turn(
                AgentRunRequest(
                    role="scout",
                    cwd=str(workspace),
                    model="gpt-5.6-terra",
                    effort="low",
                    developer_instructions=(
                        "You are a read-only parent smoke probe. Spawn exactly two sssf_recon "
                        "subagents concurrently. Give one only alpha.txt and the other only "
                        "beta.txt. Tell each to return its marker, not edit files, not load the "
                        "SSSF orchestrator skill, and not create subagents. Wait for both. Then "
                        "return only GenericOutput JSON. For each child, copy the exact first "
                        "whitespace-delimited token of its response. Set notes_for_next_agent "
                        "exactly to alpha_prefix=<token>; beta_prefix=<token>; "
                        "alpha_marker=<file marker>; beta_marker=<file marker>."
                    ),
                    prompt="Run the bounded two-child M3 smoke now.",
                    output_schema=strict_output_schema(GenericOutput),
                    raw_output_path=str(output_dir / "raw_output.jsonl"),
                    subagents=policy,
                ),
                RuntimeHooks(on_event=events.append),
            )
        finally:
            runtime.close()
        after = _snapshot(workspace)
        if result.status != "completed":
            raise AssertionError(f"parent ended as {result.status}: {result.error}")
        envelope = GenericOutput.model_validate_json(result.text)
        child_ids = {child.thread_id for child in result.subagents}
        if len(child_ids) != 2:
            raise AssertionError(f"expected two child threads, got {sorted(child_ids)}")
        if any(child.role != policy.role for child in result.subagents):
            raise AssertionError(f"unexpected child role: {result.subagents}")
        if any(child.status != "completed" for child in result.subagents):
            raise AssertionError(f"child did not settle: {result.subagents}")
        if "alpha_prefix=SSSF_RECON_OK" not in envelope.notes_for_next_agent:
            raise AssertionError("alpha child did not load sssf_recon developer instructions")
        if "beta_prefix=SSSF_RECON_OK" not in envelope.notes_for_next_agent:
            raise AssertionError("custom sssf_recon developer instructions were not observed")
        if not all(
            marker in envelope.notes_for_next_agent
            for marker in ("alpha-marker", "beta-marker")
        ):
            raise AssertionError("parent did not synthesize both child markers")
        if before != after:
            raise AssertionError("synthetic workspace changed during read-only child smoke")
        summary = {
            "status": result.status,
            "thread_id": result.thread_id,
            "turn_id": result.turn_id,
            "children": [child.model_dump(mode="json") for child in result.subagents],
            "child_usage_attribution": result.child_usage_attribution,
            "subagent_cleanup_forced": result.subagent_cleanup_forced,
            "event_methods": sorted({event.get("method", "") for event in events}),
            "workspace_unchanged": before == after,
        }
        (output_dir / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n"
        )
        print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(main(args.output_dir))
