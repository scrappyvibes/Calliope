"""Parse ComfyUI API-format workflows for (Input[:role]) / (Output[:role]) titled nodes."""
from __future__ import annotations

from typing import Any

from calliope.comfyui.bindings import resolve_binding
from calliope.comfyui.registry import (
    AUDIO_CLASSES,
    IMAGE_CLASSES,
    VIDEO_CLASSES,
    ComfyOutputKind,
    class_to_output_kind,
)
from calliope.comfyui.roles import (
    normalize_input_role,
    normalize_output_role,
    parse_title_tag,
)


def extract_default_value(node: dict[str, Any]) -> str | int | float | None:
    class_type = node.get("class_type", "")
    inputs = node.get("inputs") or {}
    if class_type in IMAGE_CLASSES or class_type in AUDIO_CLASSES or class_type in VIDEO_CLASSES:
        return None
    field, _ = resolve_binding(node)
    bound = inputs.get(field)
    if isinstance(bound, (str, int, float)):
        return bound
    if isinstance(inputs.get("text"), str):
        return inputs["text"]
    value = inputs.get("value")
    if isinstance(value, (str, int, float)):
        return value
    if isinstance(inputs.get("int"), (int, float)):
        return inputs["int"]
    if isinstance(inputs.get("float"), (int, float)):
        return inputs["float"]
    return None


def parse_dynamic_inputs(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        title = (node.get("_meta") or {}).get("title") or ""
        kind, role, label = parse_title_tag(title)
        if kind != "input":
            continue
        field, input_kind = resolve_binding(node)
        results.append(
            {
                "nodeId": str(node_id),
                "label": label or node.get("class_type", node_id),
                "role": normalize_input_role(role),
                "kind": input_kind,
                "field": field,
                "defaultValue": extract_default_value(node),
                "required": True,
            }
        )
    return results


def parse_dynamic_outputs(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        title = (node.get("_meta") or {}).get("title") or ""
        kind, role, label = parse_title_tag(title)
        if kind != "output":
            continue
        out_kind: ComfyOutputKind = class_to_output_kind(node.get("class_type", ""))
        canon = normalize_output_role(role)
        if canon == "video":
            out_kind = "video"
        elif canon == "image":
            out_kind = "image"
        results.append(
            {
                "nodeId": str(node_id),
                "label": label or node.get("class_type", node_id),
                "role": canon,
                "kind": out_kind,
            }
        )
    return results
