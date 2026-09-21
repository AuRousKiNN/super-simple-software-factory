#!/usr/bin/env -S uv run
# /// script
# dependencies = ["openai-codex==0.155.1", "pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""Run the installed full SDLC against a disposable synthetic Git repository."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / ".claude/skills/sssf/scripts/install.py"


def _run(argv: list[str], cwd: Path, *, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        cwd=cwd,
        env={**os.environ, "ENGINEER_NAME": "M3 Probe"},
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
    )


def main(output_dir: Path) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sssf-m3-sdlc-") as temporary:
        workspace = Path(temporary)
        _run([sys.executable, str(INSTALLER)], workspace)
        _run(["git", "init", "-q"], workspace)
        _run(["git", "config", "user.email", "m3-probe@example.invalid"], workspace)
        _run(["git", "config", "user.name", "M3 Probe"], workspace)
        (workspace / "README.md").write_text("# Synthetic M3 workspace\n")
        _run(["git", "add", "."], workspace)
        _run(["git", "commit", "-qm", "初始化合成仓库"], workspace)

        prompt = (
            "Add a root-level file named greeting.txt containing exactly `hello from m3` "
            "followed by one newline. Keep the implementation limited to that file."
        )
        completed = subprocess.run(
            [sys.executable, "adws/adw_simple_sdlc.py", prompt, "--adw-id", "m3full"],
            cwd=workspace,
            env={**os.environ, "ENGINEER_NAME": "M3 Probe"},
            capture_output=True,
            text=True,
            check=False,
            timeout=900,
        )
        (output_dir / "workflow.stdout.log").write_text(completed.stdout)
        (output_dir / "workflow.stderr.log").write_text(completed.stderr)
        sessions = workspace / "adws/adw_data/sessions"
        if sessions.exists():
            destination = output_dir / "sessions"
            if destination.exists():
                raise FileExistsError(f"refusing to overwrite existing {destination}")
            shutil.copytree(sessions, destination)
        if completed.returncode != 0:
            raise AssertionError(
                f"full SDLC exited {completed.returncode}; see {output_dir} logs"
            )
        greeting = workspace / "greeting.txt"
        if not greeting.is_file() or greeting.read_text() != "hello from m3\n":
            raise AssertionError("builder did not produce the exact synthetic greeting")
        specs = sorted(path.relative_to(workspace).as_posix() for path in workspace.glob("specs/*.md"))
        docs = sorted(
            path.relative_to(workspace).as_posix()
            for path in workspace.glob("app_docs/*.md")
        )
        if not specs or not docs:
            raise AssertionError(f"missing plan or documentation: specs={specs}, docs={docs}")
        commits = _run(["git", "log", "--format=%s"], workspace).stdout.splitlines()
        if len(commits) < 4:
            raise AssertionError(f"expected initial + three workflow commits, got {commits}")
        summary = {
            "returncode": completed.returncode,
            "greeting": greeting.read_text(),
            "specs": specs,
            "docs": docs,
            "commits": commits,
            "session_artifacts": str(output_dir / "sessions"),
        }
        (output_dir / "full_sdlc_summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False) + "\n"
        )
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(main(args.output_dir))
