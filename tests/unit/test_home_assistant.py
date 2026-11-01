"""Home Assistant: a fixed, small vocabulary — never a remote control for a house.

A generic Home Assistant client can unlock the front door and disarm the alarm,
so these tests pin the policy, not the plumbing:

* control is limited to lights, switches, fans and input booleans (plus scenes
  and scripts), and locks/covers/cameras/alarms are refused BY NAME;
* the service path is built from a validated domain and one of the module's own
  service names, so no argument can become ``lock/unlock``;
* every control is read back; a request that did not change the entity is
  reported as unverified, never as success;
* the token travels in the Authorization header only — never in a URL, a result
  sentence or an error.
"""

from __future__ import annotations

import json

import pytest

from capabilities import home_assistant

TOKEN = "eyJhbGciOiJIUzI1NiJ9.this-is-a-long-lived-token"


class _Response:
    def __init__(self, status: int = 200, payload=None, headers=None, raw=None):
        self.status_code = status
        self._payload = payload
        self.headers = dict(headers or {})
        if raw is not None:
            self.content = raw
        elif payload is None:
            self.content = b""
        else:
            self.content = json.dumps(payload).encode("utf-8")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _Instance:
    """A tiny in-memory Home Assistant whose commands really change state."""

    def __init__(self):
        self.states = {
            "light.office": {"entity_id": "light.office", "state": "off",
                             "attributes": {"friendly_name": "Office Lamp",
                                            "brightness": 0,
                                            "access_token": "HIDDEN"}},
            "switch.desk": {"entity_id": "switch.desk", "state": "off",
                            "attributes": {"friendly_name": "Desk Switch"}},
            "lock.front": {"entity_id": "lock.front", "state": "locked",
                           "attributes": {"friendly_name": "Front Door"}},
            "cover.garage": {"entity_id": "cover.garage", "state": "closed",
                             "attributes": {"friendly_name": "Garage"}},
            "scene.movie": {"entity_id": "scene.movie", "state": "unknown",
                            "attributes": {"friendly_name": "Movie Scene"}},
            "script.goodnight": {"entity_id": "script.goodnight",
                                 "state": "off",
                                 "attributes": {"friendly_name": "Goodnight"}},
        }
        self.posts: list[str] = []
        self.gets: list[str] = []
        self.headers: list[dict] = []
        self.ignore_commands = False

    def get(self, url, **kwargs):
        self.gets.append(url)
        self.headers.append(dict(kwargs.get("headers") or {}))
        assert kwargs.get("allow_redirects") is False, "redirects must be refused"
        if url.endswith("/api/"):
            return _Response(payload={"message": "API running."})
        if url.endswith("/api/config"):
            return _Response(payload={"version": "2026.10", "location_name": "Home",
                                      "time_zone": "Europe/London"})
        if url.endswith("/api/states"):
            return _Response(payload=list(self.states.values()))
        for entity_id, state in self.states.items():
            if url.endswith("/api/states/" + entity_id):
                return _Response(payload=state)
        return _Response(status=404, payload={"message": "Entity not found."})

    def post(self, url, **kwargs):
        self.posts.append(url)
        self.headers.append(dict(kwargs.get("headers") or {}))
        entity_id = (kwargs.get("json") or {}).get("entity_id", "")
        service = url.rsplit("/", 1)[-1]
        state = self.states.get(entity_id)
        if state is None:
            return _Response(status=404, payload={"message": "no such entity"})
        if not self.ignore_commands:
            if service == "turn_on":
                state["state"] = "on"
                if "brightness_pct" in (kwargs.get("json") or {}):
                    state["attributes"]["brightness"] = round(
                        kwargs["json"]["brightness_pct"] * 255 / 100)
            elif service == "turn_off":
                state["state"] = "off"
            elif service == "toggle":
                state["state"] = "off" if state["state"] == "on" else "on"
        return _Response(payload=[])


@pytest.fixture()
def instance(monkeypatch):
    import requests

    fake = _Instance()
    monkeypatch.setattr(home_assistant, "_config",
                        lambda: {"url": "http://homeassistant.local:8123",
                                 "token": TOKEN, "verify": False})
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setattr(requests, "post", fake.post)
    return fake


# ── read paths ────────────────────────────────────────────────────────────────

def test_status_proves_the_instance_and_the_token(instance):
    result = home_assistant.run("status", {})
    assert result["ok"], result["detail"]
    assert "2026.10" in result["detail"]
    assert "Home" in result["detail"]
    assert result["evidence"]["url"] == "http://homeassistant.local:8123"


def test_entities_lists_states_and_marks_what_is_read_only(instance):
    result = home_assistant.run("entities", {})
    assert result["ok"]
    assert "Office Lamp" in result["detail"]
    assert "[read-only]" in result["detail"]
    # Locks and covers are readable — the user owns the instance — but their
    # line must say the tool will not control them.
    assert "Front Door" in result["detail"]

    filtered = home_assistant.run("entities", {"domain": "light"})
    assert filtered["ok"] and filtered["evidence"]["count"] == 1


def test_state_returns_a_safe_attribute_subset_only(instance):
    result = home_assistant.run("state", {"entity": "light.office"})
    assert result["ok"]
    assert "Office Lamp" in result["detail"]
    assert result["evidence"]["controllable"] is True
    # ``access_token`` is not in the safe list, so it can never leak through a
    # state answer even when an integration stores one as an attribute.
    assert "access_token" not in result["evidence"]["attributes"]
    assert "HIDDEN" not in json.dumps(result)


def test_an_ambiguous_friendly_name_lists_the_candidates(instance):
    instance.states["light.office"]["attributes"]["friendly_name"] = "Shared Name"
    instance.states["switch.desk"]["attributes"]["friendly_name"] = "Shared Name"
    result = home_assistant.run("turn_on", {"entity": "Shared Name"})
    assert result["ok"] is False
    assert result["reason"] == "ambiguous_entity"
    assert "light.office" in result["detail"]
    assert "switch.desk" in result["detail"]


def test_an_unknown_name_says_what_was_searched(instance):
    result = home_assistant.run("state", {"entity": "Nonexistent Thing"})
    assert result["ok"] is False
    assert result["reason"] == "not_found"
    assert "Nonexistent Thing" in result["detail"]


# ── control paths ─────────────────────────────────────────────────────────────

def test_turn_on_reads_back_and_reports_the_real_state(instance):
    result = home_assistant.run("turn_on", {"entity": "Office Lamp"})
    assert result["ok"], result["detail"]
    assert instance.states["light.office"]["state"] == "on"
    assert result["evidence"]["verified"] is True
    assert "is now on" in result["detail"]


def test_set_brightness_verifies_the_reported_brightness(instance):
    result = home_assistant.run("set_brightness",
                                {"entity": "light.office", "brightness_pct": 40})
    assert result["ok"], result["detail"]
    assert result["evidence"]["brightness_pct"] == 40
    assert instance.states["light.office"]["attributes"]["brightness"] == 102


def test_a_command_that_did_not_change_anything_is_unverified(instance):
    instance.ignore_commands = True
    result = home_assistant.run("turn_on", {"entity": "light.office"})
    assert result["ok"] is False
    assert result["reason"] == "state_mismatch"
    assert "not confirmed" in result["detail"]


@pytest.mark.parametrize("entity", ["lock.front", "cover.garage"])
def test_locks_and_covers_are_refused_by_name(instance, entity):
    result = home_assistant.run("turn_off", {"entity": entity})
    assert result["ok"] is False
    assert result["reason"] == "blocked_domain"
    assert "deliberately not controllable" in result["detail"]
    assert instance.posts == [], "nothing may be sent for a blocked domain"


def test_brightness_on_a_switch_is_refused(instance):
    result = home_assistant.run("set_brightness",
                                {"entity": "switch.desk", "brightness_pct": 50})
    assert result["ok"] is False
    assert result["reason"] == "blocked_domain"
    assert instance.posts == []


def test_a_scene_is_activated_through_its_own_action_only(instance):
    result = home_assistant.run("activate_scene", {"entity": "scene.movie"})
    assert result["ok"], result["detail"]
    assert instance.posts[-1].endswith("/api/services/scene/turn_on")

    wrong = home_assistant.run("turn_on", {"entity": "scene.movie"})
    assert wrong["ok"] is False
    assert wrong["reason"] == "blocked_domain"


def test_there_is_no_arbitrary_service_call(instance):
    for action in ("call_service", "service", "unlock"):
        result = home_assistant.run(action, {"entity": "lock.front"})
        assert result["ok"] is False
        assert result["reason"] == "unknown_action"
    assert instance.posts == []


def test_the_service_path_is_built_only_from_validated_values(instance):
    # ``entity`` is the only argument that reaches the URL, and it must match
    # the entity-id shape — a path-like value is rejected before any request.
    result = home_assistant.run("turn_on", {"entity": "lock.front/../../unlock"})
    assert result["ok"] is False
    assert instance.posts == []


def test_a_bad_brightness_is_refused_before_anything_is_sent(instance):
    result = home_assistant.run("set_brightness",
                                {"entity": "light.office", "brightness_pct": "soon"})
    assert result["ok"] is False
    assert result["reason"] == "bad_brightness"
    assert instance.posts == []


# ── configuration and token handling ─────────────────────────────────────────

def test_the_token_is_only_ever_in_the_authorization_header(instance):
    home_assistant.run("turn_on", {"entity": "light.office"})
    assert instance.headers, "requests were made"
    for headers in instance.headers:
        assert headers.get("Authorization") == f"Bearer {TOKEN}"
    for url in instance.gets + instance.posts:
        assert TOKEN not in url
    result = home_assistant.run("status", {})
    assert TOKEN not in json.dumps(result)


def test_without_a_url_or_token_nothing_is_sent(monkeypatch):
    import requests

    monkeypatch.setattr(home_assistant, "_config",
                        lambda: {"url": "", "token": "", "verify": True})
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: pytest.fail("no request may go out"))
    result = home_assistant.run("status", {})
    assert result["ok"] is False
    assert result["reason"] == "not_configured"
    assert "Nothing was sent anywhere" in result["detail"]


def test_a_token_with_spaces_is_treated_as_pasted_text(monkeypatch):
    import requests

    monkeypatch.setattr(home_assistant, "_config",
                        lambda: {"url": "http://ha.local:8123",
                                 "token": "two words", "verify": True})
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: pytest.fail("no request may go out"))
    result = home_assistant.run("status", {})
    assert result["ok"] is False
    assert result["reason"] == "bad_config"


def test_an_unexpected_url_shape_is_refused(monkeypatch):
    import requests

    monkeypatch.setattr(home_assistant, "_config",
                        lambda: {"url": "homeassistant.local:8123",
                                 "token": TOKEN, "verify": True})
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: pytest.fail("no request may go out"))
    result = home_assistant.run("status", {})
    assert result["ok"] is False
    assert result["reason"] == "bad_config"
    assert "http://" in result["detail"]


def test_an_auth_failure_names_http_401_not_the_token(instance, monkeypatch):
    import requests

    def rejected(url, **kwargs):
        return _Response(status=401, payload={"message": "Unauthorized"})

    monkeypatch.setattr(requests, "get", rejected)
    result = home_assistant.run("status", {})
    assert result["ok"] is False
    assert result["reason"] == "auth_failed"
    assert "401" in result["detail"]
    assert TOKEN not in result["detail"]
