"""M4-001: the RLM and optimizer contract."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from pydantic import ValidationError

from onoda.ablation.contract import CAPABILITY_METRICS, EFFICIENCY_METRICS, Tolerances
from onoda.adapters.pi.contract import (
    COMPONENT_VALIDATORS,
    CTX_RLM_STATE,
    KERNEL_PROTOCOL,
    REQUIRED_COMPONENTS,
    SEED_CANDIDATE,
    SEED_PROVENANCE,
    VARIANTS,
    HarnessCandidate,
    KernelCall,
    KernelExecReply,
    KernelInit,
    RlmCell,
    RlmChildEnd,
    RlmChildRefused,
    RlmChildStart,
    RlmLimits,
    RlmState,
    Script,
    canonical_state_json,
    derive_child_key,
    parse_kernel_message,
    parse_spool_line,
)
from onoda.optimize.contract import CandidateRecord, GateSpec, SplitAssignment

H = "a" * 64

NEW_LINES = [
    {
        "seq": 3,
        "type": "rlm.cell",
        "tool_call_id": "c1",
        "cell_index": 0,
        "code": H,
        "output": H,
        "is_error": False,
        "truncated": False,
    },
    {
        "seq": 4,
        "type": "rlm.child.start",
        "tool_call_id": "c1",
        "child_index": 0,
        "depth": 1,
        "spool_subdir": "children/0000",
        "prompt": H,
        "max_requests": 5,
    },
    {
        "seq": 5,
        "type": "rlm.child.end",
        "child_index": 0,
        "exit_code": 0,
        "end_status": "settled",
        "requests": 2,
        "cost_usd": "0",
        "answer": H,
    },
    {
        "seq": 6,
        "type": "rlm.child.refused",
        "tool_call_id": "c1",
        "reason": "max-children",
        "detail": "4 children already spawned",
    },
    {"seq": 7, "type": "rlm.state", "tool_call_id": "c1", "name": "energies", "value": H},
]


@pytest.mark.parametrize("data", NEW_LINES, ids=lambda d: d["type"])
def test_every_new_spool_line_round_trips(data):
    line = parse_spool_line(data)
    assert type(line) in (RlmCell, RlmChildStart, RlmChildEnd, RlmChildRefused, RlmState)
    again = parse_spool_line(json.loads(line.model_dump_json(exclude_none=False)))
    assert again == line


def test_spool_lines_reject_bad_fields():
    bad_subdir = {**NEW_LINES[1], "spool_subdir": "../escape"}
    with pytest.raises(ValidationError):
        parse_spool_line(bad_subdir)
    with pytest.raises(ValidationError):
        parse_spool_line({**NEW_LINES[4], "name": "bad name/with slash"})
    with pytest.raises(ValidationError):
        parse_spool_line({**NEW_LINES[3], "reason": "because"})


def test_kernel_messages_parse_in_both_directions():
    init = parse_kernel_message(
        {"op": "init", "id": 0, "protocol": KERNEL_PROTOCOL, "helpers": "", "max_output_chars": 10}
    )
    assert isinstance(init, KernelInit)
    call = parse_kernel_message(
        {"op": "call", "id": 0, "fn": "rlm", "args": {"prompt": "p", "context": None}}
    )
    assert isinstance(call, KernelCall)
    reply = parse_kernel_message(
        {"op": "exec.reply", "id": 1, "stdout": "", "stderr": "", "error": None, "truncated": False}
    )
    assert isinstance(reply, KernelExecReply)
    with pytest.raises(ValidationError):
        parse_kernel_message({"op": "call", "id": 0, "fn": "eval", "args": {}})


def test_child_key_derivation_is_the_documented_hmac():
    key = bytes(range(32))
    expected = hmac.new(key, b"onoda.pi-rlm-child/0003", hashlib.sha256).digest()
    assert derive_child_key(key, 3) == expected
    assert derive_child_key(key, 3) != derive_child_key(key, 4)


def test_state_json_is_canonical_and_rejects_nan():
    assert canonical_state_json({"b": 1, "a": [1, "x"]}) == '{"a":[1,"x"],"b":1}'
    with pytest.raises(ValueError):
        canonical_state_json(float("nan"))


def test_script_children_default_empty_and_nest():
    old = Script.model_validate({"schema": "onoda.pi-script/1", "source": "m3", "turns": []})
    assert old.children == ()
    nested = Script(source="p", turns=(), children=(Script(source="c", turns=()),))
    back = Script.model_validate_json(nested.model_dump_json(by_alias=True))
    assert back.children[0].source == "c"


def test_s1r_is_no_longer_marked_unbuilt_and_gets_the_ipython_tool():
    assert VARIANTS["S1r"].rlm
    assert "ipython" in VARIANTS["S1r"].tools()
    assert "ipython" not in VARIANTS["S1"].tools()
    assert CTX_RLM_STATE == "pi.rlm_state"


def test_seed_candidate_validates_and_has_provenance():
    assert HarnessCandidate(components=dict(SEED_CANDIDATE.components)) == SEED_CANDIDATE
    assert set(SEED_PROVENANCE) == set(SEED_CANDIDATE.components)
    assert set(REQUIRED_COMPONENTS) <= set(SEED_CANDIDATE.components)
    assert SEED_CANDIDATE.limits == RlmLimits(max_depth=1, max_children=4, max_output_chars=10000)
    rc = SEED_CANDIDATE.rlm_components()
    assert rc.candidate_digest == SEED_CANDIDATE.digest()
    assert "{prompt}" in rc.child_prompt


def _with(**changes: str) -> dict[str, str]:
    return {**SEED_CANDIDATE.components, **changes}


def test_candidate_rejects_an_undeclared_component():
    with pytest.raises(ValidationError, match="undeclared"):
        HarnessCandidate(components=_with(**{"evaluator.tolerance": "1"}))
    with pytest.raises(ValidationError, match="missing"):
        HarnessCandidate(components={"rlm.tool_doc": "x"})


@pytest.mark.parametrize(
    "config",
    [
        '{"observationPack": true, "cacheWriteReadRatio": 1}',
        '{"evidencePreservingReducerModel": "x"}',
        '{"actionFusion": "yes"}',
        "[true]",
        "not json",
    ],
)
def test_sol_pi_config_may_only_toggle_the_four_mechanisms(config):
    with pytest.raises(ValidationError):
        HarnessCandidate(components=_with(**{"sol_pi.config": config}))


def test_sol_pi_config_toggles_are_read_back():
    c = HarnessCandidate(
        components=_with(**{"sol_pi.config": '{"observationPack": true, "actionFusion": false}'})
    )
    assert c.sol_pi_mechanisms() == ("observationPack",)
    assert SEED_CANDIDATE.sol_pi_mechanisms() is None


@pytest.mark.parametrize(
    "name,value",
    [
        ("rlm.helpers", "import pickle\n"),
        ("rlm.helpers", "from subprocess import run\n"),
        ("rlm.helpers", "x = __import__('ssl')\n"),
        ("rlm.helpers", "def broken(:\n"),
        ("rlm.child_prompt", "no placeholder"),
        ("rlm.child_prompt", "-{prompt}"),
        ("rlm.child_prompt", "{prompt} {unknown}"),
        ("rlm.child_prompt", "{prompt}"),  # the model's text would lead `-p` (@file, -flag)
        ("rlm.child_prompt", "Task: {prompt} {prompt.text}"),
        ("rlm.child_prompt", "Task: {prompt} {prompt[a]}"),
        ("rlm.child_prompt", "Task: {{prompt}} {prompt}"),
        ("rlm.limits", '{"max_depth": 9, "max_children": 1, "max_output_chars": 1}'),
        ("rlm.system_prompt", "   "),
    ],
)
def test_component_validators_refuse_bad_values(name, value):
    assert name in COMPONENT_VALIDATORS
    with pytest.raises(ValidationError):
        HarnessCandidate(components=_with(**{name: value}))


def test_candidate_digest_is_content_identity():
    a = HarnessCandidate(components=_with(**{"rlm.tool_doc": "one"}))
    b = HarnessCandidate(components=_with(**{"rlm.tool_doc": "one"}))
    c = HarnessCandidate(components=_with(**{"rlm.tool_doc": "two"}))
    assert a.digest() == b.digest() != c.digest()


# ------------------------------------------------------------------ optimizer

FIXTURE_TOLERANCES = {m: "0" for m in CAPABILITY_METRICS if m != "false_success_claims"}


def _spec(capability: dict[str, str], gate: tuple[str, ...] = ("cost_usd",)) -> GateSpec:
    return GateSpec(
        tolerances=Tolerances(capability=capability, justification="fixture tolerances (A47)"),
        efficiency_gate=gate,
    )


def test_gate_spec_refuses_an_unset_tolerance():
    for missing in FIXTURE_TOLERANCES:
        rest = {k: v for k, v in FIXTURE_TOLERANCES.items() if k != missing}
        with pytest.raises(ValidationError, match=f"unresolved for {missing}"):
            _spec(rest)
    with pytest.raises(ValidationError):
        _spec({**FIXTURE_TOLERANCES, "count.supported": "-1"})
    with pytest.raises(ValidationError):
        _spec({**FIXTURE_TOLERANCES, "count.supported": "1.5e"})  # not decimal text
    with pytest.raises(ValidationError):
        GateSpec(tolerances=None, efficiency_gate=("cost_usd",))


def test_gate_spec_refuses_a_nonzero_false_success_tolerance():
    with pytest.raises(ValidationError, match="fixed at 0"):
        _spec({**FIXTURE_TOLERANCES, "false_success_claims": "1"})
    spec = _spec(FIXTURE_TOLERANCES)  # omitted: always 0 through tolerance_for
    assert "false_success_claims" not in spec.tolerances.capability
    assert _spec({**FIXTURE_TOLERANCES, "false_success_claims": "0"})


def test_gate_spec_efficiency_gate_is_declared_and_known():
    with pytest.raises(ValidationError):
        _spec(FIXTURE_TOLERANCES, gate=())
    with pytest.raises(ValidationError, match="unknown efficiency metric"):
        _spec(FIXTURE_TOLERANCES, gate=("cost_usd", "requests"))  # the old M4 name
    spec = _spec(FIXTURE_TOLERANCES, gate=tuple(EFFICIENCY_METRICS))
    dumped = spec.model_dump(mode="json", by_alias=True)
    assert dumped["schema"] == "onoda.gate-spec/2"
    assert GateSpec.model_validate(dumped) == spec


def test_split_is_deterministic_disjoint_and_labelled():
    ids = ["c1", "c2", "c3", "c4", "c5", "c6"]
    a = SplitAssignment.derive(ids, seed=7, final_count=2, exploratory=True)
    b = SplitAssignment.derive(list(reversed(ids)), seed=7, final_count=2, exploratory=True)
    assert a == b
    assert set(a.acceptance) | set(a.final) == set(ids)
    assert not set(a.acceptance) & set(a.final)
    assert a.exploratory
    with pytest.raises(ValidationError):
        SplitAssignment(seed=0, acceptance=("c1",), final=("c1",), exploratory=True)


def test_candidate_record_round_trips():
    rec = CandidateRecord(
        search_id="s1",
        iteration=0,
        candidate=SEED_CANDIDATE.digest(),
        components=SEED_CANDIDATE.components,
        origin="seed",
        decision="seed",
    )
    assert CandidateRecord.model_validate_json(rec.model_dump_json(by_alias=True)) == rec


def test_gepa_is_importable_at_the_pin():
    import importlib.metadata

    import gepa  # noqa: F401

    assert importlib.metadata.version("gepa") == "0.1.4"
