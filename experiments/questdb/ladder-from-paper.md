# ladder-from-paper@1

**Reconstruction, not the authors' text.** This ladder is rebuilt by the onoda
authors from the description in Sec. II D-E of Lee and Rondinelli, arXiv
2609.13357 (the paper's `CLAUDE.md` is not public). The rule letters, their
priority order and the four validation criteria follow the paper's summary;
the wording, the ORCA phrasing and every example are ours and carry no
authority from the paper. It is an instance of assumption A33 and is recorded
as A61. If the authors share their ladder it replaces this file.

Use it as advice. It never runs anything for you; every job is one you write and
submit with `orca_submit`, and every claim is one you report with `report_vtes`.

## The task in one paragraph

For the molecule in `structure`, compute the vertical excitation energies of the
listed target states (a count per spin and irrep) with SA-CASSCF followed by
SC-NEVPT2 in aug-cc-pVTZ, with RIJCOSX and AutoAux. Report a state only when
you can say which state it is: its spin, its irrep, its character (the dominant
excitation, for example n to pi*), and the job whose output prints its energy.
A value identified only by its position in the energy order is not identified.

## How to read the rules

Rules are checked in priority order, A first. Apply the first rule whose
condition holds, act on it, and check again after the next job. A diagnosis that
repeats is not a stop; it is evidence the previous fix was wrong. Attempts are
limited per target specification (active space plus state averaging); spend
them on actions you have not yet tried.

An override is allowed. Say which rule you are overruling and why, ground the
reason in something checkable in the same output, and treat it as an override,
not a precedent.

## The rules

**A. Job failure.** The job did not run, ORCA reports an error, or the output
lacks the expected sections. A normal-looking exit is not success: an exit code
of zero does not mean the calculation converged. Read the output, fix the input
(keywords, basis, memory, process count, geometry block) and resubmit.

**B. SCF non-convergence.** The reference SCF did not converge. Change the
convergence strategy (damping, level shifting, a different initial guess)
before touching the CASSCF settings, and confirm the SCF energy is sensible
before going on.

**C. CASSCF non-convergence.** The macro-iterations did not converge or the
energy oscillates. Change the orbital optimiser or step control, start from
converged orbitals of a smaller or related job, or reduce the state average.
Do not accept energies from an unconverged CASSCF.

**D. Wrong state.** The job converged but the states are not the ones you want.
Judge each state from the natural orbital occupation numbers, the Loewdin
orbital populations and where the orbitals sit on the atoms. If the active
orbitals are wrong, rotate the intended orbitals into the active space from
converged orbitals, within irreps, and use level shifting to keep them there.
Verify orbital indices against the wavefunction that produced them (criterion 4
below), never against an older job.

**E. Incomplete correlation or root window.** An active orbital is nearly empty
or nearly full (check the least-occupied natural orbital; there is no fixed
threshold) or the target state sits outside the roots you averaged over. Enlarge
the active space for the missing orbital, or split the targets into separate
calculations. Do not widen the state average without limit.

**F. Strong disagreement with the literature.** Your energy differs strongly
from what you know of this molecule. Check the state identity and the
state-average weights first. If the disagreement cannot be explained, say so
and stop on this state rather than adjusting until the numbers agree.

**G. Rydberg states.** The target is a Rydberg state and a valence active space
cannot describe it. Use a response-type diagnostic (MCRPA) only to see which
states are present, never as a reported energy. Rank diffuse orbitals by their
smallest Gaussian exponent, enlarge the active space with the diffuse orbital
you need, and redo the SA-CASSCF and SC-NEVPT2.

**H. Success.** The CASSCF converged, the states are identified by the criteria
below, and the SC-NEVPT2 energies are printed. Report them with
`report_vtes`: one entry per identified state, with its job. Leave out any
state you could not identify.

## Validation criteria

Check these on a converged CASSCF before you trust its states.

1. The CASSCF energy lies below the HF energy of the same geometry and basis.
2. The dominant configuration weight of each state, judged against your other
   attempts on this molecule; there is no fixed threshold.
3. The irreps of the active orbitals match the active space you asked for.
4. Orbital indices and CI-string positions are valid only for the wavefunction
   that produced them. Re-verify them at every decision point.

## Notes on ORCA

ORCA weights the state-average roots by block, not equally across all roots, so
check the weights it prints against the ones you intend. Symmetry can be turned
off and on between jobs, but orbitals read or projected between symmetry
settings may be re-ordered; check the orbital order after every such step.

## Stop conditions

Stop only when the attempt budget is exhausted with every distinct action
tried, when a literature conflict cannot be explained (rule F), when the data
look corrupted, or when infrastructure outside the ladder fails.
