# Dev-only ORCA cases from cclib-data

The logs here come from cclib's regression-data repository,
https://github.com/cclib/cclib-data, commit
a16cc80ea29e8baec60abd0df346ce6862f52531. Credit: the cclib developers and the
contributors of each regression file (https://github.com/cclib/cclib).

cclib-data ships no LICENSE file. These cases are for development and
held-out checks only, used with `--cases-root cases-dev`; they are not part
of `cases/` and no optimizer split draws from them.

The two TD-DFT logs above have no upstream `.inp`. Each `.inp` for them is the log's own
INPUT FILE echo with the `| n>` prefixes stripped (for the six cclib cases
in `cases/`, that procedure reproduces the vendored `.inp` byte for byte).

## Held-out cases beyond TD-DFT (added 2026-10-02)

Ten more logs from the same repository, chosen to cover methods other than
TD-DFT: a geometry optimisation (ORCA 4.2), a compound optimisation plus EPR
job (ORCA 6.0), IR frequencies, NMR shielding, an unrestricted single point,
CCSD(T), DLPNO-CCSD, CASSCF (ORCA 6.0), TD-DFT/TDA with spin-orbit coupling
(ORCA 5.0) and ZINDO/S CIS (ORCA 6.1). All are at commit
a16cc80ea29e8baec60abd0df346ce6862f52531 except `ORCA6.1/1509.*`, which is
newer and pinned at 127b2229d584d54fb84a74e383edb18a271fc283. Where cclib-data
ships an input (`.inp`/`.in`) it is used as is; otherwise the input is the
log's first INPUT FILE echo with the prefixes stripped. The claim grader only
checks the final single-point energy and excited states, so for the
ground-state cases the graded claim is the final energy (the last
`FINAL SINGLE POINT ENERGY` in the log); frequencies, shieldings and
geometries are not graded.
