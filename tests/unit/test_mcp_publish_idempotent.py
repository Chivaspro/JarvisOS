"""``publish()`` registers each MCP tool once per active session (audit P2-F).

At boot every server's connect calls ``bridge.publish()``, which walks *all*
live sessions. Before the fix, every earlier server's tools were re-registered
on each new connect — an O(n²) set of registrations and a repeated
"Plugin hot-registered" log line per tool per connect.

These tests pin the corrected contract:

* publishing an unchanged session is idempotent (installs nothing new), and
* a **reconnect** rebinds the tool, because a reconnect builds a *new*
  ``McpSession`` object — so its runner closure must be refreshed.

The session is faked here so the assertion is about the publish policy alone;
the real end-to-end path (a spawned server whose tools are callable through the
registry) stays covered by ``test_mcp_host.py``.
"""
from __future__ import annotations

from app import plugin_loader
from mcp_host import bridge as bridge_module
from mcp_host import catalog


class _FakeTool:
    def __init__(self, name: str, description: str = "reads a thing") -> None:
        self.name = name
        self.description = description
        self.input_schema = {"type": "object", "properties": {}}


class _FakeSession:
    def __init__(self, tools) -> None:
        self.running = True
        self.tools = list(tools)


def _bridge_with(session) -> bridge_module.Bridge:
    spec = catalog.ServerSpec(name="dup", command=["true"], prefix="dup")
    instance = bridge_module.Bridge([spec])
    instance._sessions["dup"] = session
    return instance


def _registry(monkeypatch):
    registry = plugin_loader.PluginRegistry({}, lambda message: None)
    monkeypatch.setattr(plugin_loader, "_RUNTIME_REGISTRY", registry)
    return registry


def test_publish_is_idempotent_for_one_session(monkeypatch):
    registry = _registry(monkeypatch)
    instance = _bridge_with(_FakeSession([_FakeTool("a"), _FakeTool("b")]))

    assert instance.publish() == 2          # first publish installs both
    assert instance.publish() == 0          # same session → nothing new
    assert registry.has("dup__a") and registry.has("dup__b")


def test_a_reconnect_rebinds_the_runner(monkeypatch):
    registry = _registry(monkeypatch)
    instance = _bridge_with(_FakeSession([_FakeTool("a")]))

    assert instance.publish() == 1
    # A reconnect replaces the session object with a fresh one; publishing must
    # re-register so the tool's runner points at the live session.
    instance._sessions["dup"] = _FakeSession([_FakeTool("a")])
    assert instance.publish() == 1
    assert registry.has("dup__a")
