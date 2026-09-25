from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".agents/skills/sssf"
INSTALLER = SKILL / "scripts/install.py"


def _run(script: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _load_installer():
    spec = importlib.util.spec_from_file_location("sssf_m4_installer", INSTALLER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_distribution_has_one_codex_runtime_and_no_retired_extensions() -> None:
    modules = SKILL / "templates/adws/adw_modules"
    assert (modules / "agents.py").is_file()
    assert not (modules / "agents_codex.py").exists()
    assert not (modules / "agent_pi.py").exists()
    retired_extensions = SKILL / "templates/harness_engineering"
    assert not retired_extensions.exists() or not any(retired_extensions.iterdir())

    forbidden = (
        "PiRequest",
        "PiResult",
        "PI_PATH",
        "PI_MODELS_PATH",
        "@mariozechner/pi",
        ".pi/",
        "subagent_create",
        "subagent_continue",
        "subagent_list",
        "subagent_remove",
        "coding_agent: pi",
        "agent_pi",
    )
    active_files = [ROOT / "README.md", SKILL / "SKILL.md"]
    active_files.extend(SKILL.glob("cookbooks/*.md"))
    active_files.extend(SKILL.glob("references/*.md"))
    active_files.extend(SKILL.glob("templates/**/*"))
    active_files.extend(SKILL.glob("scripts/*.py"))
    offenders = []
    for path in active_files:
        if not path.is_file() or path == INSTALLER or "__pycache__" in path.parts:
            continue
        text = path.read_text(errors="replace")
        for needle in forbidden:
            if needle in text:
                offenders.append(f"{path.relative_to(ROOT)}: {needle}")
    assert offenders == []


def test_fresh_install_is_repeatable_and_records_manifest(tmp_path: Path) -> None:
    target = tmp_path / "fresh"
    first = _run(INSTALLER, "--root", str(target))
    assert first.returncode == 0, first.stderr + first.stdout
    manifest_path = target / ".sssf/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["distribution_version"] == "auto-review-v1-codex-sdk-0.155.1"
    assert (target / "adws/adw_modules/agents.py").is_file()
    assert not (target / "adws/adw_modules/agents_codex.py").exists()
    assert not (target / "adws/adw_data/harness_engineering").exists()
    assert not (target / ".env.example").exists()
    assert not (target / ".env.sample").exists()
    ignored = (target / ".gitignore").read_text().splitlines()
    assert {
        "/.agents/skills/sssf/",
        "/.codex/agents/sssf_recon.toml",
        "/.sssf/",
        "/adws/",
        "/justfile",
        "/.env",
    } <= set(ignored)
    assert "/specs/" not in ignored
    assert "specs/" not in ignored
    spec = target / "specs/example/spec.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("# Track me\n")
    initialized = subprocess.run(
        ["git", "init"], cwd=target, capture_output=True, text=True, check=False,
    )
    assert initialized.returncode == 0, initialized.stderr
    ignored_result = subprocess.run(
        [
            "git", "check-ignore", "--no-index",
            ".sssf/manifest.json",
            "adws/adw_modules/agents.py",
            ".codex/agents/sssf_recon.toml",
            "justfile",
            ".agents/skills/sssf/SKILL.md",
        ],
        cwd=target,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ignored_result.returncode == 0, ignored_result.stderr
    assert set(ignored_result.stdout.splitlines()) == {
        ".sssf/manifest.json",
        "adws/adw_modules/agents.py",
        ".codex/agents/sssf_recon.toml",
        "justfile",
        ".agents/skills/sssf/SKILL.md",
    }
    tracked_spec = subprocess.run(
        ["git", "check-ignore", "--no-index", "specs/example/spec.md"],
        cwd=target,
        capture_output=True,
        text=True,
        check=False,
    )
    assert tracked_spec.returncode == 1, tracked_spec.stdout + tracked_spec.stderr
    before = manifest_path.read_bytes()
    snapshots = list((target / ".sssf/backups").iterdir())

    second = _run(INSTALLER, "--root", str(target))
    assert second.returncode == 0, second.stderr + second.stdout
    assert manifest_path.read_bytes() == before
    assert list((target / ".sssf/backups").iterdir()) == snapshots


def test_existing_user_files_are_preserved_but_managed_conflicts_stop_atomically(
    tmp_path: Path,
) -> None:
    preserved = tmp_path / "preserved"
    config = preserved / "adws/adw_sssf_config/sssf.config.yaml"
    prompt = preserved / "adws/adw_data/prompt_engineering/planner/system.md"
    config.parent.mkdir(parents=True)
    prompt.parent.mkdir(parents=True)
    config.write_text("custom config\n")
    prompt.write_text("custom prompt\n")
    legacy_module = preserved / "adws/adw_modules/agent_pi.py"
    legacy_extension = (
        preserved / "adws/adw_data/harness_engineering/subagents.ts"
    )
    legacy_module.parent.mkdir(parents=True, exist_ok=True)
    legacy_extension.parent.mkdir(parents=True, exist_ok=True)
    legacy_module.write_text("# locally changed legacy runtime\n")
    legacy_extension.write_text("// locally changed legacy extension\n")
    result = _run(INSTALLER, "--root", str(preserved))
    assert result.returncode == 0, result.stderr + result.stdout
    assert config.read_text() == "custom config\n"
    assert prompt.read_text() == "custom prompt\n"
    assert (preserved / "adws/adw_modules/agent_codex.py").is_file()
    assert not legacy_module.exists()
    assert not legacy_extension.exists()
    backup_payloads = [
        path.read_bytes()
        for path in (preserved / ".sssf/backups").glob("*/*.bin")
    ]
    assert b"# locally changed legacy runtime\n" in backup_payloads
    assert b"// locally changed legacy extension\n" in backup_payloads

    conflicted = tmp_path / "conflicted"
    module = conflicted / "adws/adw_modules/agents.py"
    module.parent.mkdir(parents=True)
    module.write_text("# custom runtime\n")
    failed = _run(INSTALLER, "--root", str(conflicted))
    assert failed.returncode == 2
    assert module.read_text() == "# custom runtime\n"
    assert not (conflicted / ".sssf/manifest.json").exists()
    assert not (conflicted / "adws/adw_modules/agent_codex.py").exists()


def test_install_failure_restores_every_target_file(tmp_path: Path, monkeypatch) -> None:
    installer = _load_installer()
    target = tmp_path / "transaction"
    target.mkdir()
    sources = installer.collect_sources(target)
    actions, _, conflicts = installer.plan_install(
        target, sources, force_managed=False,
    )
    assert not conflicts
    real_atomic_write = installer._atomic_write
    calls = 0

    def fail_once(path, data, mode=0o644):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("synthetic write failure")
        return real_atomic_write(path, data, mode)

    monkeypatch.setattr(installer, "_atomic_write", fail_once)
    with pytest.raises(OSError, match="synthetic"):
        installer.apply_install(target, actions, sources)
    assert not (target / ".sssf/manifest.json").exists()
    assert not (target / "adws/adw_modules/agents.py").exists()
    assert not (target / "adws/adw_sssf_config/sssf.config.yaml").exists()


def test_managed_update_can_roll_back_with_matching_config_and_db(tmp_path: Path) -> None:
    target = tmp_path / "rollback"
    assert _run(INSTALLER, "--root", str(target)).returncode == 0
    agents = target / "adws/adw_modules/agents.py"
    config = target / "adws/adw_sssf_config/sssf.config.yaml"
    database = target / "adws/adw_data/sssf.db"
    drifted = agents.read_text() + "\n# reviewed local managed drift\n"
    agents.write_text(drifted)
    config.write_text("custom config kept during managed update\n")
    database.parent.mkdir(parents=True, exist_ok=True)
    database.write_bytes(b"schema-v2-backup")

    update = _run(INSTALLER, "--root", str(target), "--force-managed")
    assert update.returncode == 0, update.stderr + update.stdout
    assert "reviewed local managed drift" not in agents.read_text()
    assert config.read_text() == "custom config kept during managed update\n"
    assert database.read_bytes() == b"schema-v2-backup"

    restored = _run(INSTALLER, "--root", str(target), "--rollback", "latest")
    assert restored.returncode == 0, restored.stderr + restored.stdout
    assert agents.read_text() == drifted
    assert config.read_text() == "custom config kept during managed update\n"
    assert database.read_bytes() == b"schema-v2-backup"


def test_generated_adw_uses_pinned_sdk_rich_and_run_finish(tmp_path: Path) -> None:
    target = tmp_path / "generated"
    assert _run(INSTALLER, "--root", str(target)).returncode == 0
    generated = _run(
        SKILL / "scripts/make_adw.py",
        "--name",
        "m4_smoke",
        "--agents",
        "scout,builder",
        cwd=target,
    )
    assert generated.returncode == 0, generated.stderr + generated.stdout
    script = target / "adws/adw-m4-smoke.py"
    text = script.read_text()
    assert '"openai-codex==0.155.1"' in text
    assert '"rich"' in text
    assert "return run.finish(accepted=accepted" in text
    assert "builder -> code(checks) -> reviewer" in text
    assert "delivery.execute(run" in text
    assert "run.succeeded" not in text
    compile(text, str(script), "exec")
    launched = subprocess.run(
        ["uv", "run", str(script), "--help"],
        cwd=target,
        capture_output=True,
        text=True,
        check=False,
    )
    assert launched.returncode == 0, launched.stderr + launched.stdout
