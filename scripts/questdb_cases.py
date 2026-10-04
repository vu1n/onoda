#!/usr/bin/env python
"""Build `cases-dev/questdb/<molecule>/` from a QUESTDB checkout (VTE-007).

    uv run python scripts/questdb_cases.py /path/to/QUESTDB [--out cases-dev/questdb]

Per molecule it writes `<molecule>.vte.yaml` (`VteCaseManifest`, agent-visible),
the CC3/aug-cc-pVTZ ground-state geometry copied byte for byte, and
`references.json` (`VteReferences`, evaluator-only, pinned by sha256 in the
manifest). Reference rows are the SC-NEVPT2 column of `data/cas/json/
<molecule>_mrpt.json`; the energy is copied as printed (decimal text, eV).
Fluorescence rows (`[F]`) need an excited-state geometry and are dropped
(`parse_state_label` returns None for them). Targets are the reference rows'
counts per (spin, irrep); the manifest carries no energy and no character.

The output is deterministic: running it twice on the same checkout gives the
same bytes. Assumption A62 records the molecule choice.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

import yaml

from onoda.canonical import sha256_digest
from onoda.science.vte import (
    VTE_MANIFEST_SUFFIX,
    VteCaseManifest,
    VteReferenceRow,
    VteReferences,
    VteTarget,
    normalize_irrep,
    parse_state_label,
)

QUESTDB_REPOSITORY = "https://github.com/pfloos/QUESTDB"
QUESTDB_COMMIT = "460482bcc6231dab41fe32188dc43959a7c2bdaa"
LICENSE = "CC BY-SA 4.0"
RETRIEVED = "2026-10-03"
NEVPT2_COLUMN = "SC-NEVPT2"

# molecule -> (point group, ground-state xyz name in geometries/xyz). The
# point group is the symmetry QUESTDB's active-space tables are labelled in:
# Cs for the two planar molecules with a' / a'' columns, C2v otherwise.
# Context: doc://onoda/case-data-provenance@0001#pinned-acquired-cases — QUESTDB data is dev-only and lives under cases-dev/, credited
MOLECULES: dict[str, tuple[str, str]] = {
    "acetaldehyde": ("Cs", "acetaldehyde.xyz"),
    "formamide": ("Cs", "formamide.xyz"),
    "ketene": ("C2v", "ketene_1.xyz"),
    "diazomethane": ("C2v", "diazomethane_1.xyz"),
    "acetone": ("C2v", "acetone.xyz"),
    "thioacetone": ("C2v", "thioacetone.xyz"),
    "methylenecyclopropene": ("C2v", "methylenecyclopropene.xyz"),
    "cyclopropenone": ("C2v", "cyclopropenone.xyz"),
}


def reference_rows(table: dict) -> tuple[VteReferenceRow, ...]:
    """The SC-NEVPT2 rows of one `*_mrpt.json` table, fluorescence rows dropped."""
    headers = table["headers"]
    space_col, average_col = headers[1], headers[2]
    rows = []
    for row in table["data"]:
        parsed = parse_state_label(row["State"])
        if parsed is None:
            continue
        spin, irrep, character = parsed
        rows.append(
            VteReferenceRow(
                label=row["State"],
                spin=spin,
                irrep=irrep,
                character=character,
                energy_ev=row[NEVPT2_COLUMN],
                active_space=row[space_col],
                state_average=row[average_col],
            )
        )
    return tuple(rows)


def targets_of(rows: tuple[VteReferenceRow, ...]) -> tuple[VteTarget, ...]:
    counts = Counter((r.spin, normalize_irrep(r.irrep)) for r in rows)
    return tuple(
        VteTarget(spin=spin, irrep=irrep, count=n) for (spin, irrep), n in sorted(counts.items())
    )


def _write_json(path: Path, model: VteReferences) -> bytes:
    data = (
        json.dumps(model.model_dump(by_alias=True), indent=2, ensure_ascii=True) + "\n"
    ).encode()
    path.write_bytes(data)
    return data


def build_case(questdb: Path, commit: str, molecule: str, out: Path) -> Path:
    point_group, xyz_name = MOLECULES[molecule]
    case_id = f"questdb-{molecule}"
    case_dir = out / molecule
    case_dir.mkdir(parents=True, exist_ok=True)

    xyz_src = questdb / "geometries" / "xyz" / xyz_name
    xyz_bytes = xyz_src.read_bytes()
    (case_dir / f"{molecule}.xyz").write_bytes(xyz_bytes)

    json_path = f"data/cas/json/{molecule}_mrpt.json"
    table = json.loads((questdb / json_path).read_text())
    rows = reference_rows(table)
    refs = VteReferences(
        case_id=case_id,
        method="SC-NEVPT2/aug-cc-pVTZ",
        source={
            "repository": QUESTDB_REPOSITORY,
            "commit": commit,
            "path": json_path,
            "license": LICENSE,
        },
        rows=rows,
    )
    refs_bytes = _write_json(case_dir / "references.json", refs)

    manifest = VteCaseManifest.model_validate(
        {
            "case_id": case_id,
            "title": f"{molecule}: vertical excitation energies, SA-CASSCF/SC-NEVPT2 (QUESTDB)",
            "molecule": molecule,
            "point_group": point_group,
            "method": "SA-CASSCF/SC-NEVPT2/aug-cc-pVTZ",
            "split": "development",
            "structure": {"path": f"{molecule}.xyz", "sha256": sha256_digest(xyz_bytes)},
            "targets": [t.model_dump() for t in targets_of(rows)],
            "references": {"path": "references.json", "sha256": sha256_digest(refs_bytes)},
            "provenance": {
                "origin": "QUESTDB (QUEST database of highly-accurate excitation energies)",
                "repository": QUESTDB_REPOSITORY,
                "commit": commit,
                "upstream_path": f"geometries/xyz/{xyz_name}; {json_path}",
                "license": LICENSE,
                "license_file": "../README.md",
                "retrieved": RETRIEVED,
                "executed_here": False,
                "notes": (
                    "CC3(Full)/aug-cc-pVTZ ground-state geometry, copied unchanged. Reference "
                    "rows are QUESTDB's SC-NEVPT2 values; fluorescence rows are dropped."
                ),
            },
            "review": {
                "status": "pending-human-review",
                "reviewer": None,
                "date": None,
                "notes": (
                    "Dev-only case; molecule choice is A62. Targets are the reference rows' "
                    "counts per spin and irrep. No human has reviewed this case. The split "
                    "is provisional until the 4 / 4 optimizer and held-out assignment."
                ),
            },
            "labels": ["authentic", "acquired", "dev-only", "questdb", "cc-by-sa-4.0"],
        }
    )
    path = case_dir / f"{molecule}{VTE_MANIFEST_SUFFIX}"
    path.write_text(yaml.safe_dump(manifest.model_dump(mode="json"), sort_keys=False))
    return path


README = """# Dev-only QUESTDB cases (VTE)

Eight small closed-shell molecules for the QUESTDB agent slice: an agent
writes and runs its own SA-CASSCF/SC-NEVPT2 jobs and is graded on vertical
excitation energies by state identity. See `docs/ASSUMPTIONS.md` A57-A62.

Credit: the geometries and reference values come from the QUEST database,
{repo}, commit `{commit}`, licensed {license}
(https://creativecommons.org/licenses/by-sa/4.0/). P.-F. Loos,
M. Boggio-Pasqua, A. Blondel, F. Lipparini, and D. Jacquemin, "The QUEST
database of highly-accurate excitation energies", J. Chem. Theory Comput. 21,
8010 (2025), doi:10.1021/acs.jctc.5c00975. Everything under this directory is
an extract or adaptation of that material and is shared under the same
CC BY-SA 4.0 licence.

Per molecule:

- `<molecule>.xyz`: the CC3(Full)/aug-cc-pVTZ ground-state geometry
  (angstrom), unchanged from `geometries/xyz/`.
- `<molecule>.vte.yaml`: the agent-visible manifest (point group, target
  states as counts per spin and irrep, provenance). It holds no energies.
- `references.json`: the SC-NEVPT2 reference rows from
  `data/cas/json/<molecule>_mrpt.json` with QUESTDB's active space and state
  average. Evaluator-only; never read by an agent-side code path.

Fluorescence rows (`[F]`) need an excited-state geometry and are dropped: 53
transitions remain over the eight molecules. Used with `--cases-root
cases-dev`; not part of `cases/` and no split draws from it yet. Regenerate
with `uv run python scripts/questdb_cases.py <QUESTDB checkout>`.
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("questdb", type=Path, help="checkout of pfloos/QUESTDB")
    ap.add_argument("--out", type=Path, default=Path("cases-dev/questdb"))
    args = ap.parse_args(argv)

    head = subprocess.run(
        ["git", "-C", str(args.questdb), "rev-parse", "HEAD"], capture_output=True, text=True
    )
    if head.returncode != 0 or head.stdout.strip() != QUESTDB_COMMIT:
        sys.exit(f"{args.questdb} is not a checkout of QUESTDB at {QUESTDB_COMMIT}")

    args.out.mkdir(parents=True, exist_ok=True)
    for molecule in MOLECULES:
        print(build_case(args.questdb, QUESTDB_COMMIT, molecule, args.out))
    (args.out / "README.md").write_text(
        README.format(repo=QUESTDB_REPOSITORY, commit=QUESTDB_COMMIT, license=LICENSE)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
