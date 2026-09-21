import copy

import pytest

from calliope.comfyui.adapters import build_board_workflow
from calliope.comfyui.parser import parse_dynamic_inputs


def source():
    return {
        "55": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": "krea.safetensors", "weight_dtype": "default"},
        },
        "56": {
            "class_type": "CLIPLoader",
            "inputs": {"clip_name": "clip.safetensors", "type": "krea2", "device": "default"},
        },
        "57": {"class_type": "VAELoader", "inputs": {"vae_name": "vae.safetensors"}},
    }


def test_rough_board_needs_no_image_and_keeps_model_selection():
    original = source()
    before = copy.deepcopy(original)
    graph = build_board_workflow(original)["workflow_json"]
    assert original == before
    assert {i["role"] for i in parse_dynamic_inputs(graph)} == {"prompt", "seed", "width", "height"}
    assert graph["52"]["class_type"] == "EmptyLatentImage"
    assert graph["78"]["inputs"]["denoise"] == 1
    assert all(graph[k] == original[k] for k in original)


def test_refinement_binds_blender_frame_and_strength():
    graph = build_board_workflow(source(), refined=True)["workflow_json"]
    assert {i["role"] for i in parse_dynamic_inputs(graph)} == {
        "prompt",
        "seed",
        "image",
        "denoise",
    }
    assert graph["52"]["inputs"]["pixels"] == ["414", 0]
    assert graph["78"]["inputs"]["denoise"] == 0.5
    assert not any(n["class_type"].startswith("Claude") for n in graph.values())


def test_wrong_encoder_fails_explicitly():
    graph = source()
    graph["56"]["inputs"]["type"] = "flux"
    with pytest.raises(ValueError, match="Krea-2"):
        build_board_workflow(graph)


def test_import_route_builds_separate_preset(client):
    response = client.post(
        "/api/workflows/adapt",
        json={"adapter": "scrappyvibes_rough_board", "workflow_json": source()},
    )
    assert response.status_code == 200
    assert response.json()["kind"] == "image"
    assert client.get("/api/workflows").json() == []
