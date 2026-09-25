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
INSTALLER = ROOT / ".agents/skills/sssf/scripts/install.py"


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

        quality_path = workspace / "adws/adw_modules/quality.py"
        check = ["python3", "-c", "from pathlib import Path; assert Path('greeting.txt').read_text() == 'hello from m3\\n'"]
        quality_text = quality_path.read_text().replace('argv=_placeholder("test")', "argv=" + repr(check))
        quality_text += '\ndef not_applicable_checks():\n    return {"lint": "Synthetic text-only fixture", "typecheck": "No typed code", "build": "No build artifact"}\n'
        quality_path.write_text(quality_text)

        prompt = (
            "Add a root-level file named greeting.txt containing exactly `hello from m3` "
            "followed by one newline. Keep the implementation limited to that file."
        )
        completed = subprocess.run(
            [sys.executable, "adws/adw-simple-sdlc.py", prompt, "--adw-id", "m3full", "--spec-dir", "specs/greeting"],
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
        specs = sorted(path.relative_to(workspace).as_posix() for path in workspace.glob("specs/*/spec.md"))
        docs = sorted(
            path.relative_to(workspace).as_posix()
            for path in workspace.glob("specs/*/executions/*/*.md")
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
