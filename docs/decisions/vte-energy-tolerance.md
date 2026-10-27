# An identified VTE state is accurate only within 0.3 eV of its SC-NEVPT2 reference

**Status at migration:** draft. This migration does not change that status.


<a id="identified-within-tolerance"></a>
## Judge every identified state's energy against one declared tolerance

An identified reference row is `within` when the claimed SC-NEVPT2 excitation energy differs from
QUESTDB's SC-NEVPT2 value by at most `VTE_TOLERANCE_EV` (0.3 eV), and `outside` otherwise. Each
`vte-claims` evaluation records `ref.<row>.accuracy`, the count `accurate`, `tolerance_ev` and
`tolerance_source` (this decision), and `vte report` sums `accurate` per arm beside `coverage`.

**Why.** Before this, the grader reported each identified row's error but never judged it, so a
state 9 eV from its reference counted the same as a 0.002 eV hit. In the 32-run experiment qv5
(2026-10-05), 23 of S0's 50 identified rows and 8 of S1's 37 were more than 1 eV off. Coverage
alone could not tell a correct assignment from a wrong state under the right label.
match-by-identity requires a numeric comparison to pass only against a tolerance that names the
decision that justified it. This is that decision.

**The number.** Owner, 2026-10-06, choosing among 0.2, 0.3 and 0.5 eV: 0.3 eV. The intent is that it sits above the spread between an agent's own SC-NEVPT2
setup (basis, active space, state averaging) and QUESTDB's, and well below the gap between
different states of one molecule. No reference or paper fixes it. It is not a chemistry claim.

### Invariant
- The tolerance is one constant (`VTE_TOLERANCE_EV`), the same for every case, row and arm.
- Accuracy is judged only on identified rows. It never identifies a row: identity is still spin,
  irrep and character (match-by-identity), never energy.
- `coverage`, `mae_ev` and `false_success_claims` keep their meaning. `accurate` is added beside
  them, not substituted for them.
