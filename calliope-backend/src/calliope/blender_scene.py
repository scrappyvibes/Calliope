"""Trusted Blender entry point. Accepts validated data, never generated Python."""

import json
import shutil
import sys
from pathlib import Path

import bpy


def build(document, destination):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.unit_settings.system = "METRIC"
    scene.render.fps = document["world"]["fps"]
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 8
    scene.render.resolution_x = 640
    scene.render.resolution_y = 360
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    world = bpy.data.worlds.new("Production world")
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.16, 0.19, 0.25, 1)
    world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.6
    scene.world = world
    for item in document["world"]["objects"]:
        kind = item["kind"]
        if kind == "box":
            bpy.ops.mesh.primitive_cube_add(size=2)
        elif kind == "sphere":
            bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=12, radius=1)
        elif kind == "cylinder":
            bpy.ops.mesh.primitive_cylinder_add(vertices=24, radius=1, depth=2)
        else:
            bpy.ops.mesh.primitive_plane_add(size=2)
        obj = bpy.context.object
        obj.name = "geo_" + item["id"]
        obj["production_object_id"] = item["id"]
        obj.location = item["position"]
        obj.rotation_euler = item["rotation"]
        obj.scale = item["scale"]
        material = bpy.data.materials.new("material_" + item["id"])
        material.diffuse_color = (*item["color"], 1)
        material.use_nodes = True
        shader = material.node_tree.nodes.get("Principled BSDF")
        shader.inputs["Base Color"].default_value = (*item["color"], 1)
        shader.inputs["Roughness"].default_value = 0.8
        obj.data.materials.append(material)
    for name, position, energy, size in [
        ("Key", (0, -3, 8), 1600, 8),
        ("Fill", (4, 5, 6), 900, 6),
    ]:
        data = bpy.data.lights.new(name, "AREA")
        data.energy, data.shape, data.size = energy, "DISK", size
        light = bpy.data.objects.new(name, data)
        scene.collection.objects.link(light)
        light.location = position
    for shot in document["shots"]:
        name = f"shot_{shot['clip_id']}"
        data = bpy.data.cameras.new(name)
        camera = bpy.data.objects.new(name, data)
        scene.collection.objects.link(camera)
        target = bpy.data.objects.new(name + "_target", None)
        scene.collection.objects.link(target)
        constraint = camera.constraints.new("TRACK_TO")
        constraint.target, constraint.track_axis, constraint.up_axis = (
            target,
            "TRACK_NEGATIVE_Z",
            "UP_Y",
        )
        for frame, pose in [
            (shot["frame_start"], shot["camera"]),
            (shot["frame_end"], shot.get("end_camera") or shot["camera"]),
        ]:
            camera.location = pose["position"]
            camera.keyframe_insert(data_path="location", frame=frame)
            target.location = pose["target"]
            target.keyframe_insert(data_path="location", frame=frame)
            data.lens, data.sensor_width = pose["lens_mm"], pose["sensor_width_mm"]
            data.keyframe_insert(data_path="lens", frame=frame)
            data.keyframe_insert(data_path="sensor_width", frame=frame)
        camera["production_clip_id"] = shot["clip_id"]
        reference = next(
            (b for b in document.get("boards", []) if b["id"] == shot.get("rough_board_id")), None
        )
        if reference:
            image = bpy.data.images.load(reference["path"], check_existing=True)
            image.pack()
            data.show_background_images = True
            background = data.background_images.new()
            background.image = image
            background.alpha = 0.35
            background.display_depth = "BACK"
            camera["rough_board_id"] = reference["id"]
            camera["rough_board_sha256"] = reference["sha256"]
    scene["production_revision"] = document["revision"]
    scene["production_project_id"] = document["project_id"]
    scene.camera = bpy.data.objects[f"shot_{document['shots'][0]['clip_id']}"]
    scene.frame_set(document["shots"][0]["frame_start"])
    bpy.ops.wm.save_as_mainfile(filepath=str(destination / "world.blend"))
    return scene


def main():
    source = Path(sys.argv[sys.argv.index("--") + 1])
    request = json.loads(source.read_text(encoding="utf-8"))
    destination = source.parent
    document = request["document"]
    scene = build(document, destination)
    artifacts = []
    for shot in document["shots"]:
        if shot["clip_id"] not in request["clip_ids"]:
            continue
        scene.camera = bpy.data.objects[f"shot_{shot['clip_id']}"]
        frames = (
            range(shot["frame_start"], shot["frame_end"] + 1)
            if request["mode"] == "animation"
            else sorted({shot["frame_start"], shot["frame_end"]})
        )
        # The current world schema has no object animation. A locked camera's
        # frames are identical, so retain the complete sequence without tracing
        # the same scene hundreds of times. Moving cameras still render each frame.
        stationary = not shot.get("end_camera") or shot["end_camera"] == shot["camera"]
        first_render = None
        for frame in frames:
            scene.frame_set(frame)
            filename = f"shot-{shot['clip_id']}-{frame:06d}.png"
            target = destination / filename
            if stationary and first_render is not None:
                shutil.copyfile(first_render, target)
            else:
                scene.render.filepath = str(target)
                bpy.ops.render.render(write_still=True)
                first_render = target
            artifacts.append({"clip_id": shot["clip_id"], "frame": frame, "file": filename})
    (destination / "rendered.json").write_text(
        json.dumps(
            {
                "revision": document["revision"],
                "artifacts": artifacts,
                "object_ids": [o["id"] for o in document["world"]["objects"]],
                "blender_version": bpy.app.version_string,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
