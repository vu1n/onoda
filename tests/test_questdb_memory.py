"""A75: the memory tiers both QUESTDB arms get hold procedures, never reference values."""

from __future__ import annotations

import json
import re
from pathlib import Path

from onoda.adapters.pi.task import check_doc_name

ROOT = Path(__file__).resolve().parents[1]
MEMORY = sorted((ROOT / "experiments" / "questdb" / "memory").glob("memory-*.md"))


def test_the_tiers_are_staged_as_plain_docs():
    assert [p.name for p in MEMORY] == ["memory-harness.md", "memory-project.md"]
    for p in MEMORY:
        check_doc_name(p.name)


def test_no_reference_value_or_reference_active_space_is_in_memory():
    text = "\n".join(p.read_text() for p in MEMORY)
    for refs in (ROOT / "cases-dev" / "questdb").glob("*/references.json"):
        for row in json.loads(refs.read_text())["rows"]:
            for key in ("energy_ev", "active_space", "state_average"):
                value = row.get(key)
                if value:
                    assert not re.search(rf"(?<![\d.]){re.escape(value)}(?![\d])", text), (refs, key)
