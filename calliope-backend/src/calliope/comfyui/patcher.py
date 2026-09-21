"""Patch user values into ComfyUI API-format workflow by nodeId."""
from __future__ import annotations

import copy
from typing import Any

from calliope.comfyui.bindings import resolve_binding


def patch_workflow(
    base: dict[str, Any],
    values_by_node_id: dict[str, Any],
) -> dict[str, Any]:
    patched = copy.deepcopy(base)
    for node_id, value in values_by_node_id.items():
        if value is None:
            continue
        key = str(node_id)
        node = patched.get(key)
        if not isinstance(node, dict):
            continue
        inputs = dict(node.get("inputs") or {})
        field, _ = resolve_binding(node)
        inputs[field] = value
        node["inputs"] = inputs
        patched[key] = node
    return patched
