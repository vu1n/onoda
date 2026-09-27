"""Pi RLM ingest (M4-005): cells, child runs, committed state, over hand-written spools.

The spools are built here the way `pi/extensions/lib/spool.ts` writes them
(the `_Line` mac rule, child keys from `derive_child_key`), with synthetic
scripted responses and synthetic cell outputs; nothing here is a model or
solver result.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from onoda.adapters.pi.contract import (
    CTX_RLM_STATE,
    PI_VERSION,
    SCRIPTED_MODEL,
    SCRIPTED_PROVIDER,
    SPOOL_SCHEMA,
    canonical_state_json,
    derive_child_key,
)
from onoda.adapters.pi.ingest import ingest_spool, recorded_script
from onoda.budgets import cost_views
from onoda.clock import LogicalClock
from onoda.errors import IntegrityError, OnodaError
from onoda.records import TaskManifest
from onoda.replay import replay_run
from onoda.store import Store

CASE_ID = "demo-case"
INPUT = b"! synthetic ORCA input fixture\n* xyz 0 1\nH 0 0 0\n*\n"
OUTPUT = b"SYNTHETIC ORCA OUTPUT FIXTURE\n"
CLAIM = {
    "schema": "onoda.pi-claim/1",
    "case_id": CASE_ID,
    "final_energy": {"text": "-76.0", "unit": "Eh"},
}
KEY = bytes(range(32))
CONFIG = {"mode": "pi-scripted", "variant": "S1r"}
STATE = {"energies": ["-76.0"], "unit": "Eh"}


def _hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class RlmSpool:
    """A spool dir (events.jsonl + objects/) and its children/<nnnn>/ spools."""

    def __init__(self, path: Path, session_dir: Path, *, cost: str = "0.01"):
        self.dir = path
        (self.dir / "objects").mkdir(parents=True)
        self.session_dir = session_dir
        self.cost = cost
        self.lines: list[dict] = []
        self.requests = 0
        self.replay_keys: list[str] = []
        self.children: dict[int, RlmSpool] = {}
        self.line(
            "spool.start",
            schema=SPOOL_SCHEMA,
            pi_version=PI_VERSION,
            sol_pi_commit=None,
            variant="S1r",
            mechanisms=[],
            provider=SCRIPTED_PROVIDER,
            model=SCRIPTED_MODEL,
            scripted=True,
            session_id="sess-1",
            session_dir=str(session_dir),
        )

    def put(self, value) -> str:
        data = value if isinstance(value, bytes) else json.dumps(value).encode()
        (self.dir / "objects" / _hex(data)).write_bytes(data)
        return _hex(data)

    def line(self, type: str, **fields) -> None:
        self.lines.append({"seq": len(self.lines), "type": type, **fields})

    def turn(self, *blocks: dict) -> int:
        index = self.requests
        self.requests += 1
        context = {"messages": [{"role": "user", "content": f"{self.dir.name} request {index}"}]}
        key = _hex(json.dumps(context, sort_keys=True).encode())
        self.replay_keys.append(key)
        self.line(
            "provider.request",
            request_index=index,
            model=SCRIPTED_MODEL,
            context=self.put(context),
            source="scripted-provider",
            replay_key=key,
        )
        self.line(
            "provider.response",
            request_index=index,
            message=self.put({"role": "assistant", "content": list(blocks)}),
            stop_reason="toolUse",
            usage_tokens={"input": 10, "output": 5, "totalTokens": 15},
            cost_usd=self.cost,
        )
        return index

    def call(self, call_id: str, tool: str, args: dict) -> None:
        index = self.turn({"type": "toolCall", "id": call_id, "name": tool, "arguments": args})
        self.line(
            "tool.call",
            tool_call_id=call_id,
            tool_name=tool,
            input=self.put(args),
            request_index=index,
            decision="allowed",
        )

    def result(self, call_id: str, tool: str, text: str) -> None:
        for stage in ("raw", "visible"):
            self.line(
                "tool.result",
                tool_call_id=call_id,
                tool_name=tool,
                stage=stage,
                content=self.put([{"type": "text", "text": text}]),
                is_error=False,
            )

    def cell(self, call_id: str, index: int, code: str, stdout: str) -> None:
        reply = {"op": "exec.reply", "id": index + 1, "stdout": stdout, "stderr": ""}
        self.line(
            "rlm.cell",
            tool_call_id=call_id,
            cell_index=index,
            code=self.put(code.encode()),
            output=self.put({**reply, "error": None, "truncated": False}),
            is_error=False,
            truncated=False,
        )

    def state(self, call_id: str, name: str, value) -> str:
        digest = self.put(canonical_state_json(value).encode())
        self.line("rlm.state", tool_call_id=call_id, name=name, value=digest)
        return digest

    def child_start(self, call_id: str, index: int, depth: int, *, cost: str) -> RlmSpool:
        subdir = f"children/{index:04d}"
        self.line(
            "rlm.child.start",
            tool_call_id=call_id,
            child_index=index,
            depth=depth,
            spool_subdir=subdir,
            prompt=self.put(f"synthetic sub-question {index}".encode()),
            max_requests=10,
        )
        child = RlmSpool(self.dir / subdir, self.session_dir, cost=cost)
        self.children[index] = child
        return child

    def child_end(self, index: int, *, answer: str | None = "synthetic answer") -> None:
        child = self.children[index]
        end = child.lines[-1] if child.lines[-1]["type"] == "spool.end" else None
        self.line(
            "rlm.child.end",
            child_index=index,
            exit_code=0 if end else None,
            end_status=end["status"] if end else None,
            **child.charge(),
            answer=self.put(answer.encode()) if answer is not None else None,
        )

    def charge(self) -> dict[str, object]:
        """What this spool charges its parent, as the extension sums it."""
        requests, costs = 0, []
        for x in self.lines:
            if x["type"] == "provider.request":
                requests += 1
            elif x["type"] in ("provider.response", "rlm.child.end"):
                requests += x.get("requests", 0) if x["type"] == "rlm.child.end" else 0
                costs.append(x["cost_usd"])
        known = all(c is not None for c in costs)
        return {
            "requests": requests,
            "cost_usd": str(sum(map(Decimal, costs), Decimal(0))) if known else None,
        }

    def end(self, status: str = "settled") -> None:
        self.line("spool.end", status=status, requests=self.requests)

    def write(self, key: bytes | None = None) -> Path:
        out, prev = [], ""
        for x in self.lines:
            body = json.dumps(x, separators=(",", ":"))
            if key is not None:
                prev = hmac.new(key, (prev + body).encode(), hashlib.sha256).hexdigest()
                body = body[:-1] + f',"mac":"{prev}"}}'
            out.append(body + "\n")
        (self.dir / "events.jsonl").write_text("".join(out))
        for index, child in self.children.items():
            child.write(derive_child_key(key, index) if key is not None else None)
        return self.dir


def answering_child(parent: RlmSpool, call_id: str, index: int, depth: int, cost: str):
    child = parent.child_start(call_id, index, depth, cost=cost)
    child.turn({"type": "text", "text": f"synthetic answer {index}"})
    child.end()
    return child


def tree(root: Path, *, parent_end: bool = True) -> RlmSpool:
    """A keyed S1r parent: a cell committing state and spawning child 0 (which has a
    grandchild), a refused call, and child 1 whose spool stops without spool.end."""
    sb = RlmSpool(root / "spool", root / "sessions")
    sb.call("c1", "ipython", {"code": "a = rlm('q0')\ncommit_state('energies', s)"})
    child = sb.child_start("c1", 0, 1, cost="0.10")
    child.call("k1", "ipython", {"code": "rlm('q00')"})
    answering_child(child, "k1", 0, 2, "1.00")
    child.child_end(0)
    child.cell("k1", 0, "rlm('q00')", "'synthetic answer'\n")
    child.result("k1", "ipython", "'synthetic answer'")
    child.turn({"type": "text", "text": "synthetic answer 0"})
    child.end()
    sb.child_end(0)
    sb.state("c1", "energies", STATE)
    sb.cell("c1", 0, "a = rlm('q0')\ncommit_state('energies', s)", "")
    sb.result("c1", "ipython", "")
    sb.call("c2", "ipython", {"code": "rlm('q1')"})
    sb.line("rlm.child.refused", tool_call_id="c2", reason="budget", detail="no requests left")
    sb.cell("c2", 1, "rlm('q1')", "RlmError: budget")
    sb.result("c2", "ipython", "RlmError: budget")
    sb.call("c3", "ipython", {"code": "rlm('q2')"})
    stuck = sb.child_start("c3", 1, 1, cost="0.10")
    stuck.turn({"type": "text", "text": "never finished"})  # killed: no spool.end
    if not parent_end:
        return sb
    sb.child_end(1, answer=None)
    sb.cell("c3", 2, "rlm('q2')", "RlmError: child failed")
    sb.result("c3", "ipython", "RlmError: child failed")
    sb.state("c3", "energies", {**STATE, "checked": True})
    sb.call("c4", "report_states", CLAIM)
    sb.line("task.claim", tool_call_id="c4", claim=sb.put(CLAIM))
    sb.result("c4", "report_states", "claim recorded")
    sb.turn({"type": "text", "text": "done"})
    sb.end()
    return sb


def make_store(root: Path, **kwargs) -> tuple[Store, TaskManifest]:
    store = Store(root, clock=LogicalClock(), **kwargs)
    inp = store.put_bytes(INPUT, media_type="text/plain", labels=["task-input", "synthetic"])
    out = store.put_bytes(OUTPUT, media_type="text/plain", labels=["job-output", "synthetic"])
    task = TaskManifest.new(
        task_id="pi-demo-case",
        inputs={"input": inp.digest, "output": out.digest},
        permitted_context=["task-input"],
        split="dev",
        target={"kind": "pi-case", "case_id": CASE_ID},
        synthetic=True,
    )
    return store, task


def ingest(store, task, spool, run_id="pi-run", **kwargs):
    kwargs.setdefault("key", KEY)
    return ingest_spool(store, spool, run_id=run_id, task=task, config=CONFIG, **kwargs)


def receipts(store: Store, run_id: str):
    return [
        store.receipt(e.payload["invocation_id"])
        for e in store.events(run_id)
        if e.type == "invocation.committed"
    ]


def runs_receipts(store: Store, run_ids):
    return [r for run_id in run_ids for r in receipts(store, run_id)]


def child_events(store: Store, run_id: str):
    return [e for e in store.events(run_id) if e.type == "rlm.child"]


@pytest.fixture
def ingested(tmp_path):
    sb = tree(tmp_path / "w")
    sb.write(KEY)
    store, task = make_store(tmp_path / "store")
    return store, sb, task, ingest(store, task, sb.dir)


GRANDCHILD = "pi-run.rlm-0000.rlm-0000"


def _assert_nothing_recorded(store: Store) -> None:
    assert store.run_ids_with_journals() == []
    assert store.list_record_ids("run") == []


# ------------------------------------------------------------------- cells


def test_cell_receipts_reference_committed_objects(ingested):
    store, sb, _, result = ingested
    assert result.complete and result.status == "settled"
    cells = {
        r.metadata["tool_call_id"]: r for r in receipts(store, "pi-run") if "cell" in r.outputs
    }
    assert sorted(cells) == ["c1", "c2", "c3"]
    c1 = cells["c1"]
    assert c1.invocation_kind == "execution" and c1.metadata["tool_name"] == "ipython"
    assert set(c1.inputs) == {"input", "code"} and set(c1.outputs) == {"cell", "result"}
    assert store.get_bytes(c1.inputs["code"]).decode().startswith("a = rlm('q0')")
    assert store.get_json(c1.outputs["cell"])["op"] == "exec.reply"
    assert c1.metadata["cell"]["cell_index"] == 0 and "synthetic" in c1.labels
    for receipt in cells.values():
        for digest in (*receipt.inputs.values(), *receipt.outputs.values()):
            assert store.has_record("artifact", digest)
    spec = store.get_record("step", c1.spec_digest)
    assert spec.inputs == c1.inputs


def test_every_ref_is_committed_before_its_event(tmp_path):
    sb = tree(tmp_path / "w")
    sb.write(KEY)
    store, task = make_store(tmp_path / "store")
    checked: list[bool] = []
    original = store.append_event

    def check(run_id, **kwargs):
        checked.extend(store.ref_exists(r) for r in (kwargs.get("payload_refs") or {}).values())
        return original(run_id, **kwargs)

    store.append_event = check
    ingest(store, task, sb.dir)
    assert checked and all(checked)


# ------------------------------------------------------------------ children


def test_each_child_is_its_own_run_linked_by_an_rlm_child_event(ingested):
    store, _, task, result = ingested
    assert result.children == ("pi-run.rlm-0000", "pi-run.rlm-0001")
    done, stuck = child_events(store, "pi-run")
    child = store.run_record("pi-run.rlm-0000")
    assert child.task == task.task_id and child.parent is None and "rlm-child" in child.labels
    assert {k: child.config[k] for k in ("parent_run", "child_index", "depth")} == {
        "parent_run": "pi-run",
        "child_index": 0,
        "depth": 1,
    }
    finalized = [d for _, d, cp in store.checkpoints(child.run_id) if cp.boundary == "finalized"]
    assert done.payload["complete"] and done.payload["finalized"] == finalized[0]
    assert done.payload_refs["checkpoint"] == f"checkpoint/{finalized[0]}"
    assert done.payload_refs["run"] == "run/pi-run.rlm-0000"
    assert store.get_bytes(done.payload_refs["answer"]) == b"synthetic answer"
    assert store.get_bytes(done.payload_refs["prompt"]) == b"synthetic sub-question 0"
    # the child's journal was complete before the parent's event cited it
    assert store.events(child.run_id)[-1].type == "pi.ingested"

    assert not stuck.payload["complete"] and stuck.payload["finalized"] is None
    assert "checkpoint" not in stuck.payload_refs and "answer" not in stuck.payload_refs
    assert stuck.payload["incomplete_reason"] == "the child's spool has no spool.end"
    assert stuck.payload["child_run"] == "pi-run.rlm-0001"
    assert not any(cp.boundary == "finalized" for *_, cp in store.checkpoints("pi-run.rlm-0001"))

    # the grandchild nests the same way, keyed by the child's derived key
    (grand,) = child_events(store, "pi-run.rlm-0000")
    assert grand.payload["child_run"] == GRANDCHILD and grand.payload["depth"] == 2
    assert store.run_record(GRANDCHILD).config["parent_run"] == "pi-run.rlm-0000"
    for run_id in ("pi-run", *result.children, GRANDCHILD):
        replay_run(store, run_id)


def test_refused_call_is_an_event_and_spawns_nothing(ingested):
    store, *_ = ingested
    (refused,) = [e for e in store.events("pi-run") if e.type == "rlm.child-refused"]
    assert refused.payload["tool_call_id"] == "c2" and refused.payload["reason"] == "budget"
    assert [e.payload["child_index"] for e in child_events(store, "pi-run")] == [0, 1]


def test_parent_crash_leaves_a_started_child_incomplete(tmp_path):
    sb = tree(tmp_path / "w", parent_end=False)
    sb.write(KEY)
    store, task = make_store(tmp_path / "store")
    result = ingest(store, task, sb.dir)
    assert not result.complete
    done, stuck = child_events(store, "pi-run")
    assert done.payload["complete"]
    assert not stuck.payload["complete"] and "checkpoint" not in stuck.payload_refs
    assert stuck.payload["incomplete_reason"] == "the parent recorded no rlm.child.end"
    assert stuck.payload["exit_code"] is None
    types = [e.type for e in store.events("pi-run")]
    assert types.index("rlm.child") < types.index("pi.ingested")
    stuck_script = recorded_script(store, "pi-run").children[1]
    assert [t.expect_replay_key for t in stuck_script.turns] == sb.children[1].replay_keys


def test_child_without_a_spool_is_recorded_without_a_run(tmp_path):
    sb = RlmSpool(tmp_path / "w" / "spool", tmp_path / "w" / "sessions")
    sb.call("c1", "ipython", {"code": "rlm('q')"})
    sb.child_start("c1", 0, 1, cost="0")
    sb.write(KEY)
    shutil.rmtree(sb.dir / "children")
    store, task = make_store(tmp_path / "store")
    ingest(store, task, sb.dir)
    (event,) = child_events(store, "pi-run")
    assert event.payload["child_run"] is None and "run" not in event.payload_refs
    script = recorded_script(store, "pi-run")
    assert len(script.children) == 1 and script.children[0].turns == ()


# ------------------------------------------------------------------ state


def test_committed_state_is_an_artifact_cited_by_later_checkpoints(ingested):
    store, *_, result = ingested
    states = [e for e in store.events("pi-run") if e.type == "rlm.state"]
    assert [e.payload["name"] for e in states] == ["energies", "energies"]
    first, latest = (e.payload_refs["value"] for e in states)
    assert store.get_json(first) == STATE and store.get_json(latest)["checked"] is True
    for digest in (first, latest):
        assert "rlm-state" in store.descriptor(digest).labels
    assert result.rlm_state == {"energies": latest}
    by_boundary = {cp.boundary: cp for *_, cp in store.checkpoints("pi-run")}
    assert CTX_RLM_STATE not in by_boundary["initialized"].context
    assert by_boundary["finalized"].context[CTX_RLM_STATE] == {"energies": latest}
    # a child that committed nothing carries no key, like an M3 run
    child_cps = store.checkpoints("pi-run.rlm-0000")
    assert all(CTX_RLM_STATE not in cp.context for *_, cp in child_cps)


def test_non_canonical_state_is_refused(tmp_path):
    sb = RlmSpool(tmp_path / "w" / "spool", tmp_path / "w" / "sessions")
    sb.call("c1", "ipython", {"code": "commit_state('x', 1)"})
    sb.line("rlm.state", tool_call_id="c1", name="x", value=sb.put(b'{"b": 1, "a": 2}'))
    sb.end()
    sb.write(KEY)
    store, task = make_store(tmp_path / "store")
    with pytest.raises(IntegrityError, match="not canonical"):
        ingest(store, task, sb.dir)
    _assert_nothing_recorded(store)


# ------------------------------------------------------------------- costs


def test_cost_views_include_child_usage_exactly_once(ingested):
    store, sb, *_ = ingested
    views = cost_views(store, "pi-run")
    own = len(receipts(store, "pi-run"))
    parent_decisions = sb.requests
    # child 0: 2 requests at 0.10, its grandchild 1 at 1.00; child 1 (stuck): 1 at 0.10
    assert views["child_runs"] == ["pi-run.rlm-0000", "pi-run.rlm-0001", GRANDCHILD]
    expected = Decimal("0.01") * parent_decisions + Decimal("0.10") * 3 + Decimal("1.00")
    assert Decimal(views["marginal"]["known_cost_usd"]) == expected
    tools = [
        r for r in runs_receipts(store, ["pi-run", *views["child_runs"]]) if not r.usage.cost_usd
    ]
    assert views["marginal"]["unknown_cost_invocations"] == len(tools)  # tool receipts: no cost
    assert views["full_path"] == views["marginal"]
    children = sum(len(receipts(store, r)) for r in views["child_runs"])
    assert views["marginal"]["invocations"] == own + children
    assert views["children"]["invocations"] == children
    assert views["marginal"]["units"]["totalTokens"] == 15 * (parent_decisions + 4)
    child_view = cost_views(store, "pi-run.rlm-0000")
    assert Decimal(child_view["marginal"]["known_cost_usd"]) == Decimal("0.10") * 2 + Decimal(
        "1.00"
    )
    assert "child_runs" not in cost_views(store, GRANDCHILD)


# ------------------------------------------------------------------ replay


def test_recorded_script_rebuilds_nested_children(ingested):
    store, sb, *_ = ingested
    script = recorded_script(store, "pi-run")
    assert [t.expect_replay_key for t in script.turns] == sb.replay_keys
    assert len(script.children) == 2  # the refused call has no entry
    child, stuck = script.children
    assert child.source == "replay:pi-run.rlm-0000"
    assert [t.expect_replay_key for t in child.turns] == sb.children[0].replay_keys
    (grand,) = child.children
    assert [t.expect_replay_key for t in grand.turns] == sb.children[0].children[0].replay_keys
    assert grand.children == ()
    assert [t.expect_replay_key for t in stuck.turns] == sb.children[1].replay_keys


# ------------------------------------------------------------- idempotence


def test_reingest_is_a_noop(ingested):
    store, sb, task, first = ingested
    runs = ("pi-run", *first.children, GRANDCHILD)
    before = {r: store.events(r) for r in runs}
    again = ingest(store, task, sb.dir)
    assert again.noop and again.children == first.children
    assert {r: store.events(r) for r in runs} == before


@pytest.mark.parametrize("nth", [3, 12, 20, 30])
def test_interrupted_ingest_continues(tmp_path, nth):
    sb = tree(tmp_path / "w")
    sb.write(KEY)
    clean, task = make_store(tmp_path / "clean")
    ingest(clean, task, sb.dir)
    seen = 0

    def crash(at: str) -> None:
        nonlocal seen
        if at == "event.appended":
            seen += 1
            if seen == nth:
                raise RuntimeError("simulated crash")

    crashing, task = make_store(tmp_path / "crash", crash_hook=crash)
    with pytest.raises(RuntimeError):
        ingest(crashing, task, sb.dir)
    reopened = Store(tmp_path / "crash", clock=LogicalClock("2026-06-01T00:00:00Z"))
    assert ingest(reopened, task, sb.dir).complete

    def shape(store, run_id):
        return [(e.type, e.payload.get("step_key")) for e in store.events(run_id)]

    for run_id in ("pi-run", "pi-run.rlm-0000", "pi-run.rlm-0001", GRANDCHILD):
        assert shape(reopened, run_id) == shape(clean, run_id)


# ---------------------------------------------------------------- refusals


def test_child_line_citing_an_uncommitted_object_is_refused(tmp_path):
    sb = tree(tmp_path / "w")
    sb.write(KEY)
    prompt = next(ln["prompt"] for ln in sb.lines if ln["type"] == "rlm.child.start")
    (sb.dir / "objects" / prompt).unlink()
    store, task = make_store(tmp_path / "store")
    with pytest.raises(IntegrityError, match="spool object"):
        ingest(store, task, sb.dir)
    _assert_nothing_recorded(store)


def test_object_missing_from_a_grandchild_spool_is_refused(tmp_path):
    sb = tree(tmp_path / "w")
    sb.write(KEY)
    grand = sb.children[0].children[0]
    shutil.rmtree(grand.dir / "objects")
    (grand.dir / "objects").mkdir()
    store, task = make_store(tmp_path / "store")
    with pytest.raises(IntegrityError, match="spool object"):
        ingest(store, task, sb.dir)
    _assert_nothing_recorded(store)


def test_unnamed_child_dir_is_refused(tmp_path):
    sb = tree(tmp_path / "w")
    sb.write(KEY)
    (sb.dir / "children" / "0007").mkdir()
    store, task = make_store(tmp_path / "store")
    with pytest.raises(IntegrityError, match="children/0007"):
        ingest(store, task, sb.dir)
    _assert_nothing_recorded(store)


def test_child_spool_under_the_wrong_key_is_refused(tmp_path):
    sb = tree(tmp_path / "w")
    sb.write(KEY)
    sb.children[0].write(KEY)  # the parent's key, not the derived child key
    store, task = make_store(tmp_path / "store")
    with pytest.raises(IntegrityError, match="mac"):
        ingest(store, task, sb.dir)
    _assert_nothing_recorded(store)


def test_child_end_status_must_match_the_child_spool(tmp_path):
    sb = tree(tmp_path / "w")
    end = next(ln for ln in sb.lines if ln["type"] == "rlm.child.end")
    end["end_status"] = "aborted"
    sb.write(KEY)
    store, task = make_store(tmp_path / "store")
    with pytest.raises(IntegrityError, match="end status"):
        ingest(store, task, sb.dir)
    _assert_nothing_recorded(store)


def test_rlm_lines_outside_an_rlm_variant_are_refused(tmp_path):
    sb = RlmSpool(tmp_path / "w" / "spool", tmp_path / "w" / "sessions")
    sb.lines[0]["variant"] = "S1"
    sb.call("c1", "ipython", {"code": "1"})
    sb.cell("c1", 0, "1", "1\n")
    sb.end()
    sb.write()
    store, task = make_store(tmp_path / "store")
    with pytest.raises(IntegrityError, match="non-RLM"):
        ingest_spool(store, sb.dir, run_id="pi-run", task=task, config={**CONFIG, "variant": "S1"})
    _assert_nothing_recorded(store)


def test_child_config_keys_are_reserved(tmp_path):
    sb = tree(tmp_path / "w")
    sb.write(KEY)
    store, task = make_store(tmp_path / "store")
    with pytest.raises(OnodaError, match="parent_run"):
        ingest_spool(
            store, sb.dir, run_id="pi-run", task=task, config={**CONFIG, "parent_run": "x"}
        )


def test_a_child_charge_that_disagrees_with_its_spool_is_refused(tmp_path):
    sb = tree(tmp_path / "w")
    (end,) = [x for x in sb.lines if x["type"] == "rlm.child.end" and x["child_index"] == 0]
    end["requests"] -= 1  # the parent was charged less than its child spent
    sb.write(KEY)
    store, task = make_store(tmp_path / "store")
    with pytest.raises(IntegrityError, match="was charged"):
        ingest(store, task, sb.dir)
    _assert_nothing_recorded(store)
