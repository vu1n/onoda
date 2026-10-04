#!/usr/bin/env python3
"""The RLM kernel: one persistent Python namespace per Pi session.

Launched by `onoda-rlm.ts` as `python onoda_rlm_kernel.py --connect <socket>`,
it connects to the host's AF_UNIX stream socket, announces `ready`, execs the
candidate's helper library into its namespace on `init`, then serves `exec`
requests against that namespace for the life of the Pi process. Two calls
back into the host — `rlm()` and `commit_state()` — are exposed inside the
namespace and block the running cell for the host's reply.

This process is deliberately stdlib-only and network-free: it never imports
`onoda` or pydantic (the wire shapes are mirrored by hand from
`src/onoda/adapters/pi/contract.py`, "RLM (M4)" section, and
`pi/extensions/lib/rlm-protocol.ts`), never imports pickle, marshal, shelve,
subprocess, urllib, http or ssl, and opens no socket other than the AF_UNIX
one it is told to connect to. Untrusted candidate text (helpers, cell code)
runs in-process with the kernel's own privileges; the sandboxing boundary is
the harness around this process, not this file.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import io
import json
import os
import re
import socket
import sys
import traceback
from typing import Any

# Mirrors contract.py's KERNEL_PROTOCOL / TRUNCATION_MARKER / RLM_FORBIDDEN_IMPORTS
# field-for-field; tests assert equality against the contract's own constants.
KERNEL_PROTOCOL = "onoda.rlm-kernel/1"
TRUNCATION_MARKER = "\n[onoda-rlm: output truncated at {limit} of {total} chars]"
RLM_FORBIDDEN_IMPORTS = ("pickle", "marshal", "shelve", "subprocess", "urllib", "http", "ssl")

# A cell's compiled code carries this filename so a raised traceback can be
# trimmed to frames of the cell (never this module's own call stack).
_CELL_FILENAME = "<cell>"

_STATE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")


class RlmError(Exception):
    """Raised inside a cell when the host refuses an `rlm()` or `commit_state()` call."""


class _ProtocolError(Exception):
    """A framing or ordering violation from the host: fatal to this process."""


class _Connection:
    """One JSON-Lines message per line (UTF-8, `\\n`-terminated) over a stream socket."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buf = b""

    def send(self, message: dict[str, Any]) -> None:
        line = json.dumps(message, separators=(",", ":")) + "\n"
        self._sock.sendall(line.encode("utf-8"))

    def recv(self) -> dict[str, Any] | None:
        """The next decoded message, or None at a clean EOF."""
        while b"\n" not in self._buf:
            chunk = self._sock.recv(65536)
            if not chunk:
                if self._buf.strip():
                    raise _ProtocolError("connection closed mid-message")
                return None
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line.decode("utf-8"))


def _cell_traceback(exc_type: type, exc_value: BaseException, tb: Any) -> str:
    """`traceback.format_exception`, dropping frames outside the cell's own code."""
    while tb is not None and tb.tb_frame.f_code.co_filename != _CELL_FILENAME:
        tb = tb.tb_next
    return "".join(traceback.format_exception(exc_type, exc_value, tb))


class Kernel:
    """The persistent namespace plus the `rlm`/`commit_state` calls exposed inside it."""

    def __init__(self, conn: _Connection, max_output_chars: int) -> None:
        self._conn = conn
        self.max_output_chars = max_output_chars
        self._next_call_id = 0
        self._namespace: dict[str, Any] = {
            "RlmError": RlmError,
            "rlm": self._rlm,
            "commit_state": self._commit_state,
        }

    # ------------------------------------------------------------- host calls

    def _call(self, fn: str, args: dict[str, Any]) -> dict[str, Any]:
        """Send one `call`, then block reading messages until its matching reply."""
        call_id = self._next_call_id
        self._next_call_id += 1
        self._conn.send({"op": "call", "id": call_id, "fn": fn, "args": args})
        while True:
            msg = self._conn.recv()
            if msg is None:
                raise _ProtocolError("host closed the connection with a call pending")
            if msg.get("op") != "call.reply" or msg.get("id") != call_id:
                raise _ProtocolError(f"expected call.reply for id {call_id}, got {msg!r}")
            return msg

    def _rlm(self, prompt: str, context: str | None = None) -> str:
        if not isinstance(prompt, str):
            raise TypeError("rlm(prompt) requires a str prompt")
        if context is not None and not isinstance(context, str):
            raise TypeError("rlm(context=...) requires a str or None context")
        reply = self._call("rlm", {"prompt": prompt, "context": context})
        if reply["ok"]:
            return reply["value"]
        raise RlmError(reply.get("error"))

    # Context: doc://onoda/record-integrity@0003#immutable-single-writer-records — kernel state reaches the record only as committed JSON, never pickle
    def _commit_state(self, name: str, value: Any) -> str:
        # allow_nan=False also rejects float("inf"); the dump happens before the
        # name is even checked, so a bad value never reaches the host.
        text = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if not isinstance(name, str) or not _STATE_NAME_RE.match(name):
            raise ValueError(f"commit_state name is invalid: {name!r}")
        reply = self._call("commit_state", {"name": name, "json": text})
        if reply["ok"]:
            return reply["value"]
        raise RlmError(reply.get("error"))

    # --------------------------------------------------------------- exec

    def _cap(self, text: str) -> tuple[str, bool]:
        if len(text) <= self.max_output_chars:
            return text, False
        limit = self.max_output_chars
        marker = TRUNCATION_MARKER.format(limit=limit, total=len(text))
        return text[:limit] + marker, True

    def exec_cell(self, code: str) -> dict[str, Any]:
        """Run one cell in the persistent namespace; never raises (errors come back as data)."""
        stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
        error_text: str | None = None
        try:
            tree = ast.parse(code, mode="exec")
            trailing_expr = None
            if tree.body and isinstance(tree.body[-1], ast.Expr):
                trailing_expr = ast.Expression(tree.body.pop().value)
            body = compile(tree, _CELL_FILENAME, "exec")
            with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
                exec(body, self._namespace)
                if trailing_expr is not None:
                    value = eval(  # noqa: S307 (trusted cell namespace, not attacker input parsing)
                        compile(trailing_expr, _CELL_FILENAME, "eval"), self._namespace
                    )
                    if value is not None:
                        stdout_buf.write(repr(value) + "\n")
        except _ProtocolError:
            raise
        except BaseException:  # noqa: BLE001 - any cell exception becomes an error reply
            error_text = _cell_traceback(*sys.exc_info())
        stdout, out_truncated = self._cap(stdout_buf.getvalue())
        stderr, err_truncated = self._cap(stderr_buf.getvalue())
        return {
            "stdout": stdout,
            "stderr": stderr,
            "error": error_text,
            "truncated": out_truncated or err_truncated,
        }


def _serve(conn: _Connection, kernel: Kernel) -> int:
    while True:
        msg = conn.recv()
        if msg is None:
            return 0
        op = msg.get("op")
        if op == "exec":
            reply = kernel.exec_cell(msg["code"])
            conn.send({"op": "exec.reply", "id": msg["id"], **reply})
        elif op == "shutdown":
            return 0
        else:
            raise _ProtocolError(f"unexpected message while serving: {msg!r}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connect", required=True, help="AF_UNIX socket path to connect to")
    args = parser.parse_args(argv)

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(args.connect)
    conn = _Connection(sock)
    try:
        conn.send({"op": "ready", "protocol": KERNEL_PROTOCOL, "pid": os.getpid()})

        first = conn.recv()
        if first is None:
            return 0
        if first.get("op") != "init":
            sys.stderr.write(f"onoda-rlm-kernel: protocol error: expected init, got {first!r}\n")
            return 1

        kernel = Kernel(conn, max_output_chars=first["max_output_chars"])
        reply = kernel.exec_cell(first["helpers"])
        conn.send({"op": "exec.reply", "id": first["id"], **reply})

        return _serve(conn, kernel)
    except _ProtocolError as exc:
        sys.stderr.write(f"onoda-rlm-kernel: protocol error: {exc}\n")
        return 1
    finally:
        sock.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
