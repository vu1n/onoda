# Dev-only ORCA cases from cclib-data

The logs here come from cclib's regression-data repository,
https://github.com/cclib/cclib-data, commit
a16cc80ea29e8baec60abd0df346ce6862f52531. Credit: the cclib developers and the
contributors of each regression file (https://github.com/cclib/cclib).

cclib-data ships no LICENSE file. These cases are for development and
held-out checks only, used with `--cases-root cases-dev`; they are not part
of `cases/` and no optimizer split draws from them.

Neither log has an upstream `.inp`. Each `.inp` here is the log's own
INPUT FILE echo with the `| n>` prefixes stripped (for the six cclib cases
in `cases/`, that procedure reproduces the vendored `.inp` byte for byte).
