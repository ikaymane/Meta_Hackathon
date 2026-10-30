import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Keep usage logs and sessions out of the real ~/.focusfreeze."""
    monkeypatch.setenv("FF_HOME", str(tmp_path / "ff"))
    monkeypatch.delenv("FF_COST_CAP_USD", raising=False)
    from freezer import cost
    monkeypatch.setattr(cost, "_log_failed", False)
    return tmp_path / "ff"
