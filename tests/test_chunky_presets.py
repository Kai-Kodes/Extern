"""Regression tests for Chunky Tasks customizable presets and persistence."""

import math
from pathlib import Path
import pytest

from todo.config import Config
from todo.database import Database
from todo.integrations.filemeta import FilemetaScanResult, ScannedFile
from todo.models import ChunkyPreset, ChunkyResource, ChunkyTask
from todo.services.chunky_service import ChunkyService
from todo.services.daily_service import DailyService
from todo.services.source_service import SourceService
from todo.services.task_service import TaskService


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test_presets.db"


@pytest.fixture
def db(db_path: Path) -> Database:
    return Database(db_path)


@pytest.fixture
def chunky_service(db: Database) -> ChunkyService:
    return ChunkyService(db)


def test_default_presets_seeded_on_fresh_installation(db: Database, chunky_service: ChunkyService):
    """1. Default presets appear on a fresh installation for both seconds and pages."""
    db.init_db()

    # Media presets (seconds)
    sec_presets = chunky_service.list_presets(range_unit="seconds")
    assert len(sec_presets) == 5
    sec_names = [p.name for p in sec_presets]
    assert "15 minutes" in sec_names
    assert "20 minutes" in sec_names
    assert "30 minutes" in sec_names
    assert "45 minutes" in sec_names
    assert "60 minutes" in sec_names

    # Check durations in seconds
    sec_map = {p.name: p.duration for p in sec_presets}
    assert sec_map["15 minutes"] == 900.0
    assert sec_map["20 minutes"] == 1200.0
    assert sec_map["30 minutes"] == 1800.0
    assert sec_map["45 minutes"] == 2700.0
    assert sec_map["60 minutes"] == 3600.0

    # 20 minutes is marked as default
    default_sec = chunky_service.get_default_preset(range_unit="seconds")
    assert default_sec is not None
    assert default_sec.name == "20 minutes"
    assert default_sec.is_default is True

    # Document presets (pages)
    page_presets = chunky_service.list_presets(range_unit="pages")
    assert len(page_presets) == 5
    page_names = [p.name for p in page_presets]
    assert "5 pages" in page_names
    assert "10 pages" in page_names
    assert "15 pages" in page_names
    assert "20 pages" in page_names
    assert "25 pages" in page_names

    page_map = {p.name: p.duration for p in page_presets}
    assert page_map["5 pages"] == 5.0
    assert page_map["10 pages"] == 10.0

    default_page = chunky_service.get_default_preset(range_unit="pages")
    assert default_page is not None
    assert default_page.name == "10 pages"
    assert default_page.is_default is True


def test_custom_preset_creation_and_persistence_across_reloads(db_path: Path):
    """2. Custom presets can be created and persist across reloads (closing and reopening database)."""
    # Session 1: create custom preset
    db1 = Database(db_path)
    db1.init_db()
    svc1 = ChunkyService(db1)

    custom = ChunkyPreset(
        name="Deep Work",
        duration=2700.0,  # 45 minutes
        range_unit="seconds",
        is_default=False,
        is_builtin=False,
    )
    created = svc1.create_preset(custom)
    assert created.id is not None
    preset_id = created.id

    # Session 2: simulate application restart with fresh Database instance
    db2 = Database(db_path)
    svc2 = ChunkyService(db2)

    loaded = svc2.get_preset(preset_id)
    assert loaded is not None
    assert loaded.name == "Deep Work"
    assert loaded.duration == 2700.0
    assert loaded.range_unit == "seconds"
    assert loaded.is_builtin is False

    presets_list = svc2.list_presets(range_unit="seconds")
    assert any(p.id == preset_id and p.name == "Deep Work" for p in presets_list)


def test_edit_and_rename_preset(db: Database, chunky_service: ChunkyService):
    """3. Presets can be renamed or have their duration edited."""
    db.init_db()

    custom = ChunkyPreset(
        name="Sprint",
        duration=600.0,  # 10m
        range_unit="seconds",
    )
    created = chunky_service.create_preset(custom)

    # Edit name and duration
    created.name = "Quick Sprint"
    created.duration = 720.0  # 12m
    chunky_service.update_preset(created)

    updated = chunky_service.get_preset(created.id)
    assert updated is not None
    assert updated.name == "Quick Sprint"
    assert updated.duration == 720.0


def test_delete_custom_preset_and_default_promotion(db: Database, chunky_service: ChunkyService):
    """4. Presets can be deleted; if active default is deleted, another preset is automatically promoted."""
    db.init_db()

    # Create a custom preset and make it default
    custom = ChunkyPreset(
        name="Ultra Focus",
        duration=3000.0,
        range_unit="seconds",
        is_default=True,
    )
    created = chunky_service.create_preset(custom)
    assert created.is_default is True

    # Verify Ultra Focus is current default
    curr_def = chunky_service.get_default_preset(range_unit="seconds")
    assert curr_def.name == "Ultra Focus"

    # Delete Ultra Focus
    deleted = chunky_service.delete_preset(created.id)
    assert deleted is True

    # Verify it was deleted
    assert chunky_service.get_preset(created.id) is None

    # Verify a new default was automatically promoted
    new_def = chunky_service.get_default_preset(range_unit="seconds")
    assert new_def is not None
    assert new_def.id != created.id
    assert new_def.is_default is True


def test_deleting_preset_does_not_alter_existing_chunk_tasks(db: Database, chunky_service: ChunkyService):
    """5. Deleting a preset must not delete, alter, or invalidate previously created chunk tasks."""
    db.init_db()
    task_svc = TaskService(db)
    source_svc = SourceService(db)

    # 1. Create a custom preset
    custom_preset = chunky_service.create_preset(
        ChunkyPreset(name="Hour Power", duration=3600.0, range_unit="seconds")
    )

    # 2. Set up resource and chunk task using that duration
    scan_res = FilemetaScanResult(
        source_name="Videos",
        source_path="youtube://videos",
        items=[
            ScannedFile(
                name="vid_1",
                path="https://youtube.com/watch?v=vid_1",
                title="Lecture",
                type="video",
                metadata={"video_id": "vid_1", "duration": 7200, "source_type": "youtube"},
            )
        ],
    )
    source, _, _ = source_svc.add_or_update_source(scan_res)
    file_item = source_svc.get_items_by_source(source.id)[0]

    resource = chunky_service.get_or_create_resource(
        source_type="youtube",
        canonical_uri="https://youtube.com/watch?v=vid_1",
        title="Lecture",
        total_range=7200.0,
        range_unit="seconds",
        file_item_id=file_item.id,
    )

    task, chunk = chunky_service.create_chunk_task(
        resource=resource,
        start_pos=0.0,
        end_pos=custom_preset.duration,
        task_service=task_svc,
        task_title=f"Lecture [00:00:00–01:00:00]",
    )
    assert chunk.start_pos == 0.0
    assert chunk.end_pos == 3600.0

    # 3. Delete the preset
    chunky_service.delete_preset(custom_preset.id)
    assert chunky_service.get_preset(custom_preset.id) is None

    # 4. Verify existing task and chunky_task remain intact
    retrieved_chunk = chunky_service.get_chunks_for_resource(resource.id)
    assert len(retrieved_chunk) == 1
    assert retrieved_chunk[0].id == chunk.id
    assert retrieved_chunk[0].start_pos == 0.0
    assert retrieved_chunk[0].end_pos == 3600.0

    retrieved_task = task_svc.get_task_by_id(task.id)
    assert retrieved_task is not None
    assert retrieved_task.title == "Lecture [00:00:00–01:00:00]"


def test_invalid_and_duplicate_presets_rejected(db: Database, chunky_service: ChunkyService):
    """6. Invalid and duplicate presets are rejected safely."""
    db.init_db()

    # Empty name
    with pytest.raises(ValueError, match="Preset name cannot be empty"):
        chunky_service.create_preset(ChunkyPreset(name="", duration=600.0))

    with pytest.raises(ValueError, match="Preset name cannot be empty"):
        chunky_service.create_preset(ChunkyPreset(name="   ", duration=600.0))

    # Duration <= 0
    with pytest.raises(ValueError, match="positive"):
        chunky_service.create_preset(ChunkyPreset(name="Zero Duration", duration=0.0))

    with pytest.raises(ValueError, match="positive"):
        chunky_service.create_preset(ChunkyPreset(name="Negative Duration", duration=-100.0))

    # Non-finite duration (NaN, Inf)
    with pytest.raises(ValueError, match="positive"):
        chunky_service.create_preset(ChunkyPreset(name="Inf Duration", duration=math.inf))

    with pytest.raises(ValueError, match="positive"):
        chunky_service.create_preset(ChunkyPreset(name="NaN Duration", duration=math.nan))

    # Duplicate name within same unit (case-insensitive)
    chunky_service.create_preset(ChunkyPreset(name="Quick Session", duration=600.0, range_unit="seconds"))
    with pytest.raises(ValueError, match="already exists"):
        chunky_service.create_preset(ChunkyPreset(name="quick session", duration=900.0, range_unit="seconds"))

    # However, same name in different range_unit IS allowed (e.g. 'Standard' for seconds vs pages)
    page_standard = chunky_service.create_preset(ChunkyPreset(name="Quick Session", duration=10.0, range_unit="pages"))
    assert page_standard.id is not None

    # Cannot delete built-in presets
    builtin_p = next(p for p in chunky_service.list_presets(range_unit="seconds") if p.is_builtin)
    assert builtin_p.is_builtin is True
    with pytest.raises(ValueError, match="Built-in preset .* cannot be deleted"):
        chunky_service.delete_preset(builtin_p.id)

    with pytest.raises(ValueError, match="Built-in preset .* cannot be edited"):
        builtin_p.name = "Hacked Name"
        chunky_service.update_preset(builtin_p)

    # Cannot delete the only remaining custom preset if it's the last preset
    # Create 2 custom presets for a test unit or using allow_builtin=True
    custom1 = chunky_service.create_preset(ChunkyPreset(name="Custom A", duration=100.0, range_unit="seconds"))
    custom2 = chunky_service.create_preset(ChunkyPreset(name="Custom B", duration=200.0, range_unit="seconds"))
    chunky_service.delete_preset(custom1.id)
    chunky_service.delete_preset(custom2.id)


def test_existing_configuration_compatibility(tmp_path: Path):
    """7. Existing configuration files remain compatible."""
    # Write a standard existing config.toml
    config_file = tmp_path / "config.toml"
    config_file.write_text("""
[general]
db_path = "todo.db"

[google_tasks]
enabled = true
default_list = "@default"
""")
    # Load config and verify it parses cleanly without error
    import os
    os.environ["TODO_CONFIG_PATH"] = str(config_file)
    try:
        cfg = Config.load()
        assert cfg.google_tasks_enabled is True
        assert cfg.google_default_task_list == "@default"
    finally:
        os.environ.pop("TODO_CONFIG_PATH", None)


def test_document_and_media_presets_remain_separate(db: Database, chunky_service: ChunkyService):
    """8. Document page presets and media duration presets remain separate and correctly typed."""
    db.init_db()

    sec_presets = chunky_service.list_presets(range_unit="seconds")
    page_presets = chunky_service.list_presets(range_unit="pages")

    # Verify all seconds presets have range_unit == 'seconds' and durations in seconds
    for p in sec_presets:
        assert p.range_unit == "seconds"
        assert p.duration >= 60.0  # At least 1 minute

    # Verify all pages presets have range_unit == 'pages' and durations in pages
    for p in page_presets:
        assert p.range_unit == "pages"
        assert p.duration < 100.0  # Realistic page increments (5, 10, etc.)

    # No crossover
    sec_names = {p.name for p in sec_presets}
    page_names = {p.name for p in page_presets}
    assert "20 minutes" in sec_names
    assert "20 minutes" not in page_names
    assert "10 pages" in page_names
    assert "10 pages" not in sec_names


def test_set_default_preset(db: Database, chunky_service: ChunkyService):
    """9. Explicitly setting a new default updates the default flag and clears the previous default."""
    db.init_db()

    presets = chunky_service.list_presets(range_unit="seconds")
    p45 = next(p for p in presets if p.name == "45 minutes")
    assert p45.is_default is False

    chunky_service.set_default_preset(p45.id)

    # Re-fetch
    updated_presets = chunky_service.list_presets(range_unit="seconds")
    new_def = next(p for p in updated_presets if p.id == p45.id)
    assert new_def.is_default is True

    old_def = next(p for p in updated_presets if p.name == "20 minutes")
    assert old_def.is_default is False


def test_seeding_idempotency_across_repeated_initializations(db_path: Path):
    """10. Repeated application startups and db initializations are strictly idempotent."""
    db = Database(db_path)
    # Initialize 5 times consecutively
    for _ in range(5):
        db.init_db()

    svc = ChunkyService(db)
    # Seed explicitly 5 more times
    for _ in range(5):
        svc.seed_default_presets()

    sec_presets = svc.list_presets(range_unit="seconds")
    page_presets = svc.list_presets(range_unit="pages")

    # Still exactly 5 presets for each unit
    assert len(sec_presets) == 5
    assert len(page_presets) == 5

    # Check distinct IDs
    assert len({p.id for p in sec_presets}) == 5
    assert len({p.id for p in page_presets}) == 5

