"""Convert Pydantic schemas to the strict Codex structured-output subset."""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel


class UnsupportedOutputSchema(ValueError):
    pass


_ALLOWED_KEYS = {
    "$defs", "$ref", "additionalProperties", "anyOf", "const", "description",
    "enum", "items", "properties", "required", "title", "type",
}


def strict_output_schema(output_type: type[BaseModel]) -> dict[str, Any]:
    """Return a closed, all-fields-required schema accepted by Codex 0.155.1.

    Defaults are host-side Pydantic behaviour and are not response-format
    constraints, so they are removed. Optional fields remain nullable through
    their ``anyOf`` branch but are still present in the generated JSON object.
    """

    schema = copy.deepcopy(output_type.model_json_schema())
    _validate_subset(schema, path="#")

    def visit(value: Any) -> Any:
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        converted = {
            key: visit(item)
            for key, item in value.items()
            if key != "default"
        }
        properties = converted.get("properties")
        if converted.get("type") == "object" or isinstance(properties, dict):
            converted["additionalProperties"] = False
            converted["required"] = list((properties or {}).keys())
        return converted

    return visit(schema)


def _validate_subset(value: Any, *, path: str) -> None:
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_subset(item, path=f"{path}/{index}")
        return
    if not isinstance(value, dict):
        return

    # These objects are name -> schema maps. Their keys are user field/type
    # names, not JSON Schema keywords.
    if path.endswith("/properties") or path.endswith("/$defs"):
        for name, item in value.items():
            _validate_subset(item, path=f"{path}/{name}")
        return

    unsupported = set(value) - _ALLOWED_KEYS - {"default"}
    if unsupported:
        names = ", ".join(sorted(unsupported))
        raise UnsupportedOutputSchema(
            f"unsupported JSON Schema keyword(s) at {path}: {names}"
        )
    if "additionalProperties" in value and value["additionalProperties"] not in (False, None):
        raise UnsupportedOutputSchema(
            f"open-ended object is not supported at {path}"
        )
    for key, item in value.items():
        _validate_subset(item, path=f"{path}/{key}")
