"""Structural and isolated entrypoint validation before revision activation."""

from evog.harness.executor import validate_component
from evog.harness.schema import Harness


def validate_components(harness: Harness) -> dict:
    checked = []
    for entry in [*harness.tool_registry, *harness.middleware_registry]:
        if not entry.get("enabled", True):
            continue
        handler = entry["handler"]
        if handler in checked:
            continue
        validate_component(harness.contents, handler)
        checked.append(handler)
    return {
        "entrypoints": checked,
        "revision_id": harness.id,
        "validation": "structure_and_isolated_entrypoints",
    }
