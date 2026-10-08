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
