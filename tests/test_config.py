import logging
import logging.handlers

import pytest

import config


def test_env_int_reads_the_current_environment(monkeypatch):
    monkeypatch.setenv("X_COUNT", "7")
    assert config.env_int("X_COUNT", 3) == 7


def test_env_int_defaults_when_unset_or_empty(monkeypatch):
    monkeypatch.delenv("X_COUNT", raising=False)
    assert config.env_int("X_COUNT", 3) == 3
    monkeypatch.setenv("X_COUNT", "")
    assert config.env_int("X_COUNT", 3) == 3


def test_env_int_falls_back_on_garbage_instead_of_raising(monkeypatch, caplog):
    monkeypatch.setenv("X_COUNT", "four")
    assert config.env_int("X_COUNT", 3) == 3
    assert "X_COUNT" in caplog.text


# --- logging ---------------------------------------------------------------------

@pytest.fixture
def bare_root_logger():
    """pytest installs handlers on the root logger; configure_logging() leaves a
    logger that already has some alone, so take them off for the test."""
    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    yield root
    for handler in root.handlers:
        handler.close()
    root.handlers = saved
    root.setLevel(level)


def test_logging_goes_to_a_rotating_file(bare_root_logger, tmp_path, monkeypatch):
    bare_root_logger.handlers.clear()
    target = tmp_path / "logs" / "watcher.log"
    monkeypatch.setenv("LOG_FILE", str(target))

    config.configure_logging()
    logging.getLogger("x").info("hello from the test")
    for handler in bare_root_logger.handlers:
        handler.flush()

    kinds = {type(h) for h in bare_root_logger.handlers}
    assert logging.handlers.RotatingFileHandler in kinds
    assert "hello from the test" in target.read_text(encoding="utf-8")


def test_logging_file_can_be_turned_off(bare_root_logger, tmp_path, monkeypatch):
    bare_root_logger.handlers.clear()
    monkeypatch.setenv("LOG_FILE", "off")

    config.configure_logging()

    assert not any(isinstance(h, logging.handlers.RotatingFileHandler)
                   for h in bare_root_logger.handlers)


def test_an_unwritable_log_file_falls_back_to_stderr(bare_root_logger, tmp_path, monkeypatch):
    bare_root_logger.handlers.clear()
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    monkeypatch.setenv("LOG_FILE", str(blocker / "watcher.log"))

    config.configure_logging()  # must not raise

    assert bare_root_logger.handlers


def test_existing_handlers_are_left_alone():
    before = logging.getLogger().handlers[:]
    config.configure_logging()
    assert logging.getLogger().handlers == before
