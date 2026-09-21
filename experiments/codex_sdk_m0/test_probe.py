from __future__ import annotations

import dataclasses
import importlib.util
import json
from enum import Enum
from pathlib import Path

from pydantic import BaseModel

PROBE_PATH = Path(__file__).with_name("probe.py")
SPEC = importlib.util.spec_from_file_location("codex_sdk_m0_probe", PROBE_PATH)
assert SPEC and SPEC.loader
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class Choice(str, Enum):
    accepted = "accepted"


class Payload(BaseModel):
    snake_name: str


@dataclasses.dataclass
class Container:
    choice: Choice
    payload: Payload
    path: Path


def test_jsonable_preserves_sdk_like_shapes() -> None:
    value = Container(Choice.accepted, Payload(snake_name="ok"), Path("/tmp/example"))
    assert probe._jsonable(value) == {
        "choice": "accepted",
        "payload": {"snake_name": "ok"},
        "path": "/tmp/example",
    }


def test_every_runtime_envelope_has_an_object_schema() -> None:
    envelope_types = probe._load_envelope_types(probe._repo_root())
    assert set(envelope_types) == {
        "BuildOutput",
        "ChangesOutput",
        "DocumentOutput",
        "GenericOutput",
        "PlanOutput",
        "ReviewOutput",
        "ScoutOutput",
        "VerifyOutput",
    }
    for output_type in envelope_types.values():
        schema = output_type.model_json_schema()
        assert schema["type"] == "object"
        assert {"status", "summary", "artifacts", "notes_for_next_agent"} <= set(
            schema["properties"]
        )


def test_strict_schema_closes_every_object_and_removes_defaults() -> None:
    review_type = probe._load_envelope_types(probe._repo_root())["ReviewOutput"]
    schema = probe._output_schema(review_type)
    assert schema["additionalProperties"] is False
    assert schema["required"] == list(schema["properties"])
    finding = schema["$defs"]["ReviewFinding"]
    assert finding["additionalProperties"] is False
    assert finding["required"] == list(finding["properties"])
    assert "default" not in json.dumps(schema)


def test_turn_status_reads_completed_notification() -> None:
    records = [
        {"method": "turn/started", "params": {"turn": {"status": "inProgress"}}},
        {"method": "turn/completed", "params": {"turn": {"status": "interrupted"}}},
    ]
    assert probe._turn_status(records) == "interrupted"


def test_required_version_is_exact() -> None:
    lock = Path(__file__).with_name("requirements.lock").read_text().strip()
    assert lock == f"openai-codex=={probe.SDK_VERSION}"
    assert (
        json.loads(json.dumps({"version": probe.SDK_VERSION}))["version"] == "0.155.1"
    )


def test_redacted_event_samples_are_valid_jsonl() -> None:
    samples = Path(__file__).with_name("samples")
    for path in sorted(samples.glob("*.jsonl")):
        records = [json.loads(line) for line in path.read_text().splitlines()]
        assert records
        assert all({"method", "params"} <= set(record) for record in records)
