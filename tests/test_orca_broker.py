"""VTE-002: the host-side ORCA broker (A59) and the scheduler's extra files.

The "ORCA" here is a small Python script labelled as a stand-in, never
presented as ORCA. It mimics the calling convention (argv[1] is the input,
the log goes to stdout), echoes any `%moinp` file it was given, writes a
`job.gbw` product, and counts its own runs in a file next to itself so a test
can prove that nothing executed.
"""

from __future__ import annotations

import base64
import json
import socket
import stat
import sys
from pathlib import Path

import pytest

from onoda.adapters.pi.vte_contract import BrokerAttach, BrokerReply, BrokerSubmit
from onoda.canonical import sha256_digest
from onoda.errors import OnodaError
from onoda.science.orca_broker import (
    JOURNAL_NAME,
    BrokerReplay,
    OrcaBroker,
    cache_key,
    placed_name,
    read_journal,
)
from onoda.science.orca_local import OrcaLocalScheduler, OrcaSettings

STAND_IN_MARKER = "STAND-IN EXECUTABLE - NOT ORCA"

STAND_IN = f"""#!/usr/bin/env {sys.executable}
import pathlib, re, sys
counter = pathlib.Path(__file__).with_suffix(".runs")
counter.write_text(str(int(counter.read_text()) + 1 if counter.exists() else 1))
text = open(sys.argv[1]).read()
print("{STAND_IN_MARKER}")
print("input:", text)
m = re.search(r'%moinp "([^"]+)"', text)
if m:
    print("moinp:", open(m.group(1)).read())
open("job.gbw", "w").write("orbitals for " + text)
sys.exit(1 if "FAIL" in text else 0)
"""

INPUT = "! CASSCF aug-cc-pVTZ\n* xyzfile 0 1 structure.xyz\n"


def stand_in(tmp_path: Path) -> Path:
    path = tmp_path / "orca_stand_in.py"
    path.write_text(STAND_IN)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def runs(binary: Path) -> int:
    counter = binary.with_suffix(".runs")
    return int(counter.read_text()) if counter.exists() else 0


def scheduler(tmp_path: Path, binary: Path, work: str = "work") -> OrcaLocalScheduler:
    return OrcaLocalScheduler(
        OrcaSettings(
            enabled=True,
            binary=str(binary),
            work_root=str(tmp_path / work),
            max_wall_seconds=10,
            stand_in=True,
        )
    )


def broker(tmp_path: Path, name: str = "run1", **kw) -> OrcaBroker:
    return OrcaBroker(tmp_path / name / JOURNAL_NAME, **kw)


def submit(text: str = INPUT, call: str = "c1", attach=()) -> BrokerSubmit:
    return BrokerSubmit(tool_call_id=call, input=text, attach=attach)


def log(reply: BrokerReply) -> str:
    return base64.b64decode(reply.output_b64).decode()


def over_socket(path: Path, request: BrokerSubmit) -> BrokerReply:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(str(path))
        s.sendall(request.model_dump_json().encode() + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    return BrokerReply.model_validate_json(data)


# --- scheduler: extra input files and produced files ----------------------------------


def test_scheduler_places_extra_files_and_lists_products(tmp_path):
    sched = scheduler(tmp_path, stand_in(tmp_path))
    text = b'%moinp "j0001.gbw"\n'
    job = sched.submit("k1", "orca", {"input": text, "j0001.gbw": b"old orbitals"}, {})
    assert sched.status(job) == "completed"
    assert (sched.root / job / "j0001.gbw").read_bytes() == b"old orbitals"
    assert b"moinp: old orbitals" in sched.collect(job)["log"]
    produced = sched.produced_files(job)
    assert set(produced) == {"job.gbw"}  # not job.inp/out/err, not the extra input
    assert produced["job.gbw"].read_text().startswith("orbitals for")


@pytest.mark.parametrize("name", ["job.inp", "job.out", "../x.gbw", "a/b", ".hidden"])
def test_scheduler_refuses_bad_extra_file_names(tmp_path, name):
    sched = scheduler(tmp_path, stand_in(tmp_path))
    with pytest.raises(OnodaError, match="not allowed"):
        sched.submit("k1", "orca", {"input": b"x", name: b"y"}, {})
    assert sched.executions == 0


# --- live execution over the socket ---------------------------------------------------


def test_real_execution_over_socket_is_journaled_as_stand_in(tmp_path):
    binary = stand_in(tmp_path)
    b = broker(tmp_path, scheduler=scheduler(tmp_path, binary))
    sock = tmp_path / "b.sock"
    with b:
        b.serve(sock)
        assert stat.S_IMODE(sock.stat().st_mode) == 0o600
        reply = over_socket(sock, submit())
    assert not sock.exists()
    assert reply.status == "completed" and reply.job_id == "j0001"
    assert reply.executed_here and not reply.cache_hit
    assert log(reply).startswith(STAND_IN_MARKER)
    assert reply.files == ("job.gbw",)
    assert reply.input_sha256 == sha256_digest(INPUT.encode())
    assert reply.output_sha256 == sha256_digest(base64.b64decode(reply.output_b64))
    assert runs(binary) == 1

    request, answer = read_journal(b.journal_path)
    assert request.event == "request" and request.tool_call_id == "c1"
    assert request.input_sha256 == reply.input_sha256
    assert answer.event == "reply" and answer.job_id == "j0001"
    assert answer.executed_here and not answer.cache_hit and not answer.replay
    assert answer.stand_in is True  # synthetic, never presented as ORCA
    assert answer.binary_sha256 == sha256_digest(binary.read_bytes())
    assert answer.key == cache_key(reply.input_sha256, [], answer.binary_sha256)
    assert answer.scheduler_job_id == "job-00001"
    assert isinstance(answer.duration_ms, int)
    assert answer.output_sha256 == reply.output_sha256
    assert [f.name for f in answer.files] == ["job.gbw"]


def test_journal_is_written_before_the_reply_is_sent(tmp_path, monkeypatch):
    b = broker(tmp_path, scheduler=scheduler(tmp_path, stand_in(tmp_path)))
    seen: list[tuple] = []
    original = OrcaBroker._send

    def spy(self, wfile, reply):
        last = read_journal(self.journal_path)[-1]
        seen.append((last.event, last.job_id, last.output_sha256, reply.output_sha256))
        original(self, wfile, reply)

    monkeypatch.setattr(OrcaBroker, "_send", spy)
    with b:
        b.serve(tmp_path / "b.sock")
        reply = over_socket(tmp_path / "b.sock", submit())
    assert seen == [("reply", "j0001", reply.output_sha256, reply.output_sha256)]


def test_invalid_request_line_is_refused_and_journaled(tmp_path):
    binary = stand_in(tmp_path)
    b = broker(tmp_path, scheduler=scheduler(tmp_path, binary))
    with b:
        b.serve(tmp_path / "b.sock")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(str(tmp_path / "b.sock"))
            s.sendall(b'{"op": "rm -rf"}\n')
            reply = BrokerReply.model_validate_json(s.makefile("rb").readline())
    assert reply.status == "refused" and reply.reason == "invalid_request"
    request, answer = read_journal(b.journal_path)
    assert request.request_sha256 == sha256_digest(b'{"op": "rm -rf"}\n')
    assert answer.status == "refused" and answer.job_id is None
    assert runs(binary) == 0


# --- cache -----------------------------------------------------------------------------


def test_cache_hit_is_acquired_not_executed(tmp_path):
    binary = stand_in(tmp_path)
    cache = tmp_path / "cache"
    first = broker(tmp_path, "run1", scheduler=scheduler(tmp_path, binary), cache_dir=cache)
    r1 = first.submit(submit())
    assert r1.executed_here and runs(binary) == 1

    # Same broker, same input: served from the cache under a new job id.
    r2 = first.submit(submit(call="c2"))
    assert r2.job_id == "j0002"
    assert r2.cache_hit and not r2.executed_here
    assert r2.output_b64 == r1.output_b64 and r2.files == r1.files

    # A later run sharing the cache directory (and the work root) runs nothing either.
    sched = scheduler(tmp_path, binary)
    second = broker(tmp_path, "run2", scheduler=sched, cache_dir=cache)
    r3 = second.submit(submit())
    assert r3.job_id == "j0001" and r3.cache_hit and not r3.executed_here
    assert r3.output_sha256 == r1.output_sha256
    assert runs(binary) == 1 and sched.executions == 1

    hit = read_journal(second.journal_path)[-1]
    assert hit.cache_hit and not hit.executed_here
    assert hit.scheduler_job_id is None and hit.duration_ms is None
    assert hit.stand_in is True
    assert hit.key == read_journal(first.journal_path)[1].key


def test_a_changed_input_misses_the_cache(tmp_path):
    binary = stand_in(tmp_path)
    b = broker(tmp_path, scheduler=scheduler(tmp_path, binary), cache_dir=tmp_path / "cache")
    b.submit(submit())
    r = b.submit(submit(INPUT + "\n"))
    assert r.executed_here and not r.cache_hit and runs(binary) == 2


def test_failed_jobs_are_not_cached_and_rerun_as_new_executions(tmp_path):
    binary = stand_in(tmp_path)
    b = broker(tmp_path, scheduler=scheduler(tmp_path, binary), cache_dir=tmp_path / "cache")
    r1 = b.submit(submit("FAIL\n"))
    r2 = b.submit(submit("FAIL\n", call="c2"))
    assert r1.status == r2.status == "failed"
    assert r1.reason == "nonzero_exit"
    assert r1.executed_here and r2.executed_here and not r2.cache_hit
    assert runs(binary) == 2
    # Distinct scheduler jobs: the cache key is never the execution id.
    replies = [e for e in read_journal(b.journal_path) if e.event == "reply"]
    assert replies[0].key == replies[1].key
    assert replies[0].scheduler_job_id != replies[1].scheduler_job_id


def test_a_tampered_cache_entry_is_refused_not_served(tmp_path):
    binary = stand_in(tmp_path)
    cache = tmp_path / "cache"
    b = broker(tmp_path, scheduler=scheduler(tmp_path, binary), cache_dir=cache)
    r1 = b.submit(submit())
    (entry,) = [p for p in cache.iterdir() if not p.name.startswith(".")]
    (entry / "log").write_bytes(b"forged\n")
    r2 = b.submit(submit(call="c2"))
    assert r2.status == "refused" and r2.reason == "cache_corrupt"
    assert r1.executed_here and runs(binary) == 1
    assert (entry / "log").read_bytes() == b"forged\n"  # never overwritten


# --- attachments ---------------------------------------------------------------------


def test_attach_an_earlier_jobs_file(tmp_path):
    binary = stand_in(tmp_path)
    b = broker(tmp_path, scheduler=scheduler(tmp_path, binary), cache_dir=tmp_path / "cache")
    r1 = b.submit(submit())
    assert placed_name("j0001", "job.gbw") == "j0001.gbw"
    follow = '%moinp "j0001.gbw"\n' + INPUT
    r2 = b.submit(submit(follow, "c2", (BrokerAttach(job_id="j0001", name="job.gbw"),)))
    assert r2.status == "completed" and r2.executed_here
    assert "moinp: orbitals for " + INPUT in log(r2)

    gbw_sha = sha256_digest(("orbitals for " + INPUT).encode())
    reply = read_journal(b.journal_path)[-1]
    assert reply.key == cache_key(r2.input_sha256, [("j0001.gbw", gbw_sha)], reply.binary_sha256)
    assert r1.files == ("job.gbw",)

    # A cache hit's files can be attached too (served from the cache directory).
    r3 = b.submit(submit(call="c3"))
    assert r3.cache_hit and r3.job_id == "j0003"
    follow3 = '%moinp "j0003.gbw"\n' + INPUT
    r4 = b.submit(submit(follow3, "c4", (BrokerAttach(job_id="j0003", name="job.gbw"),)))
    assert r4.executed_here and "moinp: orbitals for " + INPUT in log(r4)


def test_unknown_attachments_are_refused(tmp_path):
    binary = stand_in(tmp_path)
    b = broker(tmp_path, scheduler=scheduler(tmp_path, binary))
    r = b.submit(submit(attach=(BrokerAttach(job_id="j0001", name="job.gbw"),)))
    assert r.status == "refused" and r.reason == "unknown_attachment"
    b.submit(submit())
    r = b.submit(submit(attach=(BrokerAttach(job_id="j0001", name="job.out"),)))
    assert r.reason == "unknown_attachment"  # logs are not products
    twice = (BrokerAttach(job_id="j0001", name="job.gbw"),) * 2
    assert b.submit(submit(attach=twice)).reason == "duplicate_attachment"
    assert runs(binary) == 1 and b.jobs == 1


def test_another_brokers_jobs_cannot_be_attached(tmp_path):
    binary = stand_in(tmp_path)
    cache = tmp_path / "cache"
    first = broker(tmp_path, "run1", scheduler=scheduler(tmp_path, binary), cache_dir=cache)
    first.submit(submit())
    second = broker(tmp_path, "run2", scheduler=scheduler(tmp_path, binary), cache_dir=cache)
    r = second.submit(submit(attach=(BrokerAttach(job_id="j0001", name="job.gbw"),)))
    assert r.status == "refused" and r.reason == "unknown_attachment"


# --- attempt cap ---------------------------------------------------------------------


def test_max_executions_counts_hits_and_refuses_beyond(tmp_path):
    binary = stand_in(tmp_path)
    b = broker(
        tmp_path,
        scheduler=scheduler(tmp_path, binary),
        cache_dir=tmp_path / "cache",
        max_executions=2,
    )
    assert b.submit(submit()).executed_here
    assert b.submit(submit(call="c2")).cache_hit
    r = b.submit(submit("! something new\n", call="c3"))
    assert r.status == "refused" and r.reason == "max_executions"
    assert r.job_id is None and not r.executed_here and not r.cache_hit
    assert runs(binary) == 1 and b.jobs == 2
    last = read_journal(b.journal_path)[-1]
    assert last.status == "refused" and last.tool_call_id == "c3"


# --- replay --------------------------------------------------------------------------


def _record(tmp_path: Path, binary: Path) -> tuple[OrcaBroker, list[BrokerReply]]:
    b = broker(tmp_path, "live", scheduler=scheduler(tmp_path, binary))
    follow = '%moinp "j0001.gbw"\n' + INPUT
    replies = [
        b.submit(submit()),
        b.submit(submit(follow, "c2", (BrokerAttach(job_id="j0001", name="job.gbw"),))),
        b.submit(submit("FAIL\n", "c3")),
    ]
    return b, replies


def _outputs(replies: list[BrokerReply]) -> dict[str, bytes]:
    return {r.output_sha256: base64.b64decode(r.output_b64) for r in replies}


def test_replay_serves_recorded_outputs_and_never_executes(tmp_path):
    binary = stand_in(tmp_path)
    live, recorded = _record(tmp_path, binary)
    assert runs(binary) == 3
    replay = BrokerReplay.from_journal(read_journal(live.journal_path), _outputs(recorded))
    b = broker(tmp_path, "replay", replay=replay)
    follow = '%moinp "j0001.gbw"\n' + INPUT
    replies = [
        b.submit(submit()),
        b.submit(submit(follow, "c2", (BrokerAttach(job_id="j0001", name="job.gbw"),))),
        b.submit(submit("FAIL\n", "c3")),
    ]
    assert runs(binary) == 3  # nothing ran
    for got, want in zip(replies, recorded, strict=True):
        assert got.status == want.status and got.job_id == want.job_id
        assert got.output_b64 == want.output_b64 and got.files == want.files
        assert not got.executed_here and got.cache_hit
    entries = [e for e in read_journal(b.journal_path) if e.event == "reply"]
    assert all(e.replay and not e.executed_here for e in entries)
    live_keys = [e.key for e in read_journal(live.journal_path) if e.event == "reply"]
    assert [e.key for e in entries] == live_keys


def test_replay_refuses_anything_not_recorded(tmp_path):
    binary = stand_in(tmp_path)
    live, recorded = _record(tmp_path, binary)
    replay = BrokerReplay.from_journal(read_journal(live.journal_path), _outputs(recorded))
    b = broker(tmp_path, "replay", replay=replay)
    assert b.submit(submit("! a different input\n")).reason == "replay_miss"
    assert b.submit(submit()).status == "completed"
    assert b.submit(submit(call="c2")).reason == "replay_miss"  # each record serves once
    assert runs(binary) == 3
    # The caller's replay is not consumed.
    assert len(replay.jobs[read_journal(live.journal_path)[1].key]) == 1


def test_replay_of_a_run_without_jobs_serves_nothing(tmp_path):
    binary = stand_in(tmp_path)
    live = broker(tmp_path, "live", scheduler=scheduler(tmp_path, binary), max_executions=1)
    live.submit(submit(attach=(BrokerAttach(job_id="j0001", name="job.gbw"),)))
    replay = BrokerReplay.from_journal(read_journal(live.journal_path), {})
    b = broker(tmp_path, "replay", replay=replay)
    assert b.submit(submit()).reason == "replay_miss"
    assert runs(binary) == 0


def test_replay_needs_the_recorded_bytes(tmp_path):
    binary = stand_in(tmp_path)
    live, recorded = _record(tmp_path, binary)
    outputs = _outputs(recorded)
    outputs[recorded[0].output_sha256] = b"not the recorded log"
    with pytest.raises(OnodaError, match="no bytes"):
        BrokerReplay.from_journal(read_journal(live.journal_path), outputs)


def test_broker_mode_settings_fail_closed(tmp_path):
    binary = stand_in(tmp_path)
    with pytest.raises(OnodaError, match="exactly one"):
        broker(tmp_path)
    replay = BrokerReplay(binary_sha256=sha256_digest(b"x"))
    with pytest.raises(OnodaError, match="exactly one"):
        broker(tmp_path, scheduler=scheduler(tmp_path, binary), replay=replay)
    with pytest.raises(OnodaError, match="no cache"):
        broker(tmp_path, replay=replay, cache_dir=tmp_path / "cache")
    with pytest.raises(OnodaError, match="max_executions"):
        broker(tmp_path, replay=replay, max_executions=0)


# --- journal -------------------------------------------------------------------------


def test_an_existing_journal_is_never_continued(tmp_path):
    binary = stand_in(tmp_path)
    b = broker(tmp_path, scheduler=scheduler(tmp_path, binary))
    b.submit(submit())
    before = b.journal_path.read_bytes()
    with pytest.raises(FileExistsError):
        broker(tmp_path, scheduler=scheduler(tmp_path, binary))
    assert b.journal_path.read_bytes() == before


def test_read_journal_refuses_gaps_and_malformed_lines(tmp_path):
    b = broker(tmp_path, scheduler=scheduler(tmp_path, stand_in(tmp_path)))
    b.submit(submit())
    lines = b.journal_path.read_bytes().splitlines(keepends=True)
    gap = tmp_path / "gap.jsonl"
    gap.write_bytes(lines[1])
    with pytest.raises(OnodaError, match="seq"):
        read_journal(gap)
    bad = tmp_path / "bad.jsonl"
    reply = json.loads(lines[1])
    reply["cache_hit"] = True  # a hit that also executed
    bad.write_bytes(lines[0] + json.dumps(reply).encode() + b"\n")
    with pytest.raises(OnodaError, match="line 2"):
        read_journal(bad)
