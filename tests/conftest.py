import pytest


@pytest.fixture(autouse=True)
def _isolated_history(tmp_path, monkeypatch):
    """Never touch the real ~/.leadscraper/history.json from tests."""
    monkeypatch.setenv("LEADSCRAPER_HISTORY", str(tmp_path / "history.json"))
