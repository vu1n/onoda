# Amendment proposal: claim-from-one-cited-job

**Decision:** doc://onoda/vte-grading-contract@0001#claim-from-one-cited-job
**Proposed by:** grading-tolerance PR   **Date:** 2026-10-06

## What should change
1. In the rule paragraph, replace "and (spin, irrep), or the normalized character among several,
   picks exactly one row" with "and its normalized character picks exactly one row of the claimed
   (spin, irrep), even when that is the only such row".
2. Add these invariant bullets:
   - "`_candidates` requires the character to pick the row, even for the only row of its spin and
     irrep. A bare or different label is `unidentified` with reason `character`."
   - "Each identified row also carries an accuracy verdict against the draft decision
     vte-energy-tolerance. `vte report` adds `accurate` and `runs_without_claim` per arm."
   - "`VTE_INSTRUCTION` tells both arms that a state is matched by spin, irrep and character
     together."
3. Note in the title or body that the evaluator version is now 3.

## Why
In the 32-run experiment qv5 (2026-10-05), 31 of the 53 targets were the only row of their spin and
irrep, so any supported claim with that label was identified whatever character it gave.
S0-methylenecyclopropene-r2 scored 6 of 6 with no character matching and errors up to 10.6 eV.
S0-diazomethane-r2 was credited with 1A2(pi,pi*) for a "pi,sigma*" state 8.9 eV from the
reference. 19 of S0's 50 identifications and 3 of S1's 37 named a different character from the
target. Coverage therefore rewarded labelling rather than finding the state.

## Code that needs it
`src/onoda/adapters/pi/vte.py` (`_candidates`, the accuracy metrics, `vte_report`) and the
`VTE_INSTRUCTION` sentence in `src/onoda/adapters/pi/run.py`. The tests are in
`tests/test_pi_vte_grading.py` and `tests/test_pi_vte_end_to_end.py`.

## Impact / risk
Earlier evaluations stay as they are. Regrading a run writes a new v3 evaluation beside them, so
qv5 can be regraded without changing its records. Coverage will drop for both arms. The agents are
not told the targets' characters (vte-task-inputs gives counts per spin and irrep only), so a
correct calculation that names a state's character differently from QUESTDB will now go
unidentified. That is the paper's identification rule applied without the shortcut.
