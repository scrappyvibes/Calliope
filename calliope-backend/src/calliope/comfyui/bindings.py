"""Explicit widget bindings kept with the graph, shared by discovery and patching."""

from __future__ import annotations

from typing import Any

from calliope.comfyui.registry import class_to_input_kind, class_to_patch_field

INPUT_KINDS = frozenset({"text", "textarea", "number", "image", "image_url", "audio", "video"})


def validate_api_workflow(workflow: dict[str, Any]) -> None:
    if isinstance(workflow.get("nodes"), list):
        raise ValueError(
            "This is a ComfyUI editor graph. Export it using Save (API Format) in the "
            "installed ComfyUI frontend so custom nodes, Set/Get links and bypasses resolve."
        )
    if not workflow:
        raise ValueError("The API workflow is empty.")
    for node_id, node in workflow.items():
        if (
            not isinstance(node, dict)
            or not isinstance(node.get("class_type"), str)
            or not node["class_type"]
            or not isinstance(node.get("inputs"), dict)
        ):
            raise ValueError(f"Node {node_id} must have a class_type and inputs (API Format).")
        resolve_binding(node)  # Fail invalid explicit mappings during import, not a render.


def resolve_binding(node: dict[str, Any]) -> tuple[str, str]:
    inputs = node.get("inputs") or {}
    meta = node.get("_meta") or {}
    if not isinstance(meta, dict):
        raise ValueError("Node _meta must be an object.")
    explicit = meta.get("calliope_input")
    class_type = node.get("class_type", "")
    kind = class_to_input_kind(class_type)
    if explicit is not None:
        if not isinstance(explicit, dict):
            raise ValueError("calliope_input must be an object with an existing field.")
        field = explicit.get("field")
        if not isinstance(field, str) or field not in inputs:
            raise ValueError(f"Bound field {field!r} does not exist on {class_type}.")
        if isinstance(inputs[field], (list, dict)):
            raise ValueError(f"Bound field {field!r} is a connection or structured widget.")
        kind = explicit.get("kind", kind)
        if not isinstance(kind, str) or kind not in INPUT_KINDS:
            raise ValueError(f"Unsupported input kind {kind!r}.")
        return field, kind

    field = class_to_patch_field(class_type)
    if field not in inputs:
        siblings = {"text": "value", "value": "text", "audio": "audio:", "audio:": "audio"}
        alternate = siblings.get(field)
        if alternate in inputs:
            field = alternate
    return field, kind
