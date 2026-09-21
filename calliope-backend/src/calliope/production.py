"""Canonical shared-world/camera state, with immutable revision receipts.

Story, scenes and clips remain in Calliope's existing project database. This
document owns only production geometry and cameras keyed by those stable clip
IDs. SQLite's writer lock serializes the write-through service across processes;
an atomic file replacement commits the canonical document.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from calliope.config import settings
from calliope.db import get_db

Finite = Annotated[float, Field(allow_inf_nan=False)]
Positive = Annotated[float, Field(gt=0, le=10000, allow_inf_nan=False)]
Channel = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
Vec3 = tuple[Finite, Finite, Finite]


class ProductionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorldObject(ProductionModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    kind: Literal["box", "sphere", "cylinder", "plane"] = "box"
    position: Vec3 = (0, 0, 0)
    rotation: Vec3 = (0, 0, 0)  # Euler XYZ radians, Blender world coordinates.
    scale: tuple[Positive, Positive, Positive] = Field(
        default=(1, 1, 1),
        description=(
            "Blender scale factors, NOT full dimensions. Box, sphere and cylinder have "
            "unscaled bounds 2 x 2 x 2 meters; plane bounds are 2 x 2 meters in XY. "
            "For a box 0.4m wide, 0.3m deep and 0.5m tall use (0.2, 0.15, 0.25). "
            "Position is the object's center; ground a box at half its height."
        ),
    )
    color: tuple[Channel, Channel, Channel] = (0.6, 0.65, 0.7)


class World(ProductionModel):
    units: Literal["meters"] = "meters"
    up_axis: Literal["Z"] = "Z"
    fps: Literal[24] = 24
    objects: list[WorldObject] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({obj.id for obj in self.objects}) != len(self.objects):
            raise ValueError("World object IDs must be unique")
        return self


class Camera(ProductionModel):
    position: Vec3 = (6, -8, 5)
    target: Vec3 = (0, 0, 1)
    lens_mm: float = Field(default=40, ge=10, le=300, allow_inf_nan=False)
    sensor_width_mm: float = Field(default=36, ge=1, le=100, allow_inf_nan=False)

    @model_validator(mode="after")
    def different_target(self):
        if sum((a - b) ** 2 for a, b in zip(self.position, self.target)) < 0.000001:
            raise ValueError("Camera position and target must differ")
        return self


class ProductionShot(ProductionModel):
    clip_id: int = Field(gt=0)
    camera: Camera = Field(default_factory=Camera)
    end_camera: Camera | None = None
    frame_start: int = Field(default=1, ge=1, le=172800)
    frame_end: int = Field(default=240, ge=1, le=172800)
    rough_board_id: str | None = None

    @model_validator(mode="after")
    def frame_order(self):
        if self.frame_end < self.frame_start:
            raise ValueError("Shot frame_end must be at or after frame_start")
        return self


class StoryboardTake(ProductionModel):
    id: str
    clip_id: int = Field(gt=0)
    stage: Literal["rough", "refined", "styled"]
    path: str
    sha256: str
    source_shot_hash: str | None = None
    source_board_id: str | None = None
    previs_job_id: int | None = None
    previs_frame: int | None = None
    generation_job_id: int | None = None
    note: str = ""


class ProductionVideoTake(ProductionModel):
    id: str
    generation_job_id: int = Field(gt=0)
    output_index: int = Field(ge=0)
    path: str
    sha256: str
    clip_id: int = Field(gt=0)
    source_shot_hash: str
    production_revision: int = Field(ge=0)
    styled_board_id: str
    previs_job_id: int | None = None
    prompt: str
    seed: int = Field(ge=0)
    requested_duration_seconds: Positive
    frame_count: int = Field(gt=0)
    fps: Literal[24] = 24
    actual_duration_seconds: Positive


class ProductionDocument(ProductionModel):
    format_version: Literal[1] = 1
    project_id: int
    revision: int = 0
    world: World = Field(default_factory=World)
    shots: list[ProductionShot] = Field(default_factory=list)
    boards: list[StoryboardTake] = Field(default_factory=list)
    selected_boards: dict[str, str] = Field(default_factory=dict)
    video_takes: list[ProductionVideoTake] = Field(default_factory=list)
    selected_videos: dict[str, str] = Field(default_factory=dict)


def source_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def atomic_write(path: Path, payload: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".production-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class ProductionStore:
    def __init__(self, project_id: int):
        if not isinstance(project_id, int) or project_id < 1:
            raise ValueError("Invalid project ID")
        self.project_id = project_id
        self.root = settings.data_dir / "production" / str(project_id)

    def _load(self) -> ProductionDocument:
        path = self.root / "production.json"
        if not path.exists():
            return ProductionDocument(project_id=self.project_id)
        doc = ProductionDocument.model_validate_json(path.read_text(encoding="utf-8"))
        if doc.project_id != self.project_id:
            raise ValueError("Production document does not belong to this project")
        return doc

    def _check_project(self, conn):
        if not conn.execute("SELECT id FROM projects WHERE id=?", (self.project_id,)).fetchone():
            raise ValueError("Project does not exist")

    @staticmethod
    def _view(doc: ProductionDocument) -> dict:
        value = doc.model_dump(mode="json")
        value["world_hash"] = source_hash(value["world"])
        for shot in value["shots"]:
            authored = dict(shot)
            if authored.get("rough_board_id") is None:
                authored.pop("rough_board_id", None)  # Preserve pre-reference camera hashes.
            shot["source_hash"] = source_hash({"world": value["world"], "shot": authored})
        hashes = {s["clip_id"]: s["source_hash"] for s in value["shots"]}
        for board in value["boards"]:
            board["stale"] = bool(
                board["source_shot_hash"]
                and board["source_shot_hash"] != hashes.get(board["clip_id"])
            )
        for take in value["video_takes"]:
            take["stale"] = take["source_shot_hash"] != hashes.get(take["clip_id"])
        return value

    def _sync_video_selection(self, doc, conn):
        """Rebuild the clip-path projection from canonical production selections."""
        for take in doc.video_takes:
            if doc.selected_videos.get(str(take.clip_id)) == take.id:
                conn.execute(
                    "UPDATE clips SET clip_path=? WHERE id=? AND project_id=?",
                    (take.path, take.clip_id, self.project_id),
                )

    def read(self) -> dict:
        conn = get_db(settings.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._check_project(conn)
            doc = self._load()
            self._sync_video_selection(doc, conn)
            conn.commit()
            return self._view(doc)
        finally:
            conn.close()

    def _mutate(self, expected_revision: int, change: Callable) -> dict:
        conn = get_db(settings.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._check_project(conn)
            doc = self._load()
            if expected_revision != doc.revision:
                raise ValueError("Production changed; reload before applying this edit")
            change(doc, conn)
            doc.revision += 1
            value = doc.model_dump(mode="json")
            payload = json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False)
            revisions = self.root / "revisions"
            revisions.mkdir(parents=True, exist_ok=True)
            receipt = revisions / f"{source_hash(value)}.json"
            # A retry after an interrupted atomic replacement may encounter the
            # identical immutable receipt. Its name includes the entire payload.
            if not receipt.exists():
                atomic_write(receipt, payload)
            elif receipt.read_text(encoding="utf-8") != payload:
                raise ValueError("Production revision receipt is corrupt")
            atomic_write(self.root / "production.json", payload)
            self._sync_video_selection(doc, conn)
            conn.commit()
            return self._view(doc)
        finally:
            conn.close()

    def set_world(self, world: World, *, expected_revision: int) -> dict:
        def change(doc, conn):
            doc.world = world

        return self._mutate(expected_revision, change)

    def add_board(self, take: StoryboardTake, *, expected_revision: int) -> dict:
        def change(doc, conn):
            if not conn.execute(
                "SELECT id FROM clips WHERE id=? AND project_id=?", (take.clip_id, self.project_id)
            ).fetchone():
                raise ValueError("Clip does not belong to this project")
            path = Path(take.path).resolve()
            root = (settings.assets_dir / str(self.project_id)).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError("Board image must belong to this project assets directory")
            if path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
                raise ValueError("Board must be a PNG, JPEG or WebP image")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != take.sha256:
                raise ValueError("Board image changed before it could be saved")
            if any(b.id == take.id for b in doc.boards):
                raise ValueError("Board ID already exists")
            parent = next((b for b in doc.boards if b.id == take.source_board_id), None)
            if take.source_board_id and (not parent or parent.clip_id != take.clip_id):
                raise ValueError("Source board does not belong to this shot")
            if take.stage == "styled" and (not parent or parent.stage != "refined"):
                raise ValueError("A styled board requires a refined source board")
            if take.stage == "refined" and not take.previs_job_id:
                raise ValueError("A refined board requires a recorded Blender frame")
            doc.boards.append(take)

        return self._mutate(expected_revision, change)

    def select_board(self, board_id: str, *, expected_revision: int) -> dict:
        def change(doc, conn):
            board = next((b for b in doc.boards if b.id == board_id), None)
            if not board:
                raise ValueError("Board does not belong to this project")
            if hashlib.sha256(Path(board.path).read_bytes()).hexdigest() != board.sha256:
                raise ValueError("Board image has changed; restore the recorded file")
            hashes = {s["clip_id"]: s["source_hash"] for s in self._view(doc)["shots"]}
            if board.source_shot_hash and board.source_shot_hash != hashes.get(board.clip_id):
                raise ValueError("Board uses an earlier camera or world; render a current take")
            doc.selected_boards[f"{board.clip_id}:{board.stage}"] = board.id

        return self._mutate(expected_revision, change)

    def select_video_output(
        self, generation_job_id: int, output_index: int, *, expected_revision: int
    ) -> dict:
        def change(doc, conn):
            row = conn.execute(
                "SELECT * FROM jobs WHERE id=? AND project_id=?",
                (generation_job_id, self.project_id),
            ).fetchone()
            if not row or row["status"] != "done" or row["kind"] != "video":
                raise ValueError("Choose a completed video job in this project")
            target = json.loads(row["payload_json"]).get("production_video_target")
            if not target or target["clip_id"] != row["clip_id"]:
                raise ValueError("Video job has no production shot provenance")
            hashes = {s["clip_id"]: s["source_hash"] for s in self._view(doc)["shots"]}
            if target["source_shot_hash"] != hashes.get(row["clip_id"]):
                raise ValueError("Video uses an earlier camera or world")
            paths = json.loads(row["output_paths_json"])
            if not 0 <= output_index < len(paths):
                raise ValueError("Invalid video output index")
            path = Path(paths[output_index]).resolve()
            root = (settings.assets_dir / str(self.project_id)).resolve()
            if (
                not path.is_relative_to(root)
                or not path.is_file()
                or path.suffix.lower() not in (".mp4", ".webm", ".mov")
            ):
                raise ValueError("Video output must be an existing project video")
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            take_id = f"{generation_job_id}:{output_index}"
            existing = next((v for v in doc.video_takes if v.id == take_id), None)
            if existing and existing.sha256 != digest:
                raise ValueError("Selected video file has changed")
            if not existing:
                doc.video_takes.append(
                    ProductionVideoTake(
                        id=take_id,
                        generation_job_id=generation_job_id,
                        output_index=output_index,
                        path=str(path),
                        sha256=digest,
                        **target,
                    )
                )
            doc.selected_videos[str(row["clip_id"])] = take_id

        return self._mutate(expected_revision, change)

    def set_camera(
        self,
        clip_id: int,
        camera: Camera,
        *,
        expected_revision: int,
        end_camera: Camera | None = None,
        frame_end: int | None = None,
        rough_board_id: str | None = None,
    ) -> dict:
        def change(doc, conn):
            row = conn.execute(
                "SELECT duration_sec FROM clips WHERE id=? AND project_id=?",
                (clip_id, self.project_id),
            ).fetchone()
            if not row:
                raise ValueError("Clip does not belong to this project")
            shot = next((s for s in doc.shots if s.clip_id == clip_id), None)
            reference_id = (
                (rough_board_id or None)
                if rough_board_id is not None
                else (shot.rough_board_id if shot else None)
            )
            if reference_id:
                board = next((b for b in doc.boards if b.id == reference_id), None)
                if not board or board.clip_id != clip_id or board.stage != "rough":
                    raise ValueError("Camera reference must be a rough board from this shot")
            length = frame_end or (
                shot.frame_end
                if shot
                else max(1, round((row["duration_sec"] or 10) * doc.world.fps))
            )
            replacement = ProductionShot(
                clip_id=clip_id,
                camera=camera,
                end_camera=end_camera,
                frame_end=length,
                rough_board_id=reference_id,
            )
            doc.shots = [replacement if s.clip_id == clip_id else s for s in doc.shots]
            if shot is None:
                doc.shots.append(replacement)

        return self._mutate(expected_revision, change)
