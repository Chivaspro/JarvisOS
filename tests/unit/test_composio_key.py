"""Composio key resolution: environment first, then the guarded config file.

The module once read env vars literally named ``ENV_API_KEY`` and
``CONFIG_API_KEY``, so both lookups defaulted to "" and ``api_key_present()``
was False no matter what the user saved — every connector verb reported
"not configured" while the Settings window showed the key as saved.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connectors import composio_client as cc  # noqa: E402


@pytest.fixture()
def bare(monkeypatch):
    """No env key; store redirected to a dict the test controls."""
    monkeypatch.delenv(cc.ENV_API_KEY, raising=False)
    store: dict = {}
    monkeypatch.setattr(cc, "_config",
                        lambda key, default="": store.get(key, default))
    return store


def test_a_key_stored_in_the_config_is_found(bare):
    bare["composio_api_key"] = "ak_live_1234567890"
    assert cc.api_key_present() is True
    assert cc._api_key() == "ak_live_1234567890"


def test_the_environment_key_wins(bare, monkeypatch):
    bare["composio_api_key"] = "ak_stored_1234567890"
    monkeypatch.setenv(cc.ENV_API_KEY, "ak_env_1234567890")
    assert cc._api_key() == "ak_env_1234567890"


def test_the_env_var_name_has_the_real_default():
    """Guard the regression itself: the module's lookup names must be the
    documented COMPOSIO_API_KEY / composio_api_key, not the old placeholders."""
    source = (ROOT / "connectors" / "composio_client.py").read_text(encoding="utf-8")
    assert 'os.getenv("ENV_API_KEY", "COMPOSIO_API_KEY")' in source
    assert 'os.getenv("CONFIG_API_KEY", "composio_api_key")' in source


def test_a_placeholder_is_not_a_key(bare):
    bare["composio_api_key"] = "your-composio-api-key-here"
    assert cc.api_key_present() is False


def test_a_pasted_sentence_is_not_a_key(bare):
    bare["composio_api_key"] = "paste your key from the dashboard here"
    assert cc.api_key_present() is False


def test_no_key_anywhere_reports_absence(bare):
    assert cc.api_key_present() is False
    assert cc._api_key() == ""
