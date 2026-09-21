#!/usr/bin/env -S uv run
# /// script
# dependencies = []
# ///
"""make_config — generate adws/adw_sssf_config/sssf.config.yaml with great defaults.

Usage:
    uv run <skill>/scripts/make_config.py [--force]
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "sssf.config.yaml"
ROLE_TEMPLATE = (
    Path(__file__).resolve().parent.parent
    / "templates" / "codex_agents" / "sssf_recon.toml"
)
SSSF_SKILL = Path(__file__).resolve().parent.parent / "SKILL.md"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    dest = Path.cwd() / "adws" / "adw_sssf_config" / "sssf.config.yaml"
    if dest.exists() and not args.force:
        print(f"{dest} already exists — use --force to overwrite")
        return 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(TEMPLATE, dest)
    print(f"wrote {dest}")
    role_dest = Path.cwd() / ".codex" / "agents" / "sssf_recon.toml"
    if not role_dest.exists() or args.force:
        role_dest.parent.mkdir(parents=True, exist_ok=True)
        role_dest.write_text(ROLE_TEMPLATE.read_text().replace(
            "{{sssf_skill_path_toml}}", json.dumps(str(SSSF_SKILL.resolve())),
        ))
        shutil.copymode(ROLE_TEMPLATE, role_dest)
        print(f"wrote {role_dest}")
    else:
        print(f"kept existing {role_dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
