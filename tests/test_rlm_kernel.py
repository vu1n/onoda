"""M4-002: the stdlib-only RLM kernel, driven over a fake host socket."""

from __future__ import annotations

import ast
import importlib.util
import json
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from onoda.adapters.pi import contract

KERNEL_PATH = Path(__file__).resolve().parents[1] / "pi" / "rlm" / "onoda_rlm_kernel.py"

_spec = importlib.util.spec_from_file_location("onoda_rlm_kernel", KERNEL_PATH)
kernel_module = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(kernel_module)


class FakeHost:
    """Drives the kernel subprocess as `onoda-rlm.ts` would, one JSON line at a time."""

    def __init__(self, conn: socket.socket, proc: subprocess.Popen) -> None:
        self._conn = conn
        self.proc = proc
        self._buf = b""

    def send(self, message: dict[str, Any]) -> None:
        self._conn.sendall((json.dumps(message) + "\n").encode("utf-8"))

    def recv(self) -> dict[str, Any] | None:
        while b"\n" not in self._buf:
            chunk = self._conn.recv(65536)
            if not chunk:
                return None
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line.decode("utf-8"))

    def init(self, helpers: str = "", max_output_chars: int = 10_000) -> dict[str, Any]:
        ready = self.recv()
        assert ready == {"op": "ready", "protocol": contract.KERNEL_PROTOCOL, "pid": self.proc.pid}
        self.send(
            {
                "op": "init",
                "id": 0,
                "protocol": contract.KERNEL_PROTOCOL,
                "helpers": helpers,
                "max_output_chars": max_output_chars,
            }
        )
        reply = self.recv()
        assert reply["op"] == "exec.reply" and reply["id"] == 0
        return reply

    def exec(self, code: str, exec_id: int) -> dict[str, Any]:
        self.send({"op": "exec", "id": exec_id, "code": code})
        reply = self.recv()
        assert reply["op"] == "exec.reply" and reply["id"] == exec_id
        return reply


@pytest.fixture
def fake_host(tmp_path) -> FakeHost:
    sock_path = tmp_path / "kernel.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(sock_path))
    listener.listen(1)
    listener.settimeout(10)

    proc = subprocess.Popen(
        [sys.executable, str(KERNEL_PATH), "--connect", str(sock_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        conn, _ = listener.accept()
        conn.settimeout(10)
    except Exception:
        proc.kill()
        proc.wait()
        raise
    finally:
        listener.close()

    host = FakeHost(conn, proc)
    try:
        yield host
    finally:
        conn.close()
        if host.proc is not None:
            try:
                host.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                host.proc.kill()
                host.proc.wait()


def test_protocol_constants_match_the_contract():
    assert kernel_module.KERNEL_PROTOCOL == contract.KERNEL_PROTOCOL
    assert kernel_module.TRUNCATION_MARKER == contract.TRUNCATION_MARKER
    assert kernel_module.RLM_FORBIDDEN_IMPORTS == contract.RLM_FORBIDDEN_IMPORTS


def test_namespace_persists_across_cells(fake_host):
    fake_host.init()
    r1 = fake_host.exec("x = 1", 1)
    assert r1["error"] is None
    r2 = fake_host.exec("x", 2)
    assert r2["error"] is None
    assert r2["stdout"] == "1\n"
    fake_host.send({"op": "shutdown", "id": 3})
    assert fake_host.proc.wait(timeout=5) == 0


def test_output_over_limit_is_truncated_with_the_marker(fake_host):
    fake_host.init(max_output_chars=20)
    reply = fake_host.exec("print('x' * 100)", 1)
    assert reply["truncated"] is True
    assert reply["stdout"].startswith("x" * 20)
    assert contract.TRUNCATION_MARKER.format(limit=20, total=101) in reply["stdout"]
    fake_host.send({"op": "shutdown", "id": 2})
    fake_host.proc.wait(timeout=5)


def test_raising_cell_returns_error_and_kernel_keeps_running(fake_host):
    fake_host.init()
    bad = fake_host.exec("1 / 0", 1)
    assert bad["error"] is not None
    assert "ZeroDivisionError" in bad["error"]
    # the kernel's own frames never appear in the trimmed traceback
    assert "onoda_rlm_kernel.py" not in bad["error"]
    good = fake_host.exec("2 + 2", 2)
    assert good["error"] is None
    assert good["stdout"] == "4\n"
    fake_host.send({"op": "shutdown", "id": 3})
    fake_host.proc.wait(timeout=5)


def test_rlm_round_trips_a_host_reply(fake_host):
    fake_host.init()
    fake_host.send({"op": "exec", "id": 1, "code": "answer = rlm('hi', context='ctx')"})
    call = fake_host.recv()
    assert call == {"op": "call", "id": 0, "fn": "rlm", "args": {"prompt": "hi", "context": "ctx"}}
    fake_host.send({"op": "call.reply", "id": 0, "ok": True, "value": "42"})
    reply = fake_host.recv()
    assert reply == {
        "op": "exec.reply",
        "id": 1,
        "stdout": "",
        "stderr": "",
        "error": None,
        "truncated": False,
    }
    check = fake_host.exec("print(answer)", 2)
    assert check["stdout"] == "42\n"
    fake_host.send({"op": "shutdown", "id": 3})
    fake_host.proc.wait(timeout=5)


def test_rlm_refusal_raises_rlm_error_in_the_cell(fake_host):
    fake_host.init()
    fake_host.send({"op": "exec", "id": 1, "code": "rlm('hi')"})
    call = fake_host.recv()
    assert call["fn"] == "rlm"
    fake_host.send({"op": "call.reply", "id": 0, "ok": False, "error": "max-depth"})
    reply = fake_host.recv()
    assert reply["error"] is not None
    assert "RlmError" in reply["error"]
    assert "max-depth" in reply["error"]
    fake_host.send({"op": "shutdown", "id": 2})
    fake_host.proc.wait(timeout=5)


def test_rlm_requires_str_prompt_and_context(fake_host):
    fake_host.init()
    reply = fake_host.exec("rlm(123)", 1)
    assert reply["error"] is not None and "TypeError" in reply["error"]
    reply = fake_host.exec("rlm('hi', context=5)", 2)
    assert reply["error"] is not None and "TypeError" in reply["error"]
    fake_host.send({"op": "shutdown", "id": 3})
    fake_host.proc.wait(timeout=5)


def test_commit_state_refuses_non_json_value_without_a_call(fake_host):
    fake_host.init()
    reply = fake_host.exec("commit_state('x', object())", 1)
    assert reply["error"] is not None
    assert "TypeError" in reply["error"] or "ValueError" in reply["error"]
    # nothing else pending on the wire: the next exec's reply comes straight back
    ok = fake_host.exec("1 + 1", 2)
    assert ok["error"] is None
    fake_host.send({"op": "shutdown", "id": 3})
    fake_host.proc.wait(timeout=5)


def test_commit_state_blocks_until_acknowledged(fake_host):
    fake_host.init()
    fake_host.send({"op": "exec", "id": 1, "code": "digest = commit_state('n', {'a': 1})"})
    call = fake_host.recv()
    assert call["fn"] == "commit_state"
    assert call["args"] == {"name": "n", "json": contract.canonical_state_json({"a": 1})}
    fake_host.send({"op": "call.reply", "id": 0, "ok": True, "value": "sha256:" + "0" * 64})
    reply = fake_host.recv()
    assert reply["error"] is None
    check = fake_host.exec("print(digest)", 2)
    assert check["stdout"] == "sha256:" + "0" * 64 + "\n"
    fake_host.send({"op": "shutdown", "id": 3})
    fake_host.proc.wait(timeout=5)


def test_commit_state_rejects_a_bad_name(fake_host):
    fake_host.init()
    reply = fake_host.exec("commit_state('bad name!', 1)", 1)
    assert reply["error"] is not None
    assert "ValueError" in reply["error"]
    fake_host.send({"op": "shutdown", "id": 2})
    fake_host.proc.wait(timeout=5)


def test_helpers_component_is_available_in_the_namespace(fake_host):
    fake_host.init(helpers="def double(x):\n    return x * 2\n")
    reply = fake_host.exec("print(double(21))", 1)
    assert reply["error"] is None
    assert reply["stdout"] == "42\n"
    fake_host.send({"op": "shutdown", "id": 2})
    fake_host.proc.wait(timeout=5)


def test_unexpected_message_before_init_is_a_protocol_error(tmp_path):
    sock_path = tmp_path / "kernel.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(sock_path))
    listener.listen(1)
    listener.settimeout(10)
    proc = subprocess.Popen(
        [sys.executable, str(KERNEL_PATH), "--connect", str(sock_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        conn, _ = listener.accept()
        conn.settimeout(10)
        listener.close()
        buf = b""
        while b"\n" not in buf:
            buf += conn.recv(65536)
        conn.sendall((json.dumps({"op": "exec", "id": 0, "code": "1"}) + "\n").encode("utf-8"))
        assert proc.wait(timeout=5) != 0
    finally:
        proc.kill()
        proc.wait()


def test_eof_on_the_socket_exits_zero(fake_host):
    fake_host.init()
    fake_host._conn.close()
    assert fake_host.proc.wait(timeout=5) == 0
    fake_host.proc = None  # already reaped; avoid a double-wait in the fixture teardown


# ------------------------------------------------------------- static checks

_ALLOWED_TOP_IMPORTS = {
    "argparse",
    "ast",
    "contextlib",
    "io",
    "json",
    "os",
    "re",
    "socket",
    "sys",
    "traceback",
    "typing",
    "__future__",
}


def test_kernel_source_has_no_forbidden_imports_and_only_af_unix_sockets():
    source = KERNEL_PATH.read_text()
    tree = ast.parse(source)
    bad = contract.forbidden_imports(tree)
    assert bad == []
    # belt and suspenders: every top-level import name is one we expect, and
    # is never one of the forbidden modules regardless of how it is spelled.
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module.split(".")[0]]
        for name in names:
            assert name not in contract.RLM_FORBIDDEN_IMPORTS

    socket_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "socket"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "socket"
    ]
    assert socket_calls, "expected at least one socket.socket(...) call"
    for call in socket_calls:
        family_arg = call.args[0] if call.args else None
        assert (
            isinstance(family_arg, ast.Attribute)
            and family_arg.attr == "AF_UNIX"
            and isinstance(family_arg.value, ast.Name)
            and family_arg.value.id == "socket"
        ), "every socket opened by the kernel must be AF_UNIX"
