"""Shared pytest setup."""
import pytest

import db


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path, monkeypatch):
    """Every test gets its own empty database, so none can touch the real one
    in data/, and none of a developer's .env can steer the ones that rely on
    defaults."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.delenv("HEALTHCHECK_URL", raising=False)
    # create_app()/configure_logging() must not drop a log file into data/.
    monkeypatch.setenv("LOG_FILE", "off")
