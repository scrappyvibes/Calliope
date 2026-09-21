import copy
import json
from pathlib import Path

import pytest

from calliope.comfyui.adapters import adapt_h3_workflow
from calliope.comfyui.parser import parse_dynamic_inputs, parse_dynamic_outputs
from calliope.comfyui.patcher import patch_workflow


@pytest.fixture
def graph():
    return json.loads((Path(__file__).parent / "fixtures/h3_resolved.json").read_text())


def test_reference_adapter_preserves_recipe_and_removes_unused_media(graph):
    original = copy.deepcopy(graph)
    result = adapt_h3_workflow(graph, image_count=1, video_count=1)
    adapted = result["workflow_json"]
    assert graph == original
    for key in ("192", "58", "322", "53", "332", "226", "252", "250", "336", "343"):
        assert adapted[key] == graph[key]
    for key in ("264", "339"):
        assert adapted[key]["inputs"] == graph[key]["inputs"]
    sockets = {k: v for k, v in adapted["265"]["inputs"].items() if k.startswith("ref_")}
    assert sockets == {"ref_image_size": "max", "ref_images.ref_image_0": ["51", 0],
                       "ref_videos.ref_video_0": ["27", 0]}
    assert adapted["27"]["inputs"]["force_rate"] == 24
    assert adapted["27"]["inputs"]["format"] == "None"
    assert adapted["51"]["inputs"]["image"] == ""
    assert "49" not in adapted and "48" not in adapted
    inputs = parse_dynamic_inputs(adapted)
    assert {i["role"] for i in inputs} == {"image_1", "video_1", "prompt", "seed", "duration"}
    assert {o["nodeId"] for o in parse_dynamic_outputs(adapted)} == {"264", "339"}
    patched = patch_workflow(adapted, {"256": 42, "259": 5, "334": "new prompt", "51": "new.png"})
    assert patched["256"]["inputs"]["noise_seed"] == 42
    assert patched["259"]["inputs"]["value"] == 5
    assert patched["334"]["inputs"]["text"] == "new prompt"
    assert patched["51"]["inputs"]["image"] == "new.png"


@pytest.mark.parametrize("counts", [(0, 0, 0), (9, 3, 1), (10, 0, 0), (1, -1, 0)])
def test_reference_limits(graph, counts):
    with pytest.raises(ValueError):
        adapt_h3_workflow(graph, image_count=counts[0], video_count=counts[1], audio_count=counts[2])


def test_all_supported_modalities_and_contiguous_labels(graph):
    adapted = adapt_h3_workflow(graph, image_count=6, video_count=3, audio_count=3)["workflow_json"]
    refs = adapted["339"]["_meta"]["calliope_adapter"]["references"]
    assert len(refs) == 12
    assert refs[-1] == {"label": "Audio 3", "kind": "audio", "node_id": "15", "socket": "ref_audios.ref_audio_2"}
    assert not any(k.startswith("ref_video_audios") for k in adapted["265"]["inputs"])


def test_missing_bypassed_reference_requires_new_export(graph):
    del graph["265"]["inputs"]["ref_videos.ref_video_0"]
    with pytest.raises(ValueError, match="Enable that reference loader"):
        adapt_h3_workflow(graph, video_count=1)


def test_dangling_recipe_link_fails(graph):
    graph["53"]["inputs"]["model"] = ["missing", 0]
    with pytest.raises(ValueError, match="missing"):
        adapt_h3_workflow(graph)


def test_adapter_endpoint_is_preparation_only(client, graph):
    result = client.post("/api/workflows/adapt", json={
        "adapter": "scrappyvibes_h3_references", "workflow_json": graph,
        "image_count": 1, "video_count": 1,
    })
    assert result.status_code == 200
    assert result.json()["kind"] == "video"
    assert client.get("/api/workflows").json() == []
