"""A host-side forwarding proxy that holds the provider credential and a spend cap.

A Pi process inside a task container shares that container with the agent's
own shell, so any credential it held (environment, file, `/proc`) would be
readable by a tool call and could end up in a spooled tool result. Instead
the container is given only this proxy's URL with a per-run random token in
the path; the proxy adds the real `Authorization` header on the way out.

It is also the hard spend limit for a whole slice. Pi's own `max_cost_usd`
(the recorder's gateway) works from Pi's price table inside a process the
agent could kill; this ledger sums the cost the provider reports for every
response and refuses further requests once a run's cap or the total cap is
reached, whatever is asking.

Stdlib only. The credential lives in this object's memory: it is never
logged, never part of the ledger, and never sent anywhere but `upstream`.
"""

from __future__ import annotations

import http.client
import json
import os
import secrets
import threading
from dataclasses import dataclass, field
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from onoda.errors import OnodaError

# Never forwarded in either direction: connection management is per hop, the
# client's placeholder credential is replaced, and bodies are re-framed.
_HOP_HEADERS = frozenset(
    {
        "host",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "authorization",
        "accept-encoding",
    }
)
_MAX_REQUEST_BYTES = 64 * 1024 * 1024
_MAX_JSON_RESPONSE_BYTES = 16 * 1024 * 1024


@dataclass
class RunLedger:
    """What one token spent. Costs are exact decimals of the provider's figures."""

    label: str
    cap_usd: Decimal
    open: bool = True
    requests: int = 0  # forwarded upstream
    refused: int = 0  # refused here (cap reached, closed token)
    cost_usd: Decimal = Decimal(0)
    cost_estimated: int = 0  # responses priced from the price table (no provider cost)
    cost_unknown: int = 0  # successful responses that could not be priced at all
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0

    def as_json(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "cap_usd": str(self.cap_usd),
            "open": self.open,
            "requests": self.requests,
            "refused": self.refused,
            "cost_usd": str(self.cost_usd),
            "cost_estimated": self.cost_estimated,
            "cost_unknown": self.cost_unknown,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
        }


@dataclass(frozen=True)
class Prices:
    """USD per million tokens, used only when a response carries no cost."""

    input: Decimal
    output: Decimal
    cache_read: Decimal

    def estimate(self, prompt: int, completion: int, cached: int) -> Decimal:
        fresh = max(0, prompt - cached)
        total = fresh * self.input + cached * self.cache_read + completion * self.output
        return total / Decimal(1_000_000)


def usage_from_body(content_type: str, body: bytes) -> dict[str, Any] | None:
    """The last `usage` object of an OpenAI-style response (JSON or SSE), if any."""
    if "text/event-stream" not in content_type:
        try:
            data = json.loads(body)
        except ValueError:
            return None
        usage = data.get("usage") if isinstance(data, dict) else None
        return usage if isinstance(usage, dict) else None
    found: dict[str, Any] | None = None
    for line in body.split(b"\n"):
        if not line.startswith(b"data:") or b'"usage"' not in line:
            continue
        try:
            data = json.loads(line[5:])
        except ValueError:
            continue
        usage = data.get("usage") if isinstance(data, dict) else None
        if isinstance(usage, dict):
            found = usage
    return found


# no credential enters the task container; the host proxy adds the key and caps spend
@dataclass
class SpendProxy:
    """Forward `/t/<token>/api/v1/...` to `upstream` with the real credential."""

    credential: str = field(repr=False)
    total_cap_usd: Decimal
    upstream: str = "https://openrouter.ai"
    prices: Prices | None = None
    ledger_path: Path | None = None  # rewritten after every change; holds no credential
    upstream_timeout_s: float = 600.0

    def __post_init__(self) -> None:
        if not self.credential:
            raise OnodaError("the spend proxy needs a credential")
        self._lock = threading.Lock()
        self._runs: dict[str, RunLedger] = {}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ lifecycle

    def start(self, port: int = 0) -> int:
        """Listen on loopback (containers reach it as `host.docker.internal`)."""
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 (http.server's name)
                proxy._handle(self)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return  # paths carry run tokens; the ledger is the log

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self._server.server_address[1]

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # --------------------------------------------------------------- ledger

    def open_run(self, label: str, cap_usd: Decimal) -> str:
        """Register a run; returns the token that names it in the URL path."""
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._runs[token] = RunLedger(label=label, cap_usd=cap_usd)
            self._persist()
        return token

    def close_run(self, token: str) -> RunLedger:
        """Stop serving `token`: a leftover process of the run cannot keep spending."""
        with self._lock:
            run = self._runs[token]
            run.open = False
            self._persist()
            return run

    def spent(self) -> Decimal:
        with self._lock:
            return sum((r.cost_usd for r in self._runs.values()), Decimal(0))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot()

    def _snapshot(self) -> dict[str, Any]:
        runs = [r.as_json() for r in self._runs.values()]
        return {
            "schema": "onoda.tb-spend-ledger/1",
            "total_cap_usd": str(self.total_cap_usd),
            "spent_usd": str(sum((r.cost_usd for r in self._runs.values()), Decimal(0))),
            "runs": runs,
        }

    def _persist(self) -> None:
        if self.ledger_path is None:
            return
        tmp = self.ledger_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._snapshot(), indent=1, sort_keys=True))
        os.replace(tmp, self.ledger_path)

    def _admit(self, token: str) -> tuple[RunLedger | None, int, str]:
        """(run, status, reason): status 0 admits the request and counts it."""
        with self._lock:
            run = self._runs.get(token)
            if run is None:
                return None, 403, "unknown run token"
            reason = ""
            total = sum((r.cost_usd for r in self._runs.values()), Decimal(0))
            if not run.open:
                reason = "run is closed"
            elif run.cost_unknown:
                reason = "a response of this run could not be priced; the cap fails closed"
            elif run.cost_usd >= run.cap_usd:
                reason = f"run cost cap reached ({run.cost_usd} >= {run.cap_usd} USD)"
            elif total >= self.total_cap_usd:
                reason = f"total cost cap reached ({total} >= {self.total_cap_usd} USD)"
            if reason:
                run.refused += 1
                self._persist()
                return run, 402, reason
            run.requests += 1
            self._persist()
            return run, 0, ""

    def _charge(self, run: RunLedger, usage: dict[str, Any] | None) -> None:
        with self._lock:
            if usage is None:
                run.cost_unknown += 1
                self._persist()
                return
            prompt = int(usage.get("prompt_tokens") or 0)
            completion = int(usage.get("completion_tokens") or 0)
            details = usage.get("prompt_tokens_details")
            cached = int(details.get("cached_tokens") or 0) if isinstance(details, dict) else 0
            run.prompt_tokens += prompt
            run.completion_tokens += completion
            run.cached_tokens += cached
            cost = usage.get("cost")
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                run.cost_usd += Decimal(str(cost))
            elif self.prices is not None:
                run.cost_usd += self.prices.estimate(prompt, completion, cached)
                run.cost_estimated += 1
            else:
                run.cost_unknown += 1
            self._persist()

    # -------------------------------------------------------------- request

    def _handle(self, h: BaseHTTPRequestHandler) -> None:
        parts = h.path.split("/", 3)  # "", "t", token, rest
        if len(parts) != 4 or parts[1] != "t" or not parts[3].startswith("api/v1/"):
            return _refuse(h, 404, "not a proxied path")
        token, rest = parts[2], parts[3]
        length = int(h.headers.get("Content-Length") or 0)
        if not 0 <= length <= _MAX_REQUEST_BYTES:
            return _refuse(h, 413, "request body too large")
        body = h.rfile.read(length)
        run, status, reason = self._admit(token)
        if run is None or status:
            return _refuse(h, status, reason)

        target = urlsplit(self.upstream)
        conn_cls = (
            http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
        )
        conn = conn_cls(target.netloc, timeout=self.upstream_timeout_s)
        headers = {k: v for k, v in h.headers.items() if k.lower() not in _HOP_HEADERS}
        headers["Authorization"] = f"Bearer {self.credential}"
        headers["Accept-Encoding"] = "identity"  # the usage figures are read from the body
        try:
            conn.request("POST", f"{target.path.rstrip('/')}/{rest}", body=body, headers=headers)
            resp = conn.getresponse()
        except OSError as exc:
            conn.close()
            return _refuse(h, 502, f"upstream request failed: {type(exc).__name__}")

        content_type = resp.getheader("Content-Type") or ""
        h.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in _HOP_HEADERS:
                h.send_header(k, v)
        h.send_header("Transfer-Encoding", "chunked")
        h.send_header("Connection", "close")
        h.end_headers()
        seen = bytearray()
        charged = False
        try:
            while chunk := resp.read1(65536):
                if len(seen) < _MAX_JSON_RESPONSE_BYTES:
                    seen += chunk
                h.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                h.wfile.flush()
            # Charge before the response ends: a client that has the whole
            # response may ask again at once, and that request must see this cost.
            charged = True
            if resp.status == 200:
                self._charge(run, usage_from_body(content_type, bytes(seen)))
            h.wfile.write(b"0\r\n\r\n")
            h.wfile.flush()
        except OSError:
            pass  # the client went away; what was generated is still charged below
        finally:
            conn.close()
            h.close_connection = True
        if not charged and resp.status == 200:
            self._charge(run, usage_from_body(content_type, bytes(seen)))


def _refuse(h: BaseHTTPRequestHandler, status: int, reason: str) -> None:
    body = json.dumps({"error": {"message": f"onoda spend proxy: {reason}", "code": status}})
    data = body.encode()
    h.send_response(status)
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Connection", "close")
    h.end_headers()
    h.wfile.write(data)
    h.close_connection = True
