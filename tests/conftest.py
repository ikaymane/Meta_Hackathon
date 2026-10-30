import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Keep usage logs and sessions out of the real ~/.focusfreeze."""
    monkeypatch.setenv("FF_HOME", str(tmp_path / "ff"))
    monkeypatch.delenv("FF_COST_CAP_USD", raising=False)
    monkeypatch.setenv("FF_SYNTHETIC_DATA", "1")  # fixtures are made up
    monkeypatch.delenv("FF_PAID_BALANCE", raising=False)
    from freezer import cost
    monkeypatch.setattr(cost, "_log_failed", False)
    monkeypatch.setattr(cost, "_pending", [])
    return tmp_path / "ff"
