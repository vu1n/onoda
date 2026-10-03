# Dev-only QUESTDB cases (VTE)

Eight small closed-shell molecules for the QUESTDB agent slice: an agent
writes and runs its own SA-CASSCF/SC-NEVPT2 jobs and is graded on vertical
excitation energies by state identity. See `docs/ASSUMPTIONS.md` A57-A62.

Credit: the geometries and reference values come from the QUEST database,
https://github.com/pfloos/QUESTDB, commit `460482bcc6231dab41fe32188dc43959a7c2bdaa`, licensed CC BY-SA 4.0
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
