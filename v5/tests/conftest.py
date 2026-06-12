"""v5 test fixtures: import path + per-test alias-store isolation."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ewsmcp.ids import reset_aliaser_cache  # noqa: E402
from ewsmcp.tools.base import reset_send_rate_window  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_stores(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    reset_aliaser_cache()
    reset_send_rate_window()
    yield
    reset_aliaser_cache()
    reset_send_rate_window()


def make_settings(**overrides):
    from ewsmcp.config import Settings
    base = dict(
        ews_server_url="https://mail.corp.example/EWS/Exchange.asmx",
        ews_email="exec@corp.example",
        ews_username="svc",
        ews_password="pw",
        mcp_transport="stdio",
        data_dir="data-test",
    )
    base.update(overrides)
    return Settings(**base)
