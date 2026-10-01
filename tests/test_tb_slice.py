"""Terminal-Bench slice adapter (A54): staging, the spend proxy, spool metrics.

Offline: no Harbor, no container, no provider. The proxy runs against a
local fake upstream whose responses are synthetic.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_pi_ingest import SpoolBuilder

from onoda.adapters.pi.contract import ENV_SPOOL_KEY, RECORDER_EXTENSION, SOL_PI_ENTRY
from onoda.adapters.tb.container import PLACEHOLDER_CREDENTIAL, build_container_launch
from onoda.adapters.tb.metrics import run_metrics
from onoda.adapters.tb.proxy import Prices, SpendProxy, usage_from_body
from onoda.errors import CapabilityError, IntegrityError, OnodaError

SECRET = "sk-test-never-leaves-the-proxy"
LIVE = dict(
    instruction="Fix the build.",
    provider="openrouter",
    model="vendor/model",
    base_url="http://host.docker.internal:1/t/tok/api/v1",
    credential_env="OPENROUTER_API_KEY",
    max_requests=40,
    max_cost_usd="0.20",
)


# ---------------------------------------------------------------- staging


def test_baseline_arm_loads_only_the_recorder():
    launch = build_container_launch(variant_id="S1", **LIVE)
    extensions = [launch.argv[i + 1] for i, a in enumerate(launch.argv) if a == "-e"]
    assert [e.rsplit("/", 1)[1] for e in extensions] == [RECORDER_EXTENSION]
    assert launch.argv[launch.argv.index("--tools") + 1] == "read,bash,edit,write"
    assert not any(path.endswith("sol-pi.json") for path in launch.files)
    assert launch.run_info["sol_pi_commit"] is None and launch.run_info["scripted"] is False


def test_full_stack_arm_loads_sol_pi_before_the_recorder():
    launch = build_container_launch(variant_id="S3", reducer=("openrouter", "vendor/model"), **LIVE)
    extensions = [launch.argv[i + 1] for i, a in enumerate(launch.argv) if a == "-e"]
    assert extensions[0].endswith(str(SOL_PI_ENTRY)) and extensions[-1].endswith(RECORDER_EXTENSION)
    tools = launch.argv[launch.argv.index("--tools") + 1].split(",")
    assert tools == ["read", "bash", "edit", "write", "obs_recall", "update_plan"]
    (cfg,) = (json.loads(t) for p, t in launch.files.items() if p.endswith("sol-pi.json"))
    assert cfg["evidencePreservingReducerModel"] == "vendor/model"
    assert all(cfg[m] for m in launch.run_info["mechanisms"])


def test_launch_carries_caps_and_no_credential():
    launch = build_container_launch(variant_id="S1", **LIVE)
    (policy,) = (json.loads(t) for p, t in launch.files.items() if p.endswith("policy.json"))
    assert policy["max_requests"] == 40 and policy["max_cost_usd"] == "0.20"
    assert launch.env["OPENROUTER_API_KEY"] == PLACEHOLDER_CREDENTIAL
    (models,) = (json.loads(t) for p, t in launch.files.items() if p.endswith("models.json"))
    assert models == {"providers": {"openrouter": {"baseUrl": LIVE["base_url"]}}}
    assert launch.env[ENV_SPOOL_KEY] == launch.key_path and len(launch.spool_key) == 32
    assert launch.spool_key.hex() not in repr(launch) + json.dumps(launch.files)


@pytest.mark.parametrize(
    ("override", "error"),
    [
        ({"variant_id": "S3"}, CapabilityError),  # the reducer arm without a reducer model
        ({"variant_id": "S1r"}, CapabilityError),
        ({"variant_id": "S1", "max_cost_usd": ""}, CapabilityError),
        ({"variant_id": "S1", "max_requests": 0}, CapabilityError),
        ({"variant_id": "S1", "base_url": "host:1"}, CapabilityError),
        ({"variant_id": "S1", "instruction": "-p injected"}, OnodaError),
    ],
)
def test_unresolved_settings_fail_closed(override, error):
    with pytest.raises(error):
        build_container_launch(**{**LIVE, **override})


# ------------------------------------------------------------------ proxy


class _Upstream(BaseHTTPRequestHandler):
    """A synthetic OpenAI-style endpoint: one SSE completion with a usage chunk."""

    seen: list[dict] = []
    usage: dict = {"prompt_tokens": 1000, "completion_tokens": 100, "cost": 0.06}

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).seen.append(
            {"path": self.path, "auth": self.headers["Authorization"], "body": body}
        )
        chunks = [{"choices": [{"delta": {"content": "ok"}}]}, {"choices": [], "usage": self.usage}]
        data = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data.encode())

    def log_message(self, *args):
        return


@pytest.fixture
def upstream():
    _Upstream.seen = []
    _Upstream.usage = {"prompt_tokens": 1000, "completion_tokens": 100, "cost": 0.06}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _proxy(upstream: str, tmp_path, **kwargs) -> tuple[SpendProxy, int]:
    proxy = SpendProxy(
        credential=SECRET,
        total_cap_usd=kwargs.pop("total", Decimal("1")),
        upstream=upstream,
        ledger_path=tmp_path / "ledger.json",
        **kwargs,
    )
    return proxy, proxy.start()


def _post(port: int, token: str, path: str = "api/v1/chat/completions") -> tuple[int, bytes]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/t/{token}/{path}",
        data=b'{"model":"m","stream":true}',
        headers={
            "Authorization": f"Bearer {PLACEHOLDER_CREDENTIAL}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_proxy_swaps_in_the_credential_and_charges_the_reported_cost(upstream, tmp_path):
    proxy, port = _proxy(upstream, tmp_path)
    token = proxy.open_run("task/S1", Decimal("0.10"))
    status, body = _post(port, token)
    proxy.stop()
    assert status == 200 and b"[DONE]" in body
    (seen,) = _Upstream.seen
    assert seen["auth"] == f"Bearer {SECRET}" and seen["path"] == "/api/v1/chat/completions"
    (run,) = proxy.snapshot()["runs"]
    assert run["requests"] == 1 and run["cost_usd"] == "0.06" and run["prompt_tokens"] == 1000
    ledger = (tmp_path / "ledger.json").read_text()
    assert json.loads(ledger)["spent_usd"] == "0.06"
    assert SECRET not in ledger and token not in ledger


def test_proxy_refuses_past_the_run_cap_and_after_close(upstream, tmp_path):
    proxy, port = _proxy(upstream, tmp_path)
    token = proxy.open_run("task/S1", Decimal("0.10"))
    assert [_post(port, token)[0] for _ in range(3)] == [200, 200, 402]  # 0.12 >= 0.10
    other = proxy.open_run("task/S3", Decimal("0.10"))
    assert _post(port, other)[0] == 200
    proxy.close_run(other)
    assert _post(port, other)[0] == 402
    proxy.stop()
    assert len(_Upstream.seen) == 3 and proxy.spent() == Decimal("0.18")


def test_proxy_refuses_past_the_total_cap_and_unknown_paths(upstream, tmp_path):
    proxy, port = _proxy(upstream, tmp_path, total=Decimal("0.05"))
    a, b = proxy.open_run("t/S1", Decimal("1")), proxy.open_run("t/S3", Decimal("1"))
    assert _post(port, a)[0] == 200
    assert _post(port, b)[0] == 402  # the slice is spent, whichever run asks
    assert _post(port, "not-a-token")[0] == 403
    assert _post(port, a, path="v2/elsewhere")[0] == 404
    proxy.stop()
    assert len(_Upstream.seen) == 1


def test_unpriced_response_fails_closed_unless_prices_are_given(upstream, tmp_path):
    _Upstream.usage = {"prompt_tokens": 1_000_000, "completion_tokens": 0}  # no cost figure
    proxy, port = _proxy(upstream, tmp_path)
    token = proxy.open_run("t/S1", Decimal("1"))
    assert [_post(port, token)[0] for _ in range(2)] == [200, 402]
    proxy.stop()
    priced, port = _proxy(
        upstream, tmp_path, prices=Prices(Decimal("0.05"), Decimal("0.1"), Decimal("0.01"))
    )
    token = priced.open_run("t/S1", Decimal("1"))
    assert _post(port, token)[0] == 200
    priced.stop()
    (run,) = priced.snapshot()["runs"]
    assert run["cost_usd"] == "0.05" and run["cost_estimated"] == 1


def test_usage_is_read_from_json_and_sse_bodies():
    assert usage_from_body("application/json", b'{"usage":{"cost":1}}') == {"cost": 1}
    assert usage_from_body("text/event-stream", b"data: {}\n\ndata: [DONE]\n\n") is None
    assert usage_from_body("application/json", b"not json") is None


# ---------------------------------------------------------------- metrics


def _spool(tmp_path, key: bytes | None):
    sb = SpoolBuilder(tmp_path).start()
    sb.call("c1", "bash", {"command": "make test"})
    sb.turn({"type": "text", "text": "receipt"}, source="model_runtime", purpose="reducer")
    receipt = {"evidencePreservingReducer": {"sourceBytes": 9000, "receiptBytes": 700}}
    sb.line(
        "tool.result",
        tool_call_id="c1",
        tool_name="bash",
        stage="visible",
        content=sb.put([{"type": "text", "text": "sol_pi_evidence_receipt_v1"}]),
        details=sb.put(receipt),
        is_error=False,
    )
    sb.call("c2", "edit", {"path": "a.c", "edits": [], "then_run": {"command": "make"}})
    sb.result("c2", "edit", "ok\n[then_run:succeeded]")
    sb.call("c3", "write", {"path": "b.c", "content": ""})
    sb.result("c3", "write", "ok")
    sb.call("c4", "bash", {"command": "sbatch x"}, decision="blocked", reason="not allowed")
    sb.line("compaction", phase="before")
    sb.turn({"type": "text", "text": "summary"}, source="model_runtime", purpose="summary")
    sb.line("compaction", phase="done", details=sb.put({"summary": "s"}))
    sb.end("budget-exhausted")
    return sb.write(key)


def test_metrics_count_each_mechanism_from_the_spool(tmp_path):
    key = b"k" * 32
    m = run_metrics(_spool(tmp_path, key), key, tmp_path / "sessions")
    assert m["spool_verified"] and m["end_status"] == "budget-exhausted"
    assert m["requests"] == {"agent": 4, "reducer": 1, "summary": 1}
    assert m["reducer"]["results_reduced"] == 1 and m["reducer"]["source_bytes"] == 9000
    assert m["compaction"] == {"summary_requests": 1, "phases": {"before": 1, "done": 1}}
    assert m["action_fusion"] == {"fused_calls": 1}
    assert m["tool_calls"] == {"bash": 1, "edit": 1, "write": 1}
    assert m["tool_calls_blocked"] == {"bash": 1}
    assert m["tokens_total"]["input"] == 60 and m["cost_usd_total"] == "0"


def test_metrics_refuse_a_spool_under_another_key(tmp_path):
    spool = _spool(tmp_path, b"k" * 32)
    with pytest.raises(IntegrityError):
        run_metrics(spool, b"x" * 32, tmp_path / "sessions")
