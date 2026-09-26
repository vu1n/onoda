"""Tests for `onoda.adapters.pi.runtime`: doctor, build_launch, launch.

Everything here is offline and deterministic; the one test that touches the
real pinned Pi process is marked `requires_pi` (a smoke check that
`doctor()` of the real runtime is ok).
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from conftest import requires_pi

from onoda.adapters.pi.contract import (
    ENV_POLICY,
    ENV_RUN_INFO,
    ENV_SCRIPT,
    ENV_SPOOL,
    ENV_TASK,
    EXTENSIONS_DIR,
    PI_VERSION,
    RECORDER_EXTENSION,
    RUNTIME_DIR,
    SCRIPTED_EXTENSION,
    SCRIPTED_MODEL,
    SCRIPTED_PROVIDER,
    SCRIPTED_REDUCER_MODEL,
    SOL_PI_COMMIT,
    SOL_PI_ENTRY,
    TASK_EXTENSION,
    GatewayPolicy,
    Script,
    ScriptTurn,
    TextBlock,
)
from onoda.adapters.pi.runtime import LiveSettings, build_launch, doctor, launch
from onoda.errors import CapabilityError, OnodaError

SCRIPT = Script(source="test", turns=(ScriptTurn(content=(TextBlock(type="text", text="done"),)),))


def make_bundle(tmp_path: Path, *, case_id: str = "c1", ladder: bool = False) -> Path:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    task = {
        "schema": "onoda.pi-task/1",
        "case_id": case_id,
        "instruction": "do the thing",
        "input_name": "job.inp",
        "input_sha256": "a" * 64,
        "output_name": "job.out",
        "output_sha256": "b" * 64,
        "output_provenance": "acquired",
        "review_status": "reviewed",
    }
    if ladder:
        task["ladder_sha256"] = "c" * 64
        (bundle_dir / "ladder.md").write_text("advisory ladder")
    (bundle_dir / "task.json").write_text(json.dumps(task))
    return bundle_dir


# --------------------------------------------------------------------- doctor


def write_fake_runtime(tmp_path: Path, *, pi_version: str, sol_pi_commit: str) -> Path:
    runtime_dir = tmp_path / "pi"
    pkg_dir = runtime_dir / "node_modules/@earendil-works/pi-coding-agent"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "package.json").write_text(json.dumps({"version": pi_version}))
    lock = {
        "packages": {
            "node_modules/sol-pi": {
                "resolved": f"git+ssh://git@github.com/NVlabs/SoL-Pi.git#{sol_pi_commit}"
            }
        }
    }
    (runtime_dir / "package-lock.json").write_text(json.dumps(lock))
    return runtime_dir


def test_doctor_ok_on_matching_fake_runtime(tmp_path):
    runtime_dir = write_fake_runtime(tmp_path, pi_version=PI_VERSION, sol_pi_commit=SOL_PI_COMMIT)
    result = doctor(runtime_dir)
    assert result["ok"] is True
    assert result["pi_version"] == PI_VERSION
    assert result["sol_pi_commit"] == SOL_PI_COMMIT
    assert result["problems"] == []


def test_doctor_flags_pi_version_mismatch(tmp_path):
    runtime_dir = write_fake_runtime(tmp_path, pi_version="0.1.0", sol_pi_commit=SOL_PI_COMMIT)
    result = doctor(runtime_dir)
    assert result["ok"] is False
    assert any("0.1.0" in p and PI_VERSION in p for p in result["problems"])


def test_doctor_flags_sol_pi_commit_mismatch(tmp_path):
    runtime_dir = write_fake_runtime(tmp_path, pi_version=PI_VERSION, sol_pi_commit="f" * 40)
    result = doctor(runtime_dir)
    assert result["ok"] is False
    assert any("f" * 40 in p for p in result["problems"])


def test_doctor_flags_missing_install(tmp_path):
    result = doctor(tmp_path / "nowhere")
    assert result["ok"] is False
    assert len(result["problems"]) >= 2


@requires_pi
def test_doctor_real_runtime_is_ok():
    assert doctor()["ok"] is True


# ---------------------------------------------------------------- LiveSettings


def test_live_settings_validate_ok(monkeypatch):
    monkeypatch.setenv("MY_KEY", "x")
    LiveSettings(
        provider="anthropic",
        model="claude",
        credential_env="MY_KEY",
        max_requests=10,
        max_cost_usd="1.00",
    ).validate()


@pytest.mark.parametrize(
    "field", ["provider", "model", "credential_env", "max_requests", "max_cost_usd"]
)
def test_live_settings_refuses_each_missing_setting(monkeypatch, field):
    monkeypatch.setenv("MY_KEY", "x")
    kwargs = dict(
        provider="anthropic",
        model="claude",
        credential_env="MY_KEY",
        max_requests=10,
        max_cost_usd="1.00",
    )
    kwargs[field] = None
    with pytest.raises(CapabilityError):
        LiveSettings(**kwargs).validate()


def test_live_settings_refuses_absent_credential_env(monkeypatch):
    monkeypatch.delenv("NOT_SET_KEY", raising=False)
    with pytest.raises(CapabilityError):
        LiveSettings(
            provider="anthropic",
            model="claude",
            credential_env="NOT_SET_KEY",
            max_requests=10,
            max_cost_usd="1.00",
        ).validate()


# ----------------------------------------------------------------- build_launch


def test_argv_load_order_s3_scripted(tmp_path):
    bundle_dir = make_bundle(tmp_path, ladder=True)
    launch_obj = build_launch(
        variant_id="S3",
        bundle_dir=bundle_dir,
        work_dir=tmp_path / "work",
        script=SCRIPT,
        max_requests=5,
        max_acquisitions=1,
    )
    dash_e = [launch_obj.argv[i + 1] for i, a in enumerate(launch_obj.argv) if a == "-e"]
    assert dash_e == [
        str(EXTENSIONS_DIR / TASK_EXTENSION),
        str(RUNTIME_DIR / SOL_PI_ENTRY),
        str(EXTENSIONS_DIR / SCRIPTED_EXTENSION),
        str(EXTENSIONS_DIR / RECORDER_EXTENSION),
    ]
    assert launch_obj.argv[0] == str(RUNTIME_DIR / "node_modules/.bin/pi")
    assert "--provider" in launch_obj.argv
    assert launch_obj.argv[launch_obj.argv.index("--provider") + 1] == SCRIPTED_PROVIDER
    assert launch_obj.argv[launch_obj.argv.index("--model") + 1] == SCRIPTED_MODEL
    assert launch_obj.argv[-2:] == ("-p", "do the thing")


def test_s0_omits_sol_pi_and_scripted_extension_in_live_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_KEY", "x")
    bundle_dir = make_bundle(tmp_path, ladder=False)
    launch_obj = build_launch(
        variant_id="S0",
        bundle_dir=bundle_dir,
        work_dir=tmp_path / "work",
        live=LiveSettings(
            provider="anthropic",
            model="claude-x",
            credential_env="MY_KEY",
            max_requests=5,
            max_cost_usd="2.50",
        ),
        max_requests=5,
        max_acquisitions=0,
    )
    dash_e = [launch_obj.argv[i + 1] for i, a in enumerate(launch_obj.argv) if a == "-e"]
    assert dash_e == [
        str(EXTENSIONS_DIR / TASK_EXTENSION),
        str(EXTENSIONS_DIR / RECORDER_EXTENSION),
    ]
    assert not (launch_obj.agent_dir / "sol-pi.json").exists()
    run_info = json.loads(launch_obj.run_info_path.read_text())
    assert run_info["sol_pi_commit"] is None
    assert run_info["scripted"] is False
    assert run_info["max_cost_usd"] == "2.50"


def test_sol_pi_json_written_per_variant(tmp_path):
    bundle_dir = make_bundle(tmp_path, ladder=True)
    launch_obj = build_launch(
        variant_id="S2b",
        bundle_dir=bundle_dir,
        work_dir=tmp_path / "work",
        script=SCRIPT,
        max_requests=5,
        max_acquisitions=1,
    )
    cfg = json.loads((launch_obj.agent_dir / "sol-pi.json").read_text())
    assert cfg["evidencePreservingReducer"] is True
    assert cfg["observationPack"] is False
    assert cfg["evidencePreservingReducerProvider"] == SCRIPTED_PROVIDER
    assert cfg["evidencePreservingReducerModel"] == SCRIPTED_REDUCER_MODEL
    run_info = json.loads(launch_obj.run_info_path.read_text())
    assert run_info["sol_pi_commit"] == SOL_PI_COMMIT
    assert run_info["mechanisms"] == ["evidencePreservingReducer"]


def test_ladder_variant_refuses_without_ladder(tmp_path):
    bundle_dir = make_bundle(tmp_path, ladder=False)
    with pytest.raises(CapabilityError):
        build_launch(
            variant_id="S1",
            bundle_dir=bundle_dir,
            work_dir=tmp_path / "work",
            script=SCRIPT,
            max_requests=5,
            max_acquisitions=1,
        )


def test_rlm_variant_always_refuses(tmp_path):
    bundle_dir = make_bundle(tmp_path, ladder=True)
    with pytest.raises(CapabilityError):
        build_launch(
            variant_id="S1r",
            bundle_dir=bundle_dir,
            work_dir=tmp_path / "work",
            script=SCRIPT,
            max_requests=5,
            max_acquisitions=1,
        )


def test_live_refused_for_missing_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_KEY", "x")
    bundle_dir = make_bundle(tmp_path, ladder=False)
    with pytest.raises(CapabilityError):
        build_launch(
            variant_id="S0",
            bundle_dir=bundle_dir,
            work_dir=tmp_path / "work",
            live=LiveSettings(
                provider="anthropic",
                model=None,
                credential_env="MY_KEY",
                max_requests=5,
                max_cost_usd="1.00",
            ),
            max_requests=5,
            max_acquisitions=0,
        )


def test_requires_exactly_one_of_script_or_live(tmp_path):
    bundle_dir = make_bundle(tmp_path, ladder=False)
    with pytest.raises(OnodaError, match="exactly one"):
        build_launch(
            variant_id="S0",
            bundle_dir=bundle_dir,
            work_dir=tmp_path / "work",
            max_requests=5,
            max_acquisitions=0,
        )


def test_scripted_env_has_no_inherited_provider_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "leaked")
    monkeypatch.setenv("OPENAI_API_KEY", "leaked")
    monkeypatch.setenv("GEMINI_API_KEY", "leaked")
    monkeypatch.setenv("GOOGLE_API_KEY", "leaked")
    monkeypatch.setenv("SOME_OTHER_API_KEY", "leaked")
    monkeypatch.setenv("UNRELATED_VAR", "keep-me")
    bundle_dir = make_bundle(tmp_path, ladder=False)
    launch_obj = build_launch(
        variant_id="S0",
        bundle_dir=bundle_dir,
        work_dir=tmp_path / "work",
        script=SCRIPT,
        max_requests=5,
        max_acquisitions=1,
    )
    for key in launch_obj.env:
        assert not key.endswith("_API_KEY"), key
        assert not key.startswith(("ANTHROPIC_", "OPENAI_", "GEMINI_", "GOOGLE_")), key
    assert launch_obj.env["UNRELATED_VAR"] == "keep-me"


def test_scripted_env_still_keeps_own_provider_name_out_of_credentials(tmp_path):
    # SCRIPTED_PROVIDER/SCRIPTED_MODEL are not env-var names, but the argv
    # must still select the scripted provider, never a real one.
    bundle_dir = make_bundle(tmp_path, ladder=False)
    launch_obj = build_launch(
        variant_id="S0",
        bundle_dir=bundle_dir,
        work_dir=tmp_path / "work",
        script=SCRIPT,
        max_requests=5,
        max_acquisitions=1,
    )
    assert launch_obj.argv[launch_obj.argv.index("--provider") + 1] == SCRIPTED_PROVIDER


def test_build_launch_writes_policy_and_run_info(tmp_path):
    bundle_dir = make_bundle(tmp_path, ladder=False)
    work_dir = tmp_path / "work"
    launch_obj = build_launch(
        variant_id="S0",
        bundle_dir=bundle_dir,
        work_dir=work_dir,
        script=SCRIPT,
        max_requests=7,
        max_acquisitions=2,
    )
    policy = GatewayPolicy.model_validate_json(launch_obj.policy_path.read_text())
    assert policy.max_requests == 7
    assert policy.max_acquisitions == 2
    assert str(bundle_dir) in policy.path_aliases
    assert str(RUNTIME_DIR) in policy.path_aliases
    assert launch_obj.env[ENV_POLICY] == str(launch_obj.policy_path)
    assert launch_obj.env[ENV_RUN_INFO] == str(launch_obj.run_info_path)
    assert launch_obj.env[ENV_SPOOL] == str(launch_obj.spool_dir)
    assert launch_obj.env[ENV_TASK] == str(bundle_dir)
    assert launch_obj.env[ENV_SCRIPT] == str(work_dir / "script.json")
    assert launch_obj.spool_dir.is_dir()
    assert list(launch_obj.spool_dir.iterdir()) == []
    assert launch_obj.cwd == work_dir / "workspace"


# --------------------------------------------------------------------- launch


def test_launch_runs_with_closed_stdin_and_returns_exit(tmp_path):
    bundle_dir = make_bundle(tmp_path, ladder=False)
    work_dir = tmp_path / "work"
    launch_obj = build_launch(
        variant_id="S0",
        bundle_dir=bundle_dir,
        work_dir=work_dir,
        script=SCRIPT,
        max_requests=1,
        max_acquisitions=0,
    )
    fake_argv = ("python3", "-c", "import sys; assert sys.stdin.read() == ''")
    fake = dataclasses.replace(launch_obj, argv=fake_argv)
    result = launch(fake, timeout_s=10)
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.spool_dir == launch_obj.spool_dir
    assert result.session_dir == launch_obj.session_dir


def test_launch_times_out(tmp_path):
    bundle_dir = make_bundle(tmp_path, ladder=False)
    launch_obj = build_launch(
        variant_id="S0",
        bundle_dir=bundle_dir,
        work_dir=tmp_path / "work",
        script=SCRIPT,
        max_requests=1,
        max_acquisitions=0,
    )
    launch_obj = dataclasses.replace(launch_obj, argv=("sleep", "5"))
    result = launch(launch_obj, timeout_s=0.2)
    assert result.timed_out is True
