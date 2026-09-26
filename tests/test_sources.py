"""Contract tests for provenance/sources.json and the Pi-first adapter seam (HX-005)."""

from __future__ import annotations

import json
from pathlib import Path

from labrun.adapters.agents import (
    AgentAdapter,
    DeterministicMockAdapter,
    ProposedAction,
    RenderedContext,
    ToolSpec,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCES_PATH = REPO_ROOT / "provenance" / "sources.json"

REQUIRED_ENTRY_KEYS = {"id", "name", "url", "kind", "revision", "integrity", "status"}
VALID_KINDS = {"repo", "npm", "pypi", "paper", "standard"}
VALID_STATUSES = {"pinned", "unresolved"}


def _load_sources() -> dict:
    with SOURCES_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def test_sources_file_parses_and_has_expected_shape():
    data = _load_sources()
    assert data["format"] == "labrun.sources/1"
    assert isinstance(data["retrieved"], str) and data["retrieved"]
    assert isinstance(data["sources"], list) and data["sources"]


def test_every_entry_has_required_keys_and_valid_kind():
    data = _load_sources()
    for entry in data["sources"]:
        missing = REQUIRED_ENTRY_KEYS - entry.keys()
        assert not missing, f"{entry.get('id')} missing keys: {missing}"
        assert entry["kind"] in VALID_KINDS, f"{entry['id']} has unknown kind {entry['kind']!r}"
        assert entry["status"] in VALID_STATUSES, (
            f"{entry['id']} has unknown status {entry['status']!r}"
        )
        assert entry["url"].startswith("http"), f"{entry['id']} url is not a URL"


def test_every_pin_has_a_revision_or_an_explicit_unresolved_reason():
    data = _load_sources()
    for entry in data["sources"]:
        if entry["status"] == "pinned":
            assert entry["revision"], f"{entry['id']} is pinned but has no revision"
        else:
            assert entry["revision"] is None, f"{entry['id']} is unresolved but carries a revision"
            reason = entry.get("reason")
            assert isinstance(reason, str) and reason.strip(), (
                f"{entry['id']} is unresolved but has no explicit reason"
            )


def test_ids_are_unique():
    data = _load_sources()
    ids = [entry["id"] for entry in data["sources"]]
    assert len(ids) == len(set(ids)), "duplicate source ids in sources.json"


def test_includes_s13_sol_pi_pinned_to_a_commit():
    data = _load_sources()
    by_id = {entry["id"]: entry for entry in data["sources"]}
    assert "S13" in by_id
    s13 = by_id["S13"]
    assert s13["status"] == "pinned"
    assert s13["kind"] == "repo"
    assert "NVlabs/SoL-Pi" in s13["url"]
    # A git commit: 40 hex characters.
    assert len(s13["revision"]) == 40
    assert all(c in "0123456789abcdef" for c in s13["revision"])


def test_includes_the_pi_npm_pin_with_integrity():
    data = _load_sources()
    pi_entries = [
        e for e in data["sources"] if e["kind"] == "npm" and "pi-coding-agent" in e["url"]
    ]
    assert pi_entries, "no npm source entry pins the Pi coding agent package"
    pi_entry = pi_entries[0]
    assert pi_entry["status"] == "pinned"
    assert pi_entry["revision"] == "0.85.1"
    assert pi_entry["integrity"], "Pi npm pin must carry an integrity hash"
    assert pi_entry["integrity"].startswith("sha512-")


def test_no_source_claims_pinned_without_a_revision():
    data = _load_sources()
    for entry in data["sources"]:
        if entry["revision"] is not None:
            assert entry["status"] == "pinned", (
                f"{entry['id']} has a revision but isn't marked pinned"
            )


# --- Adapter seam ---------------------------------------------------------


def test_agent_adapter_is_runtime_checkable_protocol():
    assert isinstance(AgentAdapter, type)  # Protocol classes are still classes
    assert hasattr(AgentAdapter, "__protocol_attrs__") or getattr(
        AgentAdapter, "_is_protocol", False
    )


def test_deterministic_mock_adapter_satisfies_the_protocol():
    adapter = DeterministicMockAdapter()
    assert isinstance(adapter, AgentAdapter)
    assert adapter.name and adapter.version


def test_mock_adapter_render_context_is_deterministic():
    adapter = DeterministicMockAdapter()
    state = {"checkpoint_id": "chk-1", "step": 3, "notes": ["a", "b"]}
    first = adapter.render_context(state)
    second = adapter.render_context(dict(state))
    assert isinstance(first, RenderedContext)
    assert first.rendered_digest == second.rendered_digest
    assert first.messages == second.messages
    assert first.source_checkpoint == "chk-1"


def test_mock_adapter_render_context_changes_with_state():
    adapter = DeterministicMockAdapter()
    a = adapter.render_context({"checkpoint_id": "chk-1", "step": 1})
    b = adapter.render_context({"checkpoint_id": "chk-1", "step": 2})
    assert a.rendered_digest != b.rendered_digest


def test_mock_adapter_propose_action_and_capture_are_deterministic():
    adapter = DeterministicMockAdapter()
    rendered = adapter.render_context({"checkpoint_id": "chk-1"})
    tools = [ToolSpec(name="validate_input", schema={"type": "object"})]

    action1 = adapter.propose_action(rendered, tools)
    action2 = adapter.propose_action(rendered, tools)
    assert isinstance(action1, ProposedAction)
    assert action1 == action2
    assert action1.tool_name == "validate_input"

    call1 = adapter.capture(action1)
    call2 = adapter.capture(action2)
    assert call1.model_id == call2.model_id == "deterministic-mock/0.1.0"
    assert call1.request_body == call2.request_body
    assert call1.response == call2.response
    assert call1.usage.simulated is True
    assert call1.usage.measured is True


def test_mock_adapter_propose_action_defaults_to_noop_without_tools():
    adapter = DeterministicMockAdapter()
    rendered = adapter.render_context({"checkpoint_id": "chk-2"})
    action = adapter.propose_action(rendered, [])
    assert action.tool_name == "noop"


def test_object_missing_methods_does_not_satisfy_protocol():
    class NameAndVersionOnly:
        name = "x"
        version = "1"

    assert not isinstance(NameAndVersionOnly(), AgentAdapter)


def test_object_missing_name_does_not_satisfy_protocol():
    class NoName:
        version = "1"

        def render_context(self, checkpoint_state):
            raise NotImplementedError

        def propose_action(self, rendered, tools):
            raise NotImplementedError

        def capture(self, call):
            raise NotImplementedError

    assert not isinstance(NoName(), AgentAdapter)
