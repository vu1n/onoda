"""Runtime: `doctor` checks the pinned install, `build_launch`/`launch` run Pi.

`doctor()` never runs `npm` or touches the network; it only reads files
already on disk (`node_modules/**/package.json`, `package-lock.json`) and
runs the system `node` binary to read its version.

`build_launch()` stages one run under `work_dir` (subdirs `workspace/`,
`spool/`, `sessions/`, `agent/`) and returns argv/env plus the paths the
recorder and the caller need; it never launches anything. `launch()` runs a
`Launch` synchronously with stdin closed (Pi hangs on an open stdin) and a
wall-clock timeout, and never raises for a non-zero exit or a timeout --
both come back as data on `LaunchResult`.

Extension load order is part of the contract (`contract.py`): the task
extension first, SoL-Pi next when the effective mechanisms are non-empty,
the RLM extension next for an `rlm` variant, the scripted provider next when
running scripted, the recorder last. An `rlm` variant's candidate (M4;
`SEED_CANDIDATE` when none is given) is written to `work_dir/rlm-components.json`
and its scratch dir (`work_dir/rlm`, never the spool) is handed to the
extension through `ENV_RLM_WORK`/`ENV_RLM_COMPONENTS`/`ENV_RLM_DEPTH`/
`ENV_RLM_PYTHON`. A candidate's `sol_pi.config` component, when set,
overrides the variant's mechanisms.

The child environment is built from an allowlist (`_ENV_ALLOWLIST`), never
copied from `os.environ`: only the Pi/onoda settings the runtime sets, and in
live mode the one variable named by `credential_env`, reach Pi. The recorder
then removes the ONODA_PI_* variables before any tool runs. Each launch gets
a fresh random spool HMAC key, handed to the Pi process through a 0600 file
it unlinks at load and kept in memory on `Launch`/`LaunchResult` for ingest;
it is never written to the store.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from onoda.adapters.pi.contract import (
    DECIMAL_RE,
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
    PI_PACKAGE,
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
    SOL_PI_PACKAGE,
    SPOOL_SCHEMA,
    TASK_EXTENSION,
    VARIANTS,
    GatewayPolicy,
    HarnessCandidate,
    Script,
    TaskBundle,
)
from onoda.errors import CapabilityError, OnodaError

_MIN_NODE = (22, 19, 0)

# The only variables the child inherits from this process. Everything else
# (credentials, tokens, unrelated settings) stays out; a live run adds exactly
# the variable its `credential_env` names.
_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TMPDIR",
    "TZ",
)


def _node_version() -> str | None:
    exe = shutil.which("node")
    if exe is None:
        return None
    try:
        result = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=5, check=True
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip()


def _parse_node_version(raw: str) -> tuple[int, ...] | None:
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", raw)
    return tuple(int(g) for g in m.groups()) if m else None


def _read_pi_version(runtime_dir: Path) -> str | None:
    path = runtime_dir / "node_modules" / PI_PACKAGE / "package.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    version = data.get("version")
    return version if isinstance(version, str) else None


def _read_sol_pi_commit(runtime_dir: Path) -> str | None:
    path = runtime_dir / "package-lock.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    entry = data.get("packages", {}).get(f"node_modules/{SOL_PI_PACKAGE}")
    resolved = entry.get("resolved") if isinstance(entry, dict) else None
    if not isinstance(resolved, str) or "#" not in resolved:
        return None
    return resolved.rsplit("#", 1)[1]


def doctor(runtime_dir: Path = RUNTIME_DIR) -> dict[str, Any]:
    """Compare the installed runtime under `runtime_dir` against the contract pins.

    Reads only `package.json`/`package-lock.json` files and runs `node
    --version`; never `npm`, never the network. Returns
    `{ok, node, pi_version, sol_pi_commit, problems}`.
    """
    problems: list[str] = []

    node_raw = _node_version()
    if node_raw is None:
        problems.append("node executable not found on PATH")
    else:
        parsed = _parse_node_version(node_raw)
        if parsed is None or parsed < _MIN_NODE:
            problems.append(
                f"node {node_raw} is older than the required v{'.'.join(map(str, _MIN_NODE))}"
            )

    pi_version = _read_pi_version(runtime_dir)
    if pi_version is None:
        problems.append(f"{PI_PACKAGE} not found under {runtime_dir}")
    elif pi_version != PI_VERSION:
        problems.append(f"{PI_PACKAGE} {pi_version} does not match the pinned {PI_VERSION}")

    sol_pi_commit = _read_sol_pi_commit(runtime_dir)
    if sol_pi_commit is None:
        problems.append(f"{SOL_PI_PACKAGE} not found in {runtime_dir / 'package-lock.json'}")
    elif sol_pi_commit != SOL_PI_COMMIT:
        problems.append(
            f"{SOL_PI_PACKAGE} commit {sol_pi_commit} does not match the pinned {SOL_PI_COMMIT}"
        )

    return {
        "ok": not problems,
        "node": node_raw,
        "pi_version": pi_version,
        "sol_pi_commit": sol_pi_commit,
        "problems": problems,
    }


@dataclass(frozen=True)
class LiveSettings:
    """A real-provider run. Every field must be explicitly given; there are no
    defaults and nothing is inferred from a present API key (A35).
    """

    provider: str | None = None
    model: str | None = None
    credential_env: str | None = None
    max_requests: int | None = None
    max_cost_usd: str | None = None

    def validate(self) -> None:
        """Raise `CapabilityError` naming every missing/invalid setting, then check
        that `credential_env` actually names a variable present in this process's
        environment.
        """
        problems = []
        if not self.provider:
            problems.append("provider")
        if not self.model:
            problems.append("model")
        if not self.credential_env:
            problems.append("credential_env")
        if not self.max_requests or self.max_requests <= 0:
            problems.append("max_requests")
        if not self.max_cost_usd or not DECIMAL_RE.match(self.max_cost_usd):
            problems.append("max_cost_usd")
        if problems:
            raise CapabilityError(f"live Pi run missing/invalid settings: {', '.join(problems)}")
        if self.credential_env not in os.environ:
            raise CapabilityError(
                f"live Pi run credential env var {self.credential_env!r} is not set"
            )


@dataclass(frozen=True)
class Launch:
    """One prepared Pi invocation: argv/env plus the paths a caller or the
    recorder needs afterward. Nothing has run yet.
    """

    argv: tuple[str, ...]
    env: dict[str, str]
    cwd: Path
    spool_dir: Path
    session_dir: Path
    agent_dir: Path
    policy_path: Path
    run_info_path: Path
    # The spool HMAC key: in memory only (the Pi process unlinks its copy).
    spool_key: bytes = field(default=b"", repr=False)
    # Set for rlm variants only: the RLM scratch dir and the components file
    # the extension reads at load (ENV_RLM_WORK / ENV_RLM_COMPONENTS).
    rlm_work_dir: Path | None = None
    rlm_components_path: Path | None = None


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, sort_keys=True))


def build_launch(
    *,
    variant_id: str,
    bundle_dir: Path,
    work_dir: Path,
    script: Script | None = None,
    live: LiveSettings | None = None,
    max_requests: int,
    max_acquisitions: int,
    reducer: tuple[str, str] | None = None,
    candidate: HarnessCandidate | None = None,
) -> Launch:
    """Stage one run of `variant_id` against the bundle at `bundle_dir` under
    `work_dir`. Exactly one of `script` (offline, deterministic) or `live` (a
    real provider) must be given; a live run's `max_requests` must equal its
    own `live.max_requests`.

    `candidate` is the harness candidate under test (M4): for an `rlm`
    variant it defaults to `SEED_CANDIDATE`. Its `sol_pi.config` component,
    when set, overrides the variant's mechanisms (the effective mechanisms
    then drive `--tools`, the SoL-Pi config and `run_info["mechanisms"]`).
    """
    if (script is None) == (live is None):
        raise OnodaError("build_launch requires exactly one of script or live")
    if live is not None and live.max_requests is not None and max_requests != live.max_requests:
        raise OnodaError(
            f"max_requests {max_requests} conflicts with the live setting {live.max_requests}"
        )

    variant = VARIANTS[variant_id]
    override_mechanisms = candidate.sol_pi_mechanisms() if candidate is not None else None
    effective_variant = (
        variant
        if override_mechanisms is None
        else variant.model_copy(update={"mechanisms": override_mechanisms})
    )
    rlm_candidate = (
        candidate if candidate is not None else (SEED_CANDIDATE if variant.rlm else None)
    )

    # Pi runs with the workspace as cwd, so every path handed to it must be absolute.
    bundle_dir = Path(bundle_dir).resolve()
    work_dir = Path(work_dir).resolve()
    task_bundle = TaskBundle.model_validate_json((bundle_dir / "task.json").read_text())
    if variant.ladder:
        if task_bundle.ladder_sha256 is None or not (bundle_dir / "ladder.md").is_file():
            raise CapabilityError(f"variant {variant_id} requires an advisory ladder")

    if live is not None:
        live.validate()
        provider, model = live.provider, live.model
    else:
        provider, model = SCRIPTED_PROVIDER, SCRIPTED_MODEL

    if reducer is not None:
        reducer_provider, reducer_model = reducer
    elif script is not None:
        reducer_provider, reducer_model = SCRIPTED_PROVIDER, SCRIPTED_REDUCER_MODEL
    else:
        reducer_provider = reducer_model = None
    try:
        sol_pi_cfg = effective_variant.sol_pi_config(
            reducer_provider=reducer_provider, reducer_model=reducer_model
        )
    except ValueError as exc:
        raise CapabilityError(str(exc)) from exc

    work_dir = Path(work_dir)
    workspace_dir = work_dir / "workspace"
    spool_dir = work_dir / "spool"
    sessions_dir = work_dir / "sessions"
    agent_dir = work_dir / "agent"
    rlm_work_dir = work_dir / "rlm" if variant.rlm else None
    dirs = (workspace_dir, spool_dir, sessions_dir, agent_dir)
    if rlm_work_dir is not None:
        dirs = (*dirs, rlm_work_dir)
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)

    if sol_pi_cfg is not None:
        _write_json(agent_dir / "sol-pi.json", sol_pi_cfg)

    rlm_components_path: Path | None = None
    if variant.rlm:
        assert rlm_candidate is not None
        rlm_components_path = work_dir / "rlm-components.json"
        rlm_components_path.write_text(
            rlm_candidate.rlm_components().model_dump_json(by_alias=True)
        )

    path_aliases = {
        str(workspace_dir): "$WORKSPACE",
        str(RUNTIME_DIR): "$PI_RUNTIME",
        str(RUNTIME_DIR.resolve()): "$PI_RUNTIME",
        # pi/node_modules may be a symlink into another checkout.
        str((RUNTIME_DIR / "node_modules").resolve()): "$PI_RUNTIME/node_modules",
        str(bundle_dir): "$BUNDLE",
        str(spool_dir): "$SPOOL",
        str(sessions_dir): "$SESSIONS",
    }
    if rlm_work_dir is not None:
        path_aliases[str(rlm_work_dir)] = "$RLM_WORK"

    policy_path = work_dir / "policy.json"
    policy = GatewayPolicy(
        allowed_tools=effective_variant.tools(),
        max_requests=max_requests,
        max_acquisitions=max_acquisitions,
        max_cost_usd=live.max_cost_usd if live is not None else None,
        path_aliases=path_aliases,
    )
    policy_path.write_text(policy.model_dump_json(by_alias=True))

    run_info_path = work_dir / "run_info.json"
    run_info: dict[str, Any] = {
        "schema": SPOOL_SCHEMA,
        "pi_version": PI_VERSION,
        "sol_pi_commit": SOL_PI_COMMIT if sol_pi_cfg is not None else None,
        "variant": variant_id,
        "mechanisms": list(effective_variant.mechanisms),
        "provider": provider,
        "model": model,
        "scripted": script is not None,
    }
    if live is not None:
        run_info["max_cost_usd"] = live.max_cost_usd
    if rlm_candidate is not None:
        run_info["candidate_digest"] = rlm_candidate.digest()
        run_info["rlm_limits"] = rlm_candidate.limits.model_dump()
    _write_json(run_info_path, run_info)

    extensions = [str(EXTENSIONS_DIR / TASK_EXTENSION)]
    if sol_pi_cfg is not None:
        extensions.append(str(RUNTIME_DIR / SOL_PI_ENTRY))
    if variant.rlm:
        extensions.append(str(EXTENSIONS_DIR / RLM_EXTENSION))
    if script is not None:
        extensions.append(str(EXTENSIONS_DIR / SCRIPTED_EXTENSION))
    extensions.append(str(EXTENSIONS_DIR / RECORDER_EXTENSION))

    spool_key = secrets.token_bytes(32)
    key_path = work_dir / "spool.key"
    key_path.unlink(missing_ok=True)  # a stale key from an earlier staging of this work dir
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, spool_key)
    finally:
        os.close(fd)

    env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
    if live is not None:
        assert live.credential_env is not None  # validate() checked it
        env[live.credential_env] = os.environ[live.credential_env]
    if script is not None:
        script_path = work_dir / "script.json"
        script_path.write_text(script.model_dump_json(by_alias=True))
        env[ENV_SCRIPT] = str(script_path)
    env.update(
        {
            "PI_CODING_AGENT_DIR": str(agent_dir),
            "PI_TELEMETRY": "0",
            "PI_SKIP_VERSION_CHECK": "1",
            ENV_SPOOL: str(spool_dir),
            ENV_SPOOL_KEY: str(key_path),
            ENV_TASK: str(bundle_dir),
            ENV_POLICY: str(policy_path),
            ENV_RUN_INFO: str(run_info_path),
        }
    )
    if rlm_work_dir is not None:
        assert rlm_components_path is not None
        env.update(
            {
                ENV_RLM_DEPTH: "0",
                ENV_RLM_COMPONENTS: str(rlm_components_path),
                ENV_RLM_WORK: str(rlm_work_dir),
                ENV_RLM_PYTHON: sys.executable,
            }
        )
    if live is None:
        # Scripted runs never reach a real provider; PI_OFFLINE keeps Pi from
        # making any of its own background network calls too. A live run
        # needs the network to reach its provider, so it is left unset.
        env["PI_OFFLINE"] = "1"

    argv = [
        str(RUNTIME_DIR / "node_modules/.bin/pi"),
        "--no-extensions",
    ]
    for ext in extensions:
        argv += ["-e", ext]
    argv += [
        "--session-dir",
        str(sessions_dir),
        "--no-context-files",
        "--no-skills",
        "--no-prompt-templates",
        "--provider",
        provider,
        "--model",
        model,
        "--tools",
        ",".join(effective_variant.tools()),
        "--mode",
        "json",
        "-p",
        task_bundle.instruction,
    ]

    return Launch(
        argv=tuple(argv),
        env=env,
        cwd=workspace_dir,
        spool_dir=spool_dir,
        session_dir=sessions_dir,
        agent_dir=agent_dir,
        policy_path=policy_path,
        run_info_path=run_info_path,
        spool_key=spool_key,
        rlm_work_dir=rlm_work_dir,
        rlm_components_path=rlm_components_path,
    )


@dataclass(frozen=True)
class LaunchResult:
    exit_code: int
    stdout: str
    stderr: str
    spool_dir: Path
    session_dir: Path
    timed_out: bool
    spool_key: bytes = field(default=b"", repr=False)


def launch(launch: Launch, *, timeout_s: float) -> LaunchResult:
    """Run a prepared `Launch` synchronously with stdin closed (Pi hangs on an
    open stdin). A timeout or non-zero exit is returned as data, never raised.
    """
    try:
        proc = subprocess.run(
            launch.argv,
            cwd=launch.cwd,
            env=launch.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = (
            exc.stdout
            if isinstance(exc.stdout, str)
            else (exc.stdout or b"").decode(errors="replace")
        )
        stderr = (
            exc.stderr
            if isinstance(exc.stderr, str)
            else (exc.stderr or b"").decode(errors="replace")
        )
        return LaunchResult(
            -1, stdout, stderr, launch.spool_dir, launch.session_dir, True, launch.spool_key
        )
    return LaunchResult(
        proc.returncode,
        proc.stdout,
        proc.stderr,
        launch.spool_dir,
        launch.session_dir,
        False,
        launch.spool_key,
    )
