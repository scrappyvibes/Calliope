import pytest

from calliope.production import Camera, ProductionStore, World, WorldObject


def setup_project(client):
    project = client.post("/api/projects", json={"title": "Previs proof"}).json()
    return project["id"]


def test_revision_conflict_preserves_current_world(client):
    project_id = setup_project(client)
    store = ProductionStore(project_id)
    assert store.read()["revision"] == 0
    world = World(objects=[WorldObject(id="platform")])
    first = store.set_world(world, expected_revision=0)
    assert first["revision"] == 1
    with pytest.raises(ValueError, match="changed"):
        store.set_world(World(), expected_revision=0)
    assert store.read()["world"]["objects"][0]["id"] == "platform"


def test_shared_world_and_camera_revisions(client):
    from calliope.config import settings
    from calliope.db import get_db

    project_id = setup_project(client)
    db = get_db(settings.db_path)
    scene = db.execute(
        "INSERT INTO scenes(project_id,order_index,heading) VALUES (?,1,?)",
        (project_id, "EXT. STATION"),
    ).lastrowid
    ids = [
        db.execute(
            "INSERT INTO clips(project_id,scene_id,order_index) VALUES (?,?,?)",
            (project_id, scene, index),
        ).lastrowid
        for index in (1, 2)
    ]
    db.commit()
    db.close()
    store = ProductionStore(project_id)
    store.set_camera(ids[0], Camera(), expected_revision=0)
    store.set_camera(ids[1], Camera(position=(4, -6, 3)), expected_revision=1)
    before = store.read()
    shot_hashes = {shot["clip_id"]: shot["source_hash"] for shot in before["shots"]}
    store.set_camera(ids[0], Camera(position=(8, -6, 3)), expected_revision=2)
    after = store.read()
    after_hashes = {shot["clip_id"]: shot["source_hash"] for shot in after["shots"]}
    assert shot_hashes[ids[0]] != after_hashes[ids[0]]
    assert shot_hashes[ids[1]] == after_hashes[ids[1]]
    store.set_world(World(objects=[WorldObject(id="bench")]), expected_revision=3)
    assert all(
        shot["source_hash"] != after_hashes[shot["clip_id"]] for shot in store.read()["shots"]
    )
    assert len(list((store.root / "revisions").glob("*.json"))) == 4


def test_camera_cannot_target_another_project(client):
    from calliope.config import settings
    from calliope.db import get_db

    project_id = setup_project(client)
    other_id = setup_project(client)
    db = get_db(settings.db_path)
    scene = db.execute(
        "INSERT INTO scenes(project_id,order_index) VALUES (?,1)", (other_id,)
    ).lastrowid
    clip = db.execute(
        "INSERT INTO clips(project_id,scene_id,order_index) VALUES (?,?,1)", (other_id, scene)
    ).lastrowid
    db.commit()
    db.close()
    with pytest.raises(ValueError, match="project"):
        ProductionStore(project_id).set_camera(clip, Camera(), expected_revision=0)


def test_invalid_camera_and_duplicate_world_ids():
    with pytest.raises(ValueError):
        Camera(position=(0, 0, 0), target=(0, 0, 0))
    with pytest.raises(ValueError):
        World(objects=[WorldObject(id="bench"), WorldObject(id="bench")])


def test_production_api_conflicts_and_scope(client):
    project_id = setup_project(client)
    endpoint = f"/api/projects/{project_id}/production"
    assert client.get(endpoint).json()["revision"] == 0
    update = {"expected_revision": 0, "world": {"objects": [{"id": "bench"}]}}
    assert client.put(endpoint + "/world", json=update).status_code == 200
    assert client.put(endpoint + "/world", json=update).status_code == 409
    assert client.get("/api/projects/999999/production").status_code == 404


def test_interrupted_write_keeps_previous_revision(client, monkeypatch):
    from calliope import production

    project_id = setup_project(client)
    store = ProductionStore(project_id)
    store.set_world(World(objects=[WorldObject(id="old")]), expected_revision=0)
    replace = production.os.replace

    def interrupt(source, target):
        if target.name == "production.json":
            raise OSError("simulated interruption before commit")
        return replace(source, target)

    monkeypatch.setattr(production.os, "replace", interrupt)
    with pytest.raises(OSError):
        store.set_world(World(objects=[WorldObject(id="new")]), expected_revision=1)
    assert store.read()["revision"] == 1
    assert store.read()["world"]["objects"][0]["id"] == "old"
    monkeypatch.setattr(production.os, "replace", replace)
    assert (
        store.set_world(World(objects=[WorldObject(id="new")]), expected_revision=1)["revision"]
        == 2
    )


@pytest.mark.asyncio
async def test_agent_tools_use_context_project(client):
    from calliope.agent.harness.plugins.production import register
    from calliope.agent.harness.registry import ToolContext, ToolRegistry

    project_id = setup_project(client)
    registry = ToolRegistry()
    register(registry)
    ctx = ToolContext(session_id=1, project_id=project_id)
    result = await registry.execute(
        ctx,
        "set_production_world",
        {
            "expected_revision": 0,
            "world": {"objects": [{"id": "station"}]},
        },
    )
    assert result["ok"]
    assert ProductionStore(project_id).read()["world"]["objects"][0]["id"] == "station"
    denied = await registry.execute(ToolContext(session_id=1), "get_production", {})
    assert not denied["ok"]
    rejected = await registry.execute(ctx, "get_production", {"project_id": project_id + 1})
    assert not rejected["ok"]
