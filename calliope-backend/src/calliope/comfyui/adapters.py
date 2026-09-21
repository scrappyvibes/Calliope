"""Version-checked adapters for the captain's production graphs.

These prepare copies only. They neither mutate the supplied source nor submit jobs.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from calliope.comfyui.bindings import validate_api_workflow


def _ancestors(graph: dict[str, Any], output: str) -> set[str]:
    pending = [output]
    visited: set[str] = set()
    while pending:
        node_id = pending.pop()
        if node_id in visited:
            continue
        if node_id not in graph:
            raise ValueError(f"Workflow connection points to missing node {node_id}.")
        visited.add(node_id)
        for value in graph[node_id]["inputs"].values():
            if (
                isinstance(value, list)
                and len(value) == 2
                and isinstance(value[0], str)
                and isinstance(value[1], int)
            ):
                pending.append(value[0])
    return visited


def adapt_style_workflow(source: dict[str, Any]) -> dict[str, Any]:
    """Convert GPT to Style to explicit external prompts, retaining its render recipe."""
    validate_api_workflow(source)
    expected = {
        "414": "LoadImage",
        "419": "PrimitiveStringMultiline",
        "420": "PrimitiveStringMultiline",
        "421": "PrimitiveStringMultiline",
        "404": "ClaudeAPIChat",
        "79": "Seed (rgthree)",
        "78": "KSampler",
        "386": "KSampler",
        "392": "SaveImage",
    }
    for node_id, class_type in expected.items():
        if source.get(node_id, {}).get("class_type") != class_type:
            raise ValueError(
                f"GPT to Style version mismatch: expected {class_type} at node {node_id}. "
                "Use the original supplied API graph, or update its adapter explicitly."
            )
    graph = copy.deepcopy(source)
    graph["404"] = {"class_type": "PrimitiveStringMultiline", "inputs": {"value": ""}}
    graph["420"]["inputs"]["value"] = ""
    required = _ancestors(graph, "392")
    bindings = {
        "414": ("Refined board", "image", "image", "image"),
        "420": ("Scene prompt", "prompt", "value", "textarea"),
        "404": ("Second-pass prompt", "style_detail", "value", "textarea"),
        "419": ("Style prepend", "style_prepend", "value", "textarea"),
        "421": ("Style append", "style_append", "value", "textarea"),
        "79": ("Seed", "seed", "seed", "number"),
    }
    missing = set(bindings) - required
    if missing:
        raise ValueError(
            f"Required style inputs are disconnected from output 392: {sorted(missing)}"
        )
    graph = {key: node for key, node in graph.items() if key in required}
    if any(node["class_type"].startswith("Claude") for node in graph.values()):
        raise ValueError("Unexpected Claude API dependency remains in the image output branch.")
    for node_id, (label, role, field, kind) in bindings.items():
        graph[node_id].setdefault("_meta", {}).update(
            {
                "title": f"{label} (Input:{role})",
                "calliope_input": {"field": field, "kind": kind},
            }
        )
    source_hash = hashlib.sha256(
        json.dumps(source, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    graph["392"].setdefault("_meta", {}).update(
        {
            "title": "Styled board (Output:image)",
            "calliope_adapter": {
                "name": "scrappyvibes_style_external_prompts",
                "version": 1,
                "source_graph_sha256": source_hash,
            },
        }
    )
    validate_api_workflow(graph)
    return {
        "workflow_json": graph,
        "kind": "image",
        "notes": [
            "Scene and second-pass prompts are supplied by the agent or entered in the form.",
            "Claude API nodes and unused preview branches were removed from this copy.",
            "Sampler settings, models, LoRAs, style text and color correction are preserved.",
        ],
    }


def adapt_h3_workflow(
    source: dict[str, Any],
    *,
    image_count: int = 1,
    video_count: int = 0,
    audio_count: int = 0,
) -> dict[str, Any]:
    """Bind selected reference slots from the installed frontend's resolved export.

    Counts retain contiguous sockets in their original order. Video references are
    motion-only: the source graph does not connect their audio outputs.
    """
    validate_api_workflow(source)
    counts = (image_count, video_count, audio_count)
    if any(type(n) is not int or not 0 <= n <= cap for n, cap in zip(counts, (9, 3, 3))):
        raise ValueError("H3 supports 0–9 images, 0–3 videos and 0–3 audio references.")
    if not 1 <= sum(counts) <= 12:
        raise ValueError("H3 reference count must be between 1 and 12 combined.")
    expected = {
        "265": "MiniMaxH3ReferenceToVideo",
        "334": "Textbox",
        "259": "PrimitiveFloat",
        "256": "RandomNoise",
        "250": "ComfyMathExpression",
        "252": "ResolutionSelector",
        "192": "UNETLoader",
        "58": "MiniMaxH3MemoryEfficientSageAttentionPatch",
        "322": "LoraLoaderModelOnly",
        "53": "LoraLoaderModelOnly",
        "332": "MiniMaxH3DualClockSamplerT8",
        "226": "SamplerCustomAdvanced",
        "264": "VHS_VideoCombine",
        "339": "VHS_VideoCombine",
        "336": "RTXVideoSuperResolution",
        "343": "VRAM_Debug",
    }
    for node_id, class_type in expected.items():
        if source.get(node_id, {}).get("class_type") != class_type:
            raise ValueError(f"H3 version mismatch: expected {class_type} at node {node_id}.")
    graph = copy.deepcopy(source)
    reference_inputs = graph["265"]["inputs"]
    # Never retain unselected sockets or stale sample filenames from the source.
    for key in list(reference_inputs):
        if key.startswith(("ref_images.", "ref_videos.", "ref_audios.", "ref_video_audios.")):
            del reference_inputs[key]
    bindings = {
        "334": ("H3 prompt", "prompt", "text", "textarea"),
        "259": ("Duration in seconds", "duration", "value", "number"),
        "256": ("Seed", "seed", "noise_seed", "number"),
    }
    manifest = []
    for kind, group, prefix, count, cls, field, label in (
        ("image", "ref_images", "ref_image", image_count, "LoadImage", "image", "Picture"),
        ("video", "ref_videos", "ref_video", video_count, "VHS_LoadVideo", "video", "Video"),
        ("audio", "ref_audios", "ref_audio", audio_count, "LoadAudio", "audio", "Audio"),
    ):
        for index in range(count):
            socket = f"{group}.{prefix}_{index}"
            link = source["265"]["inputs"].get(socket)
            if (
                not isinstance(link, list)
                or len(link) != 2
                or link[1] != 0
                or source.get(str(link[0]), {}).get("class_type") != cls
            ):
                raise ValueError(
                    f"Missing resolved {socket}. Enable that reference loader and export "
                    "API Format in the installed ComfyUI frontend again."
                )
            node_id = str(link[0])
            if node_id in bindings:
                raise ValueError("H3 reference slots must use distinct loaders.")
            reference_inputs[socket] = link
            graph[node_id]["inputs"][field] = ""
            if kind == "video":
                graph[node_id]["inputs"].update(force_rate=24, format="None")
            bindings[node_id] = (f"{label} {index + 1}", f"{kind}_{index + 1}", field, kind)
            manifest.append(
                {
                    "label": f"{label} {index + 1}",
                    "kind": kind,
                    "node_id": node_id,
                    "socket": socket,
                }
            )
    graph["334"]["inputs"]["text"] = ""
    required = _ancestors(graph, "264") | _ancestors(graph, "339")
    if set(bindings) - required:
        raise ValueError("H3 prompt, duration, seed or reference is disconnected from outputs.")
    graph = {key: node for key, node in graph.items() if key in required}
    for node_id, (label, role, field, kind) in bindings.items():
        if field not in source[node_id]["inputs"]:
            raise ValueError(f"H3 version mismatch: missing {field} on node {node_id}.")
        graph[node_id].setdefault("_meta", {}).update(
            title=f"{label} (Input:{role})",
            calliope_input={"field": field, "kind": kind},
        )
    receipt = {
        "name": "scrappyvibes_h3_references",
        "version": 1,
        "source_graph_sha256": hashlib.sha256(
            json.dumps(source, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "references": manifest,
        "video_reference_fps": 24,
    }
    for node_id, label in (("264", "Native video"), ("339", "Upscaled video")):
        graph[node_id].setdefault("_meta", {}).update(
            title=f"{label} (Output:video)",
            calliope_adapter=receipt,
        )
    validate_api_workflow(graph)
    return {
        "workflow_json": graph,
        "kind": "video",
        "notes": [
            "Models, LoRAs, samplers, resolution, duration grid "
            "and both output branches are preserved.",
            "Selected references are required; unused sockets and example media are removed.",
            "Video loaders explicitly decode at 24 fps without the AnimateDiff preset; "
            "their audio is not connected.",
        ],
    }


def build_board_workflow(source: dict[str, Any], *, refined: bool = False) -> dict[str, Any]:
    """Separate neutral Krea draft/refinement graphs using A's installed model names.

    The installed Comfy core Krea-2 blueprint uses EmptyLatentImage, eight Euler
    steps, CFG 1 and simple scheduling. No style LoRAs or second pass are copied.
    """
    validate_api_workflow(source)
    for key, cls in (("55", "UNETLoader"), ("56", "CLIPLoader"), ("57", "VAELoader")):
        if source.get(key, {}).get("class_type") != cls:
            raise ValueError(f"Board preset requires GPT to Style's {cls} at node {key}")
    if source["56"]["inputs"].get("type") != "krea2":
        raise ValueError("Board preset requires the Krea-2 text encoder")

    def node(cls, **inputs):
        return {"class_type": cls, "inputs": inputs}

    graph = {k: copy.deepcopy(source[k]) for k in ("55", "56", "57")}
    graph.update(
        {
            "420": node("PrimitiveStringMultiline", value=""),
            "51": node("CLIPTextEncode", text=["420", 0], clip=["56", 0]),
            "58": node("ConditioningZeroOut", conditioning=["51", 0]),
            "79": node("Seed (rgthree)", seed=1),
            "78": node(
                "KSampler",
                seed=["79", 0],
                steps=8,
                cfg=1,
                sampler_name="euler",
                scheduler="simple",
                denoise=1,
                model=["55", 0],
                positive=["51", 0],
                negative=["58", 0],
                latent_image=["52", 0],
            ),
            "54": node("VAEDecode", samples=["78", 0], vae=["57", 0]),
            "392": node(
                "SaveImage",
                images=["54", 0],
                filename_prefix="Calliope_refined" if refined else "Calliope_rough",
            ),
        }
    )
    bindings = {
        "420": ("Board prompt", "prompt", "value", "textarea"),
        "79": ("Seed", "seed", "seed", "number"),
    }
    if refined:
        graph["414"] = node("LoadImage", image="")
        graph["52"] = node("VAEEncode", pixels=["414", 0], vae=["57", 0])
        graph["78"]["inputs"]["denoise"] = 0.5
        bindings.update(
            {
                "414": ("Blender frame", "image", "image", "image"),
                "78": ("Refinement strength", "denoise", "denoise", "number"),
            }
        )
    else:
        graph["10"] = node("PrimitiveInt", value=1024)
        graph["11"] = node("PrimitiveInt", value=576)
        graph["52"] = node("EmptyLatentImage", width=["10", 0], height=["11", 0], batch_size=1)
        bindings.update(
            {
                "10": ("Width", "width", "value", "number"),
                "11": ("Height", "height", "value", "number"),
            }
        )
    for key, (label, role, field, kind) in bindings.items():
        graph[key]["_meta"] = {
            "title": f"{label} (Input:{role})",
            "calliope_input": {"field": field, "kind": kind},
        }
    purpose = "refined" if refined else "rough"
    graph["392"]["_meta"] = {
        "title": f"{purpose.title()} board (Output:image)",
        "calliope_adapter": {
            "name": f"scrappyvibes_{purpose}_board",
            "version": 1,
            "source_graph_sha256": hashlib.sha256(
                json.dumps(source, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        },
    }
    validate_api_workflow(graph)
    return {
        "workflow_json": graph,
        "kind": "image",
        "notes": [
            "Separate storyboard preset using the Krea model, text encoder "
            "and VAE names from workflow A.",
            "Eight Euler steps, CFG 1, simple scheduling; no style LoRAs or SDXL second pass.",
            "Refinement starts from the bound Blender frame with editable strength."
            if refined
            else "Rough boards start from text at 1024×576; no source image is required.",
            "Workflow A remains unchanged for the subsequent styling stage.",
        ],
    }
