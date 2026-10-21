from pathlib import Path

import pytest

from classbot.config import Settings
from classbot.db import Database


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(
        _env_file=None,
        materials_dir=tmp_path / "materials",
        data_dir=tmp_path / "data",
        system_prompt_file=Path(__file__).resolve().parents[1] / "config" / "system_prompt.md",
        pricing_file=Path(__file__).resolve().parents[1] / "config" / "pricing.toml",
        slack_channel_id="C123",
        slack_admin_user_ids=["UADMIN"],
        claude_model="claude-opus-5-5",
        file_stable_sec=0,
    )
    s.ensure_dirs()
    return s


@pytest.fixture
def db(settings: Settings) -> Database:
    d = Database(settings.db_path)
    d.init_schema()
    yield d
    d.close()
