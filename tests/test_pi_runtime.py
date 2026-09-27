"""Tests for `onoda.adapters.pi.runtime`: doctor, build_launch, launch.

Everything here is offline and deterministic; the one test that touches the
real pinned Pi process is marked `requires_pi` (a smoke check that
`doctor()` of the real runtime is ok).
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import pytest
from conftest import requires_pi

from onoda.adapters.pi.contract import (
    ENV_POLICY,
    ENV_RLM_COMPONENTS,
    ENV_RLM_DEPTH,
    ENV_RLM_PYTHON,
    ENV_RLM_WORK,
    ENV_RUN_INFO,
    ENV_SCRIPT,
    ENV_SPOOL,
    ENV_SPOOL_KEY,
    ENV_TASK,
    EXTENSIONS_DIR,
    PI_VERSION,
    RECORDER_EXTENSION,
    RLM_EXTENSION,
    RUNTIME_DIR,
    SCRIPTED_EXTENSION,
    SCRIPTED_MODEL,
    SCRIPTED_PROVIDER,
    SCRIPTED_REDUCER_MODEL,
    SEED_CANDIDATE,
    SOL_PI_COMMIT,
    SOL_PI_ENTRY,
    TASK_EXTENSION,
    GatewayPolicy,
    HarnessCandidate,
    RlmComponents,
    Script,
    ScriptTurn,
    TextBlock,
    parse_spool_line,
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


def test_relative_paths_reach_pi_as_absolute(tmp_path, monkeypatch):
    # Pi runs with the workspace as cwd; a relative path would resolve there instead.
    make_bundle(tmp_path)
    monkeypatch.chdir(tmp_path)
    launch_obj = build_launch(
        variant_id="S0",
        bundle_dir=Path("bundle"),
        work_dir=Path("work"),
        script=SCRIPT,
        max_requests=5,
        max_acquisitions=1,
    )
    for name in (ENV_SPOOL, ENV_TASK, ENV_POLICY, ENV_RUN_INFO, ENV_SCRIPT):
        assert Path(launch_obj.env[name]).is_absolute(), name


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


def test_rlm_variant_no_longer_refused(tmp_path):
    bundle_dir = make_bundle(tmp_path, ladder=True)
    build_launch(
        variant_id="S1r",
        bundle_dir=bundle_dir,
        work_dir=tmp_path / "work",
        script=SCRIPT,
        max_requests=5,
        max_acquisitions=1,
    )


def test_rlm_argv_order_task_sol_pi_rlm_scripted_recorder(tmp_path):
    # S1r itself has no mechanisms; a candidate's sol_pi.config override turns
    # one on, so this also exercises "sol-pi loads when the effective
    # mechanisms are non-empty" together with the rlm extension.
    candidate = HarnessCandidate(
        components={
            **SEED_CANDIDATE.components,
            "sol_pi.config": '{"observationPack": true}',
        }
    )
    bundle_dir = make_bundle(tmp_path, ladder=True)
    launch_obj = build_launch(
        variant_id="S1r",
        bundle_dir=bundle_dir,
        work_dir=tmp_path / "work",
        script=SCRIPT,
        max_requests=5,
        max_acquisitions=1,
        candidate=candidate,
        reducer=(SCRIPTED_PROVIDER, SCRIPTED_REDUCER_MODEL),
    )
    dash_e = [launch_obj.argv[i + 1] for i, a in enumerate(launch_obj.argv) if a == "-e"]
    assert dash_e == [
        str(EXTENSIONS_DIR / TASK_EXTENSION),
        str(RUNTIME_DIR / SOL_PI_ENTRY),
        str(EXTENSIONS_DIR / RLM_EXTENSION),
        str(EXTENSIONS_DIR / SCRIPTED_EXTENSION),
        str(EXTENSIONS_DIR / RECORDER_EXTENSION),
    ]


def test_rlm_argv_order_without_mechanisms(tmp_path):
    bundle_dir = make_bundle(tmp_path, ladder=True)
    launch_obj = build_launch(
        variant_id="S1r",
        bundle_dir=bundle_dir,
        work_dir=tmp_path / "work",
        script=SCRIPT,
        max_requests=5,
        max_acquisitions=1,
    )
    dash_e = [launch_obj.argv[i + 1] for i, a in enumerate(launch_obj.argv) if a == "-e"]
    assert dash_e == [
        str(EXTENSIONS_DIR / TASK_EXTENSION),
        str(EXTENSIONS_DIR / RLM_EXTENSION),
        str(EXTENSIONS_DIR / SCRIPTED_EXTENSION),
        str(EXTENSIONS_DIR / RECORDER_EXTENSION),
    ]


def test_rlm_components_file_and_run_info_carry_the_candidate(tmp_path):
    bundle_dir = make_bundle(tmp_path, ladder=True)
    work_dir = tmp_path / "work"
    launch_obj = build_launch(
        variant_id="S1r",
        bundle_dir=bundle_dir,
        work_dir=work_dir,
        script=SCRIPT,
        max_requests=5,
        max_acquisitions=1,
    )
    assert launch_obj.rlm.work_dir == work_dir / "rlm"
    assert launch_obj.rlm.work_dir.is_dir()
    assert launch_obj.rlm.components_path == work_dir / "rlm-components.json"
    assert (
        launch_obj.rlm.components_path.read_text()
        == SEED_CANDIDATE.rlm_components().model_dump_json(by_alias=True)
    )
    run_info = json.loads(launch_obj.run_info_path.read_text())
    assert run_info["candidate_digest"] == SEED_CANDIDATE.digest()
    assert run_info["rlm_limits"] == SEED_CANDIDATE.limits.model_dump()


def test_rlm_components_file_reflects_an_explicit_candidate(tmp_path):
    candidate = HarnessCandidate(
        components={**SEED_CANDIDATE.components, "rlm.tool_doc": "a different tool doc"}
    )
    assert candidate.digest() != SEED_CANDIDATE.digest()
    bundle_dir = make_bundle(tmp_path, ladder=True)
    work_dir = tmp_path / "work"
    launch_obj = build_launch(
        variant_id="S1r",
        bundle_dir=bundle_dir,
        work_dir=work_dir,
        script=SCRIPT,
        max_requests=5,
        max_acquisitions=1,
        candidate=candidate,
    )
    written = RlmComponents.model_validate_json(launch_obj.rlm.components_path.read_text())
    assert written == candidate.rlm_components()
    run_info = json.loads(launch_obj.run_info_path.read_text())
    assert run_info["candidate_digest"] == candidate.digest()


def test_rlm_env_vars_and_path_alias(tmp_path):
    bundle_dir = make_bundle(tmp_path, ladder=True)
    work_dir = tmp_path / "work"
    launch_obj = build_launch(
        variant_id="S1r",
        bundle_dir=bundle_dir,
        work_dir=work_dir,
        script=SCRIPT,
        max_requests=5,
        max_acquisitions=1,
    )
    assert launch_obj.env[ENV_RLM_DEPTH] == "0"
    assert launch_obj.env[ENV_RLM_COMPONENTS] == str(launch_obj.rlm.components_path)
    assert launch_obj.env[ENV_RLM_WORK] == str(launch_obj.rlm.work_dir)
    assert launch_obj.env[ENV_RLM_PYTHON] == sys.executable
    policy = GatewayPolicy.model_validate_json(launch_obj.policy_path.read_text())
    assert policy.path_aliases[str(launch_obj.rlm.work_dir)] == "$RLM_WORK"


def test_non_rlm_variant_has_no_rlm_env_or_alias(tmp_path):
    bundle_dir = make_bundle(tmp_path, ladder=False)
    launch_obj = build_launch(
        variant_id="S0",
        bundle_dir=bundle_dir,
        work_dir=tmp_path / "work",
        script=SCRIPT,
        max_requests=5,
        max_acquisitions=1,
    )
    for env_name in (ENV_RLM_DEPTH, ENV_RLM_COMPONENTS, ENV_RLM_WORK, ENV_RLM_PYTHON):
        assert env_name not in launch_obj.env
    assert launch_obj.rlm is None
    policy = GatewayPolicy.model_validate_json(launch_obj.policy_path.read_text())
    assert not any(v == "$RLM_WORK" for v in policy.path_aliases.values())
    run_info = json.loads(launch_obj.run_info_path.read_text())
    assert "candidate_digest" not in run_info
    assert "rlm_limits" not in run_info


def test_candidate_sol_pi_config_overrides_a_non_rlm_variant(tmp_path):
    # S1 has no mechanisms of its own; the candidate turns onlineContextCompact on.
    candidate = HarnessCandidate(
        components={
            **SEED_CANDIDATE.components,
            "sol_pi.config": '{"onlineContextCompact": true}',
        }
    )
    bundle_dir = make_bundle(tmp_path, ladder=True)
    launch_obj = build_launch(
        variant_id="S1",
        bundle_dir=bundle_dir,
        work_dir=tmp_path / "work",
        script=SCRIPT,
        max_requests=5,
        max_acquisitions=1,
        candidate=candidate,
    )
    cfg = json.loads((launch_obj.agent_dir / "sol-pi.json").read_text())
    assert cfg["onlineContextCompact"] is True
    assert cfg["observationPack"] is False
    run_info = json.loads(launch_obj.run_info_path.read_text())
    assert run_info["mechanisms"] == ["onlineContextCompact"]
    assert run_info["sol_pi_commit"] == SOL_PI_COMMIT
    assert "--tools" in launch_obj.argv
    tools = launch_obj.argv[launch_obj.argv.index("--tools") + 1].split(",")
    assert "update_plan" in tools  # onlineContextCompact's mechanism tool
    # A non-rlm variant given a candidate still records it, even without RLM.
    assert run_info["candidate_digest"] == candidate.digest()
    assert run_info["rlm_limits"] == candidate.limits.model_dump()
    dash_e = [launch_obj.argv[i + 1] for i, a in enumerate(launch_obj.argv) if a == "-e"]
    assert RLM_EXTENSION not in "".join(dash_e)  # S1 is not an rlm variant


# ------------------------------------------------------- golden values (M3)


def test_golden_s0_scripted_argv_env_run_info_unchanged_without_candidate(tmp_path):
    bundle_dir = make_bundle(tmp_path, ladder=False)
    work_dir = tmp_path / "work"
    launch_obj = build_launch(
        variant_id="S0",
        bundle_dir=bundle_dir,
        work_dir=work_dir,
        script=SCRIPT,
        max_requests=5,
        max_acquisitions=1,
    )
    assert launch_obj.argv == (
        str(RUNTIME_DIR / "node_modules/.bin/pi"),
        "--no-extensions",
        "-e",
        str(EXTENSIONS_DIR / TASK_EXTENSION),
        "-e",
        str(EXTENSIONS_DIR / SCRIPTED_EXTENSION),
        "-e",
        str(EXTENSIONS_DIR / RECORDER_EXTENSION),
        "--session-dir",
        str(work_dir / "sessions"),
        "--no-context-files",
        "--no-skills",
        "--no-prompt-templates",
        "--provider",
        SCRIPTED_PROVIDER,
        "--model",
        SCRIPTED_MODEL,
        "--tools",
        "case_input,orca_run,report_states,read,bash,edit,write",
        "--mode",
        "json",
        "-p",
        "do the thing",
    )
    run_info = json.loads(launch_obj.run_info_path.read_text())
    assert run_info == {
        "schema": "onoda.pi-spool/1",
        "pi_version": PI_VERSION,
        "sol_pi_commit": None,
        "variant": "S0",
        "mechanisms": [],
        "provider": SCRIPTED_PROVIDER,
        "model": SCRIPTED_MODEL,
        "scripted": True,
    }
    rlm_env_keys = {ENV_RLM_DEPTH, ENV_RLM_COMPONENTS, ENV_RLM_WORK, ENV_RLM_PYTHON}
    assert not (rlm_env_keys & launch_obj.env.keys())
    assert launch_obj.rlm is None


def test_golden_s3_scripted_argv_env_run_info_unchanged_without_candidate(tmp_path):
    bundle_dir = make_bundle(tmp_path, ladder=True)
    work_dir = tmp_path / "work"
    launch_obj = build_launch(
        variant_id="S3",
        bundle_dir=bundle_dir,
        work_dir=work_dir,
        script=SCRIPT,
        max_requests=5,
        max_acquisitions=1,
        reducer=(SCRIPTED_PROVIDER, SCRIPTED_REDUCER_MODEL),
    )
    dash_e = [launch_obj.argv[i + 1] for i, a in enumerate(launch_obj.argv) if a == "-e"]
    assert dash_e == [
        str(EXTENSIONS_DIR / TASK_EXTENSION),
        str(RUNTIME_DIR / SOL_PI_ENTRY),
        str(EXTENSIONS_DIR / SCRIPTED_EXTENSION),
        str(EXTENSIONS_DIR / RECORDER_EXTENSION),
    ]
    run_info = json.loads(launch_obj.run_info_path.read_text())
    assert run_info["mechanisms"] == [
        "observationPack",
        "evidencePreservingReducer",
        "onlineContextCompact",
        "actionFusion",
    ]
    assert run_info["sol_pi_commit"] == SOL_PI_COMMIT
    assert "candidate_digest" not in run_info
    assert "rlm_limits" not in run_info
    rlm_env_keys = {ENV_RLM_DEPTH, ENV_RLM_COMPONENTS, ENV_RLM_WORK, ENV_RLM_PYTHON}
    assert not (rlm_env_keys & launch_obj.env.keys())
    assert launch_obj.rlm is None


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


def test_scripted_env_is_an_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "leaked")
    monkeypatch.setenv("OPENAI_API_KEY", "leaked")
    monkeypatch.setenv("GEMINI_API_KEY", "leaked")
    monkeypatch.setenv("GOOGLE_API_KEY", "leaked")
    monkeypatch.setenv("SOME_OTHER_API_KEY", "leaked")
    monkeypatch.setenv("ONODA_TEST_SECRET", "leaked")
    monkeypatch.setenv("LANG", "C.UTF-8")
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
    # Built from an allowlist, not os.environ: an arbitrary variable never reaches Pi.
    assert "ONODA_TEST_SECRET" not in launch_obj.env
    assert launch_obj.env["LANG"] == "C.UTF-8"


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


# ------------------------------------------------------------ review fixes (M3)


def live_launch(tmp_path: Path, monkeypatch, **kw):
    monkeypatch.setenv("MY_KEY", "x")
    live = LiveSettings(
        provider="anthropic",
        model="claude-x",
        credential_env="MY_KEY",
        max_requests=5,
        max_cost_usd="2.50",
    )
    return build_launch(
        variant_id="S0",
        bundle_dir=make_bundle(tmp_path, ladder=False),
        work_dir=tmp_path / "work",
        live=live,
        max_acquisitions=0,
        **{"max_requests": 5, **kw},
    )


def test_live_spool_start_matches_the_contract(tmp_path, monkeypatch):
    launch_obj = live_launch(tmp_path, monkeypatch)
    run_info = json.loads(launch_obj.run_info_path.read_text())
    # What the recorder writes: run info verbatim plus type, schema and session.
    line = {
        "seq": 0,
        **run_info,
        "type": "spool.start",
        "session_id": "s",
        "session_dir": str(launch_obj.session_dir),
    }
    start = parse_spool_line(line)
    assert (start.max_cost_usd, start.scripted) == ("2.50", False)
    policy = GatewayPolicy.model_validate_json(launch_obj.policy_path.read_text())
    assert (policy.max_cost_usd, policy.max_requests) == ("2.50", 5)


def test_live_launch_refuses_a_conflicting_request_cap(tmp_path, monkeypatch):
    with pytest.raises(OnodaError, match="conflicts"):
        live_launch(tmp_path, monkeypatch, max_requests=6)


def test_live_env_passes_only_the_named_credential(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "other-key")
    launch_obj = live_launch(tmp_path, monkeypatch)
    assert launch_obj.env["MY_KEY"] == "x"
    assert "ANTHROPIC_API_KEY" not in launch_obj.env
    assert "PI_OFFLINE" not in launch_obj.env


def test_spool_key_file_is_private_and_the_key_stays_in_memory(tmp_path):
    launch_obj = build_launch(
        variant_id="S0",
        bundle_dir=make_bundle(tmp_path, ladder=False),
        work_dir=tmp_path / "work",
        script=SCRIPT,
        max_requests=5,
        max_acquisitions=1,
    )
    key_path = Path(launch_obj.env[ENV_SPOOL_KEY])
    assert key_path.read_bytes() == launch_obj.spool_key and len(launch_obj.spool_key) == 32
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert launch_obj.spool_key.hex() not in repr(launch_obj)
    again = build_launch(
        variant_id="S0",
        bundle_dir=tmp_path / "bundle",
        work_dir=tmp_path / "work",
        script=SCRIPT,
        max_requests=5,
        max_acquisitions=1,
    )
    assert again.spool_key != launch_obj.spool_key  # fresh per launch


def test_replay_aliases_cover_sessions_and_the_resolved_runtime(tmp_path):
    launch_obj = build_launch(
        variant_id="S0",
        bundle_dir=make_bundle(tmp_path, ladder=False),
        work_dir=tmp_path / "work",
        script=SCRIPT,
        max_requests=5,
        max_acquisitions=1,
    )
    aliases = GatewayPolicy.model_validate_json(launch_obj.policy_path.read_text()).path_aliases
    assert aliases[str(launch_obj.session_dir)] == "$SESSIONS"
    assert aliases[str(RUNTIME_DIR.resolve())] == "$PI_RUNTIME"
    assert aliases[str((RUNTIME_DIR / "node_modules").resolve())] == "$PI_RUNTIME/node_modules"


# ---------------------------------------------------------------- run_case (M4)
#
# `launch` and `ingest_spool` are faked here: run_case's own job (staging,
# committing, and building the run config) is what these tests check, not a
# real Pi process or spool parsing (covered by test_pi_end_to_end.py and
# test_pi_ingest.py).


def test_run_case_threads_a_candidate_ladder_and_sol_pi_override(tmp_path, monkeypatch):
    from onoda.adapters.pi import run as pi_run
    from onoda.adapters.pi.ingest import IngestResult
    from onoda.clock import LogicalClock
    from onoda.science.contract import case_manifests, load_case
    from onoda.store import Store

    case = load_case(case_manifests()["orca6-water-mp2"])
    store = Store(tmp_path / "store", clock=LogicalClock())
    candidate = HarnessCandidate(
        components={
            **SEED_CANDIDATE.components,
            "ladder": "# candidate ladder (test fixture)\n",
            "sol_pi.config": '{"observationPack": true}',
        }
    )

    captured: dict = {}

    monkeypatch.setattr(
        pi_run, "launch", lambda prepared, *, timeout_s: _fake_launch_result(prepared)
    )

    def fake_ingest_spool(store, spool_dir, *, run_id, task, config, key, expected_session_dir):
        captured["config"] = config
        captured["task"] = task
        return IngestResult(
            run_id=run_id,
            spool="sha256:" + "0" * 64,
            complete=False,
            status=None,
            noop=False,
            invocations=(),
            checkpoints=(),
            claim=None,
            observation_pack={},
        )

    monkeypatch.setattr(pi_run, "ingest_spool", fake_ingest_spool)

    pi_run.run_case(
        store,
        case,
        run_id="r1",
        variant_id="S1",
        work_dir=tmp_path / "w",
        script=SCRIPT,
        candidate=candidate,
    )
    cfg = captured["config"]
    assert cfg["ladder_source"] == "candidate"
    assert cfg["sol_pi_config_override"] == {"observationPack": True}
    assert (
        cfg["candidate"]
        == store.put_json(candidate.components, labels=["harness-candidate"]).digest
    )
    # The ladder committed to the task is the candidate's, not None.
    ladder_bytes = store.get_bytes(captured["task"].inputs["ladder"])
    assert ladder_bytes == b"# candidate ladder (test fixture)\n"


def _fake_launch_result(prepared):
    from onoda.adapters.pi.runtime import LaunchResult

    (prepared.spool_dir / "events.jsonl").write_text("")
    return LaunchResult(
        exit_code=0,
        stdout="",
        stderr="",
        spool_dir=prepared.spool_dir,
        session_dir=prepared.session_dir,
        timed_out=False,
        spool_key=prepared.spool_key,
    )


def test_run_case_with_no_candidate_omits_candidate_config_keys(tmp_path, monkeypatch):
    from onoda.adapters.pi import run as pi_run
    from onoda.adapters.pi.ingest import IngestResult
    from onoda.clock import LogicalClock
    from onoda.science.contract import case_manifests, load_case
    from onoda.store import Store

    case = load_case(case_manifests()["orca6-water-mp2"])
    store = Store(tmp_path / "store", clock=LogicalClock())
    captured: dict = {}

    monkeypatch.setattr(
        pi_run, "launch", lambda prepared, *, timeout_s: _fake_launch_result(prepared)
    )

    def fake_ingest_spool(store, spool_dir, *, run_id, task, config, key, expected_session_dir):
        captured["config"] = config
        return IngestResult(
            run_id=run_id,
            spool="sha256:" + "0" * 64,
            complete=False,
            status=None,
            noop=False,
            invocations=(),
            checkpoints=(),
            claim=None,
            observation_pack={},
        )

    monkeypatch.setattr(pi_run, "ingest_spool", fake_ingest_spool)

    pi_run.run_case(
        store, case, run_id="r1", variant_id="S0", work_dir=tmp_path / "w", script=SCRIPT
    )
    cfg = captured["config"]
    assert "candidate" not in cfg
    assert "ladder_source" not in cfg
    assert "sol_pi_config_override" not in cfg
