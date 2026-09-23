#!/usr/bin/env python3
"""Opt-in real Codex smoke using only a disposable synthetic repository."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main(output: Path) -> int:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    workspace = Path(tempfile.mkdtemp(prefix="sssf-ticket-smoke-"))
    (output / "workspace.txt").write_text(str(workspace) + "\n")

    def execute(name, argv):
        with (output / f"{name}.stdout.log").open("w") as stdout, (output / f"{name}.stderr.log").open("w") as stderr:
            result = subprocess.run(argv, cwd=workspace, stdout=stdout, stderr=stderr,
                                    env={**os.environ, "ENGINEER_NAME": "Ticket smoke"}, timeout=600)
        if result.returncode:
            raise RuntimeError(f"{name} exited {result.returncode}; logs: {output}")

    execute("install", [sys.executable, str(ROOT / ".agents/skills/sssf/scripts/install.py")])
    execute("git-init", ["git", "init", "-q"])
    execute("git-name", ["git", "config", "user.name", "Ticket Smoke"])
    execute("git-email", ["git", "config", "user.email", "ticket-smoke@example.invalid"])
    (workspace / "README.md").write_text("# Synthetic ticket smoke\nNo greeting exists yet.\n")
    (workspace / "specs").mkdir()
    (workspace / "specs/greeting.md").write_text(
        "---\nrevision: 1\n---\n# Greeting delivery\n\nREQ-01: Add greeting.txt at repository root.\n"
        "CONTRACT-01: Its exact UTF-8 bytes are hello from ticket followed by one newline.\n"
        "AC-01: Reading greeting.txt yields the exact CONTRACT-01 bytes.\n"
        "Scope: only this one new file; no other implementation changes.\n"
        "Verification: the host checks the exact bytes. No required manual validation.\n"
        "This is one independent behavior with no external or ticket prerequisites.\n")
    prompt = workspace / "adws/adw_data/prompt_engineering/decomposer/system.md"
    prompt.write_text(prompt.read_text() +
        "\nSmoke obligation: delegate exactly one read-only sssf_recon child to inspect README.md "
        "and confirm whether a greeting capability is present. Wait for its completed result, "
        "include the SSSF_RECON_OK marker in your handoff notes, then produce one behavior ticket.\n")
    execute("git-add", ["git", "add", "."])
    execute("git-commit", ["git", "commit", "-qm", "初始化合成拆解验收仓库"])
    execute("decompose", [sys.executable, "adws/adw_decompose.py", "--spec", "specs/greeting.md",
                          "--adw-id", "ticket-decompose"])
    set_path = "specs/greeting.tickets/ticket-set.md"
    index = json.loads((workspace / "specs/greeting.tickets/index.json").read_text())
    if len(index["tickets"]) != 1:
        raise AssertionError("expected one independent ticket")
    selected = index["tickets"][0]
    events = [json.loads(line) for line in
              (workspace / "adws/adw_data/sessions/ticket-decompose/events.jsonl").read_text().splitlines()]
    end = next(event for event in reversed(events) if event.get("type") == "agent_end")
    children = end["payload"]["subagents"]
    if len(children) != 1 or children[0]["status"] != "completed":
        raise AssertionError(f"expected one completed recon child: {children}")
    execute("build", [sys.executable, "adws/adw_build.py", "--ticket", selected["artifact"]["path"],
                      "--ticket-set", set_path, "--adw-id", "ticket-build"])
    if (workspace / "greeting.txt").read_bytes() != b"hello from ticket\n":
        raise AssertionError("builder did not deliver the selected ticket")
    summary = {"workspace": str(workspace), "ticket_id": selected["id"],
               "definition_sha256": index["definition_sha256"], "children": children,
               "implementation_verified": True}
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    raise SystemExit(main(parser.parse_args().output_dir))
