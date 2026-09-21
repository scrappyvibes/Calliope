import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace


def test_locked_camera_reuses_frame_but_moving_camera_renders_every_frame(tmp_path, monkeypatch):
    scene = SimpleNamespace(render=SimpleNamespace(filepath=""), frame_set=lambda frame: None)
    renders = []

    def render(**kwargs):
        renders.append(scene.render.filepath)
        Path(scene.render.filepath).write_bytes(b"rendered frame")

    bpy = SimpleNamespace(
        data=SimpleNamespace(objects={"shot_1": object(), "shot_2": object()}),
        ops=SimpleNamespace(render=SimpleNamespace(render=render)),
        app=SimpleNamespace(version_string="test"),
    )
    monkeypatch.setitem(sys.modules, "bpy", bpy)
    path = Path(__file__).parents[1] / "src/calliope/blender_scene.py"
    spec = importlib.util.spec_from_file_location("blender_scene_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "build", lambda document, destination: scene)
    request = {
        "mode": "animation",
        "clip_ids": [1, 2],
        "document": {
            "revision": 1,
            "world": {"objects": []},
            "shots": [
                {
                    "clip_id": 1,
                    "camera": {"position": [0, 0, 0]},
                    "end_camera": None,
                    "frame_start": 1,
                    "frame_end": 3,
                },
                {
                    "clip_id": 2,
                    "camera": {"position": [0, 0, 0]},
                    "end_camera": {"position": [1, 0, 0]},
                    "frame_start": 1,
                    "frame_end": 3,
                },
            ],
        },
    }
    source = tmp_path / "input.json"
    source.write_text(json.dumps(request), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["blender", "--", str(source)])
    module.main()
    assert [Path(p).name for p in renders] == [
        "shot-1-000001.png",
        "shot-2-000001.png",
        "shot-2-000002.png",
        "shot-2-000003.png",
    ]
    result = json.loads((tmp_path / "rendered.json").read_text())
    assert len(result["artifacts"]) == 6
    assert all(
        (tmp_path / item["file"]).read_bytes() == b"rendered frame" for item in result["artifacts"]
    )
