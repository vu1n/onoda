"""Shared fixtures: an isolated store + mock scheduler + controller per test."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from labrun.clock import LogicalClock
from labrun.controller import Controller, RunState
from labrun.fixture import PROGRAMS
from labrun.jobs import MockScheduler
from labrun.store import Store


@dataclass
class Env:
    root: Path
    store: Store
    scheduler: MockScheduler
    controller: Controller

    def reopen(self, **scheduler_kwargs) -> Env:
        """Simulate a fresh process over the same durable state."""
        return open_env(self.root, **scheduler_kwargs)


def open_env(root: Path, *, crash_hook=None, **scheduler_kwargs) -> Env:
    store = Store(root / "store", clock=LogicalClock())
    scheduler = MockScheduler(root / "scheduler", PROGRAMS, **scheduler_kwargs)
    return Env(root, store, scheduler, Controller(store, scheduler, crash_hook=crash_hook))


@pytest.fixture
def make_env(tmp_path):
    def factory(name: str = "a", **kwargs) -> Env:
        return open_env(tmp_path / name, **kwargs)

    return factory


@pytest.fixture
def fixture_run(make_env) -> tuple[Env, RunState]:
    """A completed deterministic fixture run 'demo' under mock-policy-v1."""
    env = make_env()
    return env, env.controller.run_fixture("demo")
