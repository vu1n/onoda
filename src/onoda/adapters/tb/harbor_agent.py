"""Harbor agent: the pinned Pi with one arm's extensions, inside the task container.

Import path for `harbor run --agent`: `onoda.adapters.tb.harbor_agent:OnodaPiAgent`.
Harbor is not an onoda dependency; this module is only imported by Harbor
(`uv run --with harbor ...`), and nothing else in onoda imports it.

`install` unpacks a runtime tarball built by `slice.build_runtime` (the pinned
`pi/` tree plus a Node binary for the container's architecture); nothing is
downloaded inside the task container. `run` stages `container.build_container_launch`
and runs it under `timeout`. The spool HMAC key stays in this object and is
used after the run to verify the spool and write `onoda/metrics.json`.
"""

from __future__ import annotations

import json
import shlex
import tempfile
from pathlib import Path
from typing import Any, override

from harbor.agents.installed.base import BaseInstalledAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from onoda.adapters.tb.container import (
    INSTALL_ROOT,
    LOGS_DIR,
    NODE_PATH,
    RUN_DIR,
    ContainerLaunch,
    build_container_launch,
)
from onoda.adapters.tb.metrics import run_metrics
from onoda.errors import OnodaError

_ARCH = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
_STDOUT = LOGS_DIR / "pi.jsonl"
_STDERR = LOGS_DIR / "pi.stderr"
_EXIT = LOGS_DIR / "exit_code"


class OnodaPiAgent(BaseInstalledAgent):
    def __init__(
        self,
        *args: Any,
        variant: str,
        runtime_dir: str,
        base_url: str,
        max_requests: int | str,
        max_cost_usd: str | float,
        timeout_sec: int | str,
        credential_env: str = "OPENROUTER_API_KEY",
        reducer_model: str | None = None,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self._variant = variant
        self._runtime_dir = Path(runtime_dir)
        self._base_url = base_url
        self._max_requests = int(max_requests)
        self._max_cost_usd = str(max_cost_usd)
        self._timeout_sec = int(timeout_sec)
        self._credential_env = credential_env
        self._reducer_model = reducer_model
        self._launch: ContainerLaunch | None = None

    @staticmethod
    @override
    def name() -> str:
        return "onoda-pi"

    @override
    def get_version_command(self) -> str | None:
        return None

    def _provider_model(self) -> tuple[str, str]:
        if not self.model_name or "/" not in self.model_name:
            raise OnodaError("model must be provider/model, e.g. openrouter/deepseek/...")
        provider, model = self.model_name.split("/", 1)
        return provider, model

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        machine = (await environment.exec(command="uname -m", user="root")).stdout or ""
        arch = _ARCH.get(machine.strip().splitlines()[-1] if machine.strip() else "")
        if arch is None:
            raise OnodaError(f"no runtime for container architecture {machine.strip()!r}")
        tarball = self._runtime_dir / f"runtime-{arch}.tar.gz"
        if not tarball.is_file():
            raise OnodaError(f"runtime tarball {tarball} is missing; build it first")
        remote = f"{INSTALL_ROOT}/runtime.tar.gz"
        await self.exec_as_root(environment, command=f"mkdir -p {INSTALL_ROOT}/runtime {RUN_DIR}")
        await environment.upload_file(tarball, remote)
        await self.exec_as_root(
            environment,
            command=(
                f"tar -xzf {remote} -C {INSTALL_ROOT}/runtime && rm {remote} && "
                f"chmod -R a+rX {INSTALL_ROOT}/runtime && {NODE_PATH} --version"
            ),
        )

    async def _write(self, environment: BaseEnvironment, path: str, data: bytes) -> None:
        with tempfile.TemporaryDirectory(prefix="onoda-tb-") as tmp:
            local = Path(tmp) / "f"
            local.write_bytes(data)
            await environment.upload_file(local, path)
        owner = environment.default_user
        chown = f"chown {shlex.quote(str(owner))} {shlex.quote(path)} && " if owner else ""
        await self.exec_as_root(environment, command=f"{chown}chmod 600 {shlex.quote(path)}")

    @override
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        provider, model = self._provider_model()
        launch = build_container_launch(
            variant_id=self._variant,
            instruction=instruction,
            provider=provider,
            model=model,
            base_url=self._base_url,
            credential_env=self._credential_env,
            max_requests=self._max_requests,
            max_cost_usd=self._max_cost_usd,
            reducer=(provider, self._reducer_model) if self._reducer_model else None,
        )
        self._launch = launch
        dirs = " ".join(shlex.quote(d) for d in (str(RUN_DIR), str(LOGS_DIR), *launch.dirs))
        owner = environment.default_user
        chown = f" && chown -R {shlex.quote(str(owner))} {dirs}" if owner else ""
        await self.exec_as_root(environment, command=f"mkdir -p {dirs}{chown}")
        for path, text in launch.files.items():
            await self._write(environment, path, text.encode())
        await self._write(environment, launch.key_path, launch.spool_key)

        pi = " ".join(shlex.quote(a) for a in launch.argv)
        # Pi's exit code goes to a file and the command itself succeeds: a timeout or a
        # budget stop is a recorded outcome the verifier still grades, not a harness error.
        command = (
            f"( timeout -k 15 {self._timeout_sec} {pi} </dev/null 2>{_STDERR}; "
            f'echo $? >{_EXIT} ) | {{ grep -v \'"type":"message_update"\' || true; }} >{_STDOUT}'
        )
        await self.exec_as_agent(
            environment, command=command, env=launch.env, timeout_sec=self._timeout_sec + 120
        )

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        if self._launch is None:
            return
        out = self.logs_dir / LOGS_DIR.name
        out.mkdir(parents=True, exist_ok=True)
        try:
            metrics = run_metrics(out / "spool", self._launch.spool_key, out / "sessions")
        except (OnodaError, OSError) as exc:
            # Recorded, not raised: the verifier's verdict on the container still stands,
            # and a run without a verified spool is reported as such.
            metrics = {"spool_verified": False, "error": f"{type(exc).__name__}: {exc}"}
        exit_file = out / _EXIT.name
        metrics["pi_exit_code"] = exit_file.read_text().strip() if exit_file.is_file() else None
        metrics["caps"] = {
            "max_requests": self._max_requests,
            "max_cost_usd": self._max_cost_usd,
            "timeout_sec": self._timeout_sec,
        }
        (out / "metrics.json").write_text(json.dumps(metrics, indent=1, sort_keys=True))
        totals = metrics.get("tokens_total")
        if totals:
            context.n_input_tokens = totals["input"] + totals["cacheRead"]
            context.n_output_tokens = totals["output"]
            context.n_cache_tokens = totals["cacheRead"]
            context.cost_usd = float(metrics["cost_usd_total"])
