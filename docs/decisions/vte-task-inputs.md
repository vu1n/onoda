# What a VTE agent is given: geometry, point group and target counts; no energies, no ORCA manual

**Status at migration:** active. This migration does not change that status.


<a id="agent-gets-structure-and-targets"></a>
## Give the agent the structure and target counts only; ladder variants need a pinned ladder file

A VTE task gives the molecule's QUESTDB CC3/aug-cc-pVTZ geometry, its point group and the target states as counts per spin and irrep, with no energies and no character labels. The agent writes its own ORCA inputs. No ORCA manual is shipped (not redistributable). Ladder variants refuse to run without an operator-supplied ladder file, recorded by sha256; the QUESTDB experiment uses the labelled reconstruction `experiments/questdb/ladder-from-paper.md`.

**Why.** The comparison is about how the agent works from the structure, as in the source paper; extra inputs would change the task, and a ladder that is not pinned cannot be compared across arms.

### Invariant
- `VteCaseManifest` exposes only structure, point group and `VteTarget` (spin, irrep, count) to the agent.
- `build_launch` refuses a ladder variant without a ladder file; the ladder's sha256 is recorded.
- Allowed documentation is committed per run as content-addressed `allowed-documentation` objects.

### Known gaps (not yet enforced; owner to decide)
- A2: there is no versioned corpus manifest and nothing records the difference from the paper's historical snapshot; only per-run allowed-documentation objects exist.

### Carried forward from the assumptions register

Verbatim from `docs/ASSUMPTIONS.md` as of 83df418 (2026-10-04). Where a row and the invariants above disagree, the invariants are current.

#### A2: Historical literature/context snapshot available to the original agents

Register status: unresolved. Owner milestone: M3.

Version our own permitted corpus explicitly and record the difference from any historical snapshot.

#### A33: Content of the advisory ladder and the allowed documentation

Register status: unresolved (instances of A2/A3). Owner milestone: M3.

Ladder variants (S1 onward) refuse to run without an operator-supplied ladder file, recorded by sha256. Tests use a synthetic ladder fixture that is labelled synthetic and makes no scientific claim.

#### A57: What does a VTE task give the agent?

Register status: decided (default, QUESTDB slice). Owner milestone: QUESTDB slice.

The molecule's QUESTDB CC3/aug-cc-pVTZ geometry, its point group, and the target states as counts per spin and irrep (`VteTarget`), with no energies and no character labels. The paper says its agent had "only the molecular structure files"; giving the target counts is our choice so the task is defined, and is a question for the authors. The agent writes its own ORCA inputs and runs them with `orca_submit`; it gets no ORCA manual (not redistributable).

#### A61: Which advisory ladder does the Decision arm get?

Register status: decided (default, QUESTDB slice). Owner milestone: QUESTDB slice.

`experiments/questdb/ladder-from-paper.md`: rules A–H and the four validation criteria reconstructed from Sec. II D–E of arXiv 2609.13357, labelled a reconstruction, not the authors' CLAUDE.md (an instance of A33). It replaces the synthetic fixture for this experiment only and is replaced by the authors' text if they share it.
