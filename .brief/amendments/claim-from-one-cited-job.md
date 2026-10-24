# Amendment proposal: claim-from-one-cited-job

**Decision:** doc://onoda/vte-grading-contract@latest#claim-from-one-cited-job
**Proposed by:** Claude, from the "Plan the next round of testing" project thread (Vu chose "Label fix, then ladder test", 2026-10-24)   **Date:** 2026-10-24

## What should change
The grader becomes evaluator version 4. "Its normalized character picks exactly one row" becomes "its identity character picks exactly one row, by the row's own character or one of the row's recorded aliases":

- `identity_character` is `normalize_character` with a Rydberg p orbital's axis dropped (`n,3pz` and `n,3p` compare equal). Within one irrep the axis is fixed, so the suffix carries nothing the irrep does not. Reference keys and reports keep the full character.
- A reference row may carry `aliases` (other characters naming the same state) and must then carry `notes` saying why. Loading refuses references where two rows of one spin and irrep accept the same character.
- Reference characters may be corrected from the printed label by an erratum in `scripts/questdb_cases.py`, recorded in the row's `notes`.

The first data under this rule:
- Four rows whose pi glyph lost its Symbol font and reads `p`: ketene 1A2(p,3p) and 3A1(p,pi*), diazomethane 1A1 and 3A1 (p,pi*). QUESTDB's own `data/json/MAIN/` tables type the same states `p3p`/`ppi`, where `p` is its spelling of pi. Corrected to `pi,3p` and `pi,pi*`.
- Thioacetone's sulfur Rydberg rows: QUESTDB writes 3s/3p; sulfur's first Rydberg shell is n = 4, which is what agents write. Aliases `n,4s` (1B2 n,3s) and `n,4p` (1B2 n,3pz, 1A1 n,3py).

## Why
pr52-assessment and the wrong-state dig found these labels, not the agents, account for most of Opus 5.5's misses on ketene and diazomethane and three thioacetone slots per run: no agent writes a bare `p`, and a correct sulfur 4s/4p label never matched. Scores under v3 therefore understate every arm by the same unreachable slots.

## What does not change
Matching stays by spin, irrep and character, never by energy (match-by-identity). A claim still needs one cited job that printed the value with its irrep. Agents still see no characters (vte-task-inputs): aliases and notes live only in the evaluator-only references file.

## Impact / risk
v3 and v4 evaluations of one run differ and are not compared with each other (`evaluation.compare` already refuses mixed versions); saved runs are regraded under v4 with new evaluations, the v3 ones kept. The axis fold also lets `n,3p` identify acetone, cyclopropenone and methylenecyclopropene 3p rows that only matched `n,3px/y/z` before. The reference files of ketene, diazomethane and thioacetone change, so their manifest pins change.
