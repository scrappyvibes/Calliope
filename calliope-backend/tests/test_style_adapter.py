import copy

import pytest

from calliope.comfyui.adapters import adapt_style_workflow
from calliope.comfyui.parser import parse_dynamic_inputs, parse_dynamic_outputs
from calliope.comfyui.patcher import patch_workflow


def style_graph():
    def node(class_type, **inputs):
        return {"class_type": class_type, "inputs": inputs}

    return {
        "414": node("LoadImage", image="board.png"),
        "419": node("PrimitiveStringMultiline", value="style before"),
        "420": node("PrimitiveStringMultiline", value="old scene"),
        "421": node("PrimitiveStringMultiline", value="style after"),
        "422": node("Text Concatenate", text_a=["419", 0], text_b=["420", 0], text_c=["421", 0]),
        "404": node("ClaudeAPIChat", api_key=["405", 0], user_prompt=["422", 0]),
        "405": node("ClaudeAPIKey", api_key="test-value-must-not-survive"),
        "418": node("ClaudeAPIChat", image=["414", 0]),
        "79": node("Seed (rgthree)", seed=72),
        "78": node(
            "KSampler", positive=["422", 0], latent_image=["414", 0], seed=["79", 0], denoise=0.25
        ),
        "386": node(
            "KSampler", positive=["404", 0], latent_image=["78", 0], seed=["79", 0], denoise=0.1
        ),
        "392": node("SaveImage", images=["386", 0], filename_prefix="styled"),
        "900": node("PreviewImage", images=["418", 0]),
    }


def test_external_prompt_variant_keeps_render_settings_and_prunes_api_calls():
    source = style_graph()
    before = copy.deepcopy(source)
    result = adapt_style_workflow(source)
    graph = result["workflow_json"]
    assert source == before
    assert graph["78"]["inputs"] == source["78"]["inputs"]
    assert graph["386"]["inputs"] == source["386"]["inputs"]
    assert graph["404"]["class_type"] == "PrimitiveStringMultiline"
    assert graph["404"]["inputs"]["value"] == ""
    assert not any(n["class_type"].startswith("Claude") for n in graph.values())
    assert "405" not in graph and "900" not in graph
    assert {i["role"] for i in parse_dynamic_inputs(graph)} == {
        "image",
        "prompt",
        "style_detail",
        "style_prepend",
        "style_append",
        "seed",
    }
    assert [o["nodeId"] for o in parse_dynamic_outputs(graph)] == ["392"]
    patched = patch_workflow(
        graph, {"414": "new.png", "420": "new scene", "404": "detail", "79": 42}
    )
    assert patched["79"]["inputs"]["seed"] == 42
    assert patched["404"]["inputs"]["value"] == "detail"


def test_adapter_rejects_unrecognized_version():
    source = style_graph()
    source["386"]["class_type"] = "UnexpectedSampler"
    with pytest.raises(ValueError, match="386"):
        adapt_style_workflow(source)


def test_adapter_rejects_disconnected_required_input():
    source = style_graph()
    source["78"]["inputs"]["latent_image"] = "constant"
    with pytest.raises(ValueError, match="414"):
        adapt_style_workflow(source)


def test_adapter_endpoint_only_prepares_graph(client):
    response = client.post(
        "/api/workflows/adapt",
        json={"adapter": "scrappyvibes_style_external_prompts", "workflow_json": style_graph()},
    )
    assert response.status_code == 200
    assert response.json()["inputs"]
    assert client.get("/api/workflows").json() == []


def test_adapter_rejects_dangling_links():
    source = style_graph()
    source["78"]["inputs"]["model"] = ["4040", 0]
    with pytest.raises(ValueError, match="4040"):
        adapt_style_workflow(source)
