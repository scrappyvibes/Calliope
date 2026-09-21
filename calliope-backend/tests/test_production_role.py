from calliope.agent.harness.orchestrator import ROLE_TOOLS, _scoped_payload
from calliope.agent.harness.registry import ToolContext


def test_production_role_exposes_complete_pipeline_in_linked_project(client):
    pid = client.post("/api/projects", json={"title":"Production role"}).json()["id"]
    payload = _scoped_payload(ToolContext(session_id=1, project_id=pid), ROLE_TOOLS["production"])
    names = {t["function"]["name"] for t in payload}
    assert set(ROLE_TOOLS["production"]) == names
    assert {"get_production_workflow", "inspect_production_image", "generate_storyboard",
            "render_production_previs", "generate_production_video", "select_production_video"} <= names
    assert "enqueue_video_jobs" not in names


def test_script_role_has_scene_and_clip_creation(client):
    pid = client.post("/api/projects", json={"title":"Script role"}).json()["id"]
    payload = _scoped_payload(ToolContext(session_id=1, project_id=pid), ROLE_TOOLS["script"])
    assert {"get_workspace", "list_scenes", "add_scene", "add_clip"} <= {t["function"]["name"] for t in payload}
