"""Stage one Pi run inside a benchmark task container (exploratory, A54).

`runtime.build_launch` stages a run on the host around an ORCA `TaskBundle`
and, for a live run, requires the bubblewrap sandbox (Linux only). A
Terminal-Bench task instead brings its own container, so here the whole Pi
process (and therefore its `bash` tool and Action Fusion's `then_run`) runs
inside that container and the container is the isolation.

This module only describes the launch: the files to write in the container,
the argv and the environment. It starts nothing and imports no harness.

What stays the same as `build_launch`: the pinned runtime, the variant table's
mechanisms and `sol-pi.json`, the gateway policy with its request and cost
caps, the extension order (SoL-Pi, then the recorder last) and the keyed spool.

What differs, on purpose:

- No task extension: there is no ORCA bundle, so no task tools, no ladder
  and no raw tool-result tap. A variant id here names its mechanism set only.
- No credential in the container. The provider's base URL points at
  `proxy.SpendProxy` on the host, which adds the real key; Pi is given a
  placeholder, so nothing the agent's tools can read is a secret.
- Pi inherits the container's environment (the task image's PATH and
  settings), not an allowlist: the tools must see the task as its image
  defines it.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from onoda.adapters.pi.contract import (
    BASE_TOOLS,
    DECIMAL_RE,
    ENV_POLICY,
    ENV_RUN_INFO,
    ENV_SPOOL,
    ENV_SPOOL_KEY,
    MECHANISM_TOOLS,
    PI_PACKAGE,
    PI_VERSION,
    RECORDER_EXTENSION,
    SOL_PI_COMMIT,
    SOL_PI_ENTRY,
    SPOOL_SCHEMA,
    VARIANTS,
    GatewayPolicy,
)
from onoda.errors import CapabilityError, OnodaError

# Where the agent installs the runtime (read-only to the run) and stages a run.
INSTALL_ROOT = PurePosixPath("/installed-agent/onoda")
NODE_PATH = INSTALL_ROOT / "runtime" / "node"
PI_RUNTIME = INSTALL_ROOT / "runtime" / "pi"
RUN_DIR = INSTALL_ROOT / "run"
# Harbor mounts /logs/agent from the trial directory, so the spool and the
# sessions (with SoL-Pi's archives under them) are on the host as written.
LOGS_DIR = PurePosixPath("/logs/agent/onoda")

PLACEHOLDER_CREDENTIAL = "onoda-proxy-no-credential"


@dataclass(frozen=True)
class ContainerLaunch:
    """One prepared in-container Pi invocation. Nothing has run yet."""

    argv: tuple[str, ...]
    env: dict[str, str]
    files: dict[str, str]  # container path -> text to write before launch
    dirs: tuple[str, ...]  # container directories the run writes into
    key_path: str  # where `spool_key` must be written (0600); Pi unlinks it at load
    spool_dir: PurePosixPath
    session_dir: PurePosixPath
    run_info: dict[str, Any]
    spool_key: bytes = field(default=b"", repr=False)  # in memory only


def tools_for(variant_id: str) -> tuple[str, ...]:
    """Pi's base tools plus the tools the variant's mechanisms register."""
    variant = VARIANTS[variant_id]
    return (*BASE_TOOLS, *(t for m in variant.mechanisms for t in MECHANISM_TOOLS.get(m, ())))


def build_container_launch(
    *,
    variant_id: str,
    instruction: str,
    provider: str,
    model: str,
    base_url: str,
    credential_env: str,
    max_requests: int,
    max_cost_usd: str,
    reducer: tuple[str, str] | None = None,
) -> ContainerLaunch:
    """Stage `variant_id` on `instruction` for a live run through `base_url`.

    Every live setting is explicit and unresolved ones fail closed, as for
    `LiveSettings`. `reducer` = (provider, model) is required by a variant
    with the Evidence-Preserving Reducer.
    """
    if variant_id not in VARIANTS:
        raise OnodaError(f"unknown variant {variant_id!r}")
    variant = VARIANTS[variant_id]
    if variant.rlm:
        raise CapabilityError(
            f"variant {variant_id} needs the RLM kernel; not staged in a container"
        )
    problems = [
        name
        for name, ok in (
            ("provider", bool(provider)),
            ("model", bool(model)),
            ("base_url", base_url.startswith(("http://", "https://"))),
            ("credential_env", bool(credential_env)),
            ("max_requests", max_requests > 0),
            ("max_cost_usd", bool(DECIMAL_RE.match(max_cost_usd))),
        )
        if not ok
    ]
    if problems:
        raise CapabilityError(f"container Pi run missing/invalid settings: {', '.join(problems)}")
    # Pi reads a leading `@` as a file include and a leading `-` as a flag (TaskBundle).
    if instruction.startswith(("@", "-")):
        raise OnodaError("instruction must not start with '@' or '-'")
    try:
        sol_pi_cfg = variant.sol_pi_config(
            reducer_provider=reducer[0] if reducer else None,
            reducer_model=reducer[1] if reducer else None,
        )
    except ValueError as exc:
        raise CapabilityError(str(exc)) from exc

    agent_dir = RUN_DIR / "agent"
    spool_dir = LOGS_DIR / "spool"
    session_dir = LOGS_DIR / "sessions"
    policy_path = RUN_DIR / "policy.json"
    run_info_path = RUN_DIR / "run_info.json"
    key_path = RUN_DIR / "spool.key"
    tools = tools_for(variant_id)

    policy = GatewayPolicy(
        allowed_tools=tools,
        max_requests=max_requests,
        max_acquisitions=0,
        max_cost_usd=max_cost_usd,
        path_aliases={
            str(PI_RUNTIME): "$PI_RUNTIME",
            str(spool_dir): "$SPOOL",
            str(session_dir): "$SESSIONS",
        },
    )
    run_info: dict[str, Any] = {
        "schema": SPOOL_SCHEMA,
        "pi_version": PI_VERSION,
        "sol_pi_commit": SOL_PI_COMMIT if sol_pi_cfg is not None else None,
        "variant": variant_id,
        "mechanisms": list(variant.mechanisms),
        "provider": provider,
        "model": model,
        "scripted": False,
        "max_cost_usd": max_cost_usd,
    }
    files = {
        str(policy_path): policy.model_dump_json(by_alias=True),
        str(run_info_path): json.dumps(run_info, sort_keys=True),
        # A `.pi/` the agent writes into its workspace must not reconfigure Pi or SoL-Pi.
        str(agent_dir / "settings.json"): json.dumps({"defaultProjectTrust": "never"}),
        # The built-in provider keeps its models and prices; only its endpoint moves.
        str(agent_dir / "models.json"): json.dumps(
            {"providers": {provider: {"baseUrl": base_url}}}, sort_keys=True
        ),
    }
    if sol_pi_cfg is not None:
        files[str(agent_dir / "sol-pi.json")] = json.dumps(sol_pi_cfg, sort_keys=True)

    extensions = [str(PI_RUNTIME / SOL_PI_ENTRY)] if sol_pi_cfg is not None else []
    extensions.append(str(PI_RUNTIME / "extensions" / RECORDER_EXTENSION))
    argv = [
        str(NODE_PATH),
        str(PI_RUNTIME / "node_modules" / PI_PACKAGE / "dist/bundle/cli.js"),
        "--no-extensions",
    ]
    for ext in extensions:
        argv += ["-e", ext]
    argv += [
        "--session-dir",
        str(session_dir),
        "--no-context-files",
        "--no-skills",
        "--no-prompt-templates",
        "--provider",
        provider,
        "--model",
        model,
        "--tools",
        ",".join(tools),
        "--mode",
        "json",
        "-p",
        instruction,
    ]
    env = {
        "PI_CODING_AGENT_DIR": str(agent_dir),
        "PI_TELEMETRY": "0",
        "PI_SKIP_VERSION_CHECK": "1",
        ENV_SPOOL: str(spool_dir),
        ENV_SPOOL_KEY: str(key_path),
        ENV_POLICY: str(policy_path),
        ENV_RUN_INFO: str(run_info_path),
        credential_env: PLACEHOLDER_CREDENTIAL,
    }
    return ContainerLaunch(
        argv=tuple(argv),
        env=env,
        files=files,
        dirs=(str(agent_dir), str(spool_dir), str(session_dir)),
        key_path=str(key_path),
        spool_dir=spool_dir,
        session_dir=session_dir,
        run_info=run_info,
        spool_key=secrets.token_bytes(32),
    )
