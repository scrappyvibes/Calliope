"""Production bindings must patch real fields and never queue an editor graph."""

import copy

import pytest

from calliope.comfyui.parser import parse_dynamic_inputs
from calliope.comfyui.patcher import patch_workflow


@pytest.mark.parametrize(
    "class_type,field",
    [
        ("Seed (rgthree)", "seed"),
        ("RandomNoise", "noise_seed"),
        ("KSampler", "seed"),
        ("KSamplerAdvanced", "noise_seed"),
    ],
)
def test_seed_binding_changes_actual_input(class_type, field):
    graph = {
        "79": {
            "class_type": class_type,
            "inputs": {field: 10},
            "_meta": {"title": "Seed (Input:seed)"},
        }
    }
    before = copy.deepcopy(graph)
    schema = parse_dynamic_inputs(graph)
    assert schema[0]["defaultValue"] == 10
    assert schema[0]["field"] == field
    result = patch_workflow(graph, {"79": 1234})
    assert result["79"]["inputs"] == {field: 1234}
    assert graph == before


def test_explicit_field_binding_for_custom_node():
    graph = {
        "252": {
            "class_type": "ResolutionSelector",
            "inputs": {"aspect_ratio": "16:9", "megapixels": 1.0},
            "_meta": {
                "title": "Aspect ratio (Input:aspect_ratio)",
                "calliope_input": {"field": "aspect_ratio", "kind": "text"},
            },
        }
    }
    schema = parse_dynamic_inputs(graph)
    assert schema[0]["field"] == "aspect_ratio"
    assert schema[0]["defaultValue"] == "16:9"
    result = patch_workflow(graph, {"252": "9:16"})
    assert result["252"]["inputs"] == {"aspect_ratio": "9:16", "megapixels": 1.0}


def test_bad_explicit_binding_fails_before_patching():
    graph = {
        "1": {
            "class_type": "PrimitiveInt",
            "inputs": {"value": 5},
            "_meta": {"title": "Seed (Input:seed)", "calliope_input": {"field": "nonexistent"}},
        }
    }
    with pytest.raises(ValueError, match="nonexistent"):
        parse_dynamic_inputs(graph)
    with pytest.raises(ValueError, match="nonexistent"):
        patch_workflow(graph, {"1": 3})


def test_binding_cannot_replace_a_graph_connection():
    graph = {
        "1": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["2", 0]},
            "_meta": {"title": "Prompt (Input:prompt)", "calliope_input": {"field": "clip"}},
        }
    }
    with pytest.raises(ValueError, match="connection"):
        patch_workflow(graph, {"1": "text"})


@pytest.mark.parametrize("path", ["/api/workflows/analyze", "/api/workflows"])
def test_editor_graph_is_rejected_with_export_guidance(client, path):
    response = client.post(
        path,
        json={
            "name": "H3",
            "kind": "video",
            "workflow_json": {
                "nodes": [{"id": 265, "type": "MiniMaxH3ReferenceToVideo"}],
                "links": [],
            },
        },
    )
    assert response.status_code == 422
    assert "API" in response.json()["detail"]
    assert client.get("/api/workflows").json() == []


def test_analyze_reports_field_and_import_preserves_it(client):
    graph = {
        "1": {
            "class_type": "Seed (rgthree)",
            "inputs": {"seed": 72},
            "_meta": {"title": "Seed (Input:seed)"},
        }
    }
    response = client.post("/api/workflows", json={"name": "Bound", "workflow_json": graph})
    assert response.status_code == 200
    data = response.json()
    assert data["input_schema"][0]["field"] == "seed"
    assert (
        client.post(f"/api/workflows/{data['id']}/reanalyze").json()["input_schema"][0]["field"]
        == "seed"
    )
