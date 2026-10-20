# Source-backed ORCA evidence packets

The offline packet keeps every evidence reference from the unchanged
`orca-parser@3` observation, with the output SHA-256, 1-based source line, exact
parser text, and half-open byte span. A separate presentation can omit text under
its character budget without deleting facts or hiding omissions. The validator
rebuilds the packet from the supplied output and rejects altered fields.

This is an integrity and coverage result. It establishes neither better chemistry
nor a better scientific decision policy. No Clairic/Clef integration is justified
by these measurements: deterministic parsing already provides the printed facts.

Validation: 36 focused packet/CLI/archive tests passed; the full offline suite
with pinned Pi passed **1008 tests, 18 skipped**. Ruff lint/format and the Pi
TypeScript check passed. Brief check/pin passed; doctor reported no warnings and
two existing draft-decision infos. No scientific parser, grader or audit rule was
changed.

A security regression checks that nested malformed packet fields are rejected
before canonicalization can amplify memory through cumulative diagnostic paths.
The fix compares against reconstructed field shapes first; it changes neither
scientific parsing nor packet facts and introduces no scientific thresholds.

## Use and reproduce

From the repository root:

```sh
uv run onoda vte evidence tests/fixtures/orca611-nevpt2/ketene_f_target.out > /tmp/ketene-packet.json
uv run onoda vte evidence-check /tmp/ketene-packet.json tests/fixtures/orca611-nevpt2/ketene_f_target.out
uv run onoda vte evidence tests/fixtures/orca611-nevpt2/ketene_f_target.out --max-evidence-chars 0
uv run python scripts/evaluate_evidence_packet.py > /tmp/evidence-report.json
diff -u experiments/evidence-packet/report.json /tmp/evidence-report.json
```

The check prints `valid` and mechanical mismatch codes; invalid packets exit 2.
Malformed JSON and unavailable files produce an error on stderr and exit 2.
Copying the packet and identical source to another directory preserves validation;
filenames are not identity. Use `--synthetic` for mutated or stand-in source bytes.
Synthetic status is a caller declaration and cannot establish authenticity.

The v1 schema stores the unchanged observation, deduplicated evidence inventory,
bounded presentation with retained/omitted line numbers, unavailable provenance,
an empty interpretations list, and explicit unknowns. Byte spans include original
whitespace and line endings; `text` follows the parser's decoded/rstripped lines.
Invalid UTF-8 is explicit and parser problems survive. The 12,000-character cap
applies to `presentation.text` only; the complete packet is not bounded to a
provider context window. Parser termination/problems expose available evidence of
source truncation; the packet does not certify that a supplied file is complete.

## Archive comparison

The fixed source manifest pins ten authentic phase-0/0c outputs from 2026-10-02:
five ketene, three ethylene and two formaldehyde logs. Jobs within each molecule
share an acquisition lineage. There are three molecule/lineage groups, not ten
independent scientific trials. The script checks the source pins before analyzing
bytes. The committed report has per-output digests and counts and is deterministic.

| Mechanical measure | Direct parser | Existing shadow excerpt | Packet |
| --- | ---: | ---: | ---: |
| Unique parser evidence lines retained | 47/47 | 24/47 | 47/47 |
| Parser warning lines retained | 14/14 | 0/14 | 14/14 |
| Genuine packets validating | — | — | 10/10 |
| Zero-character presentations with valid full inventory | — | — | 10/10 |

Excerpt retention means the exact parser evidence text occurs as an excerpt line.
It is a lexical comparison, without line/digest verification, and does not assign
scientific importance to a warning. The direct parser is the strong deterministic
baseline: the packet adds no extracted facts or independent corroboration. The
packet presentation retained all 47 lines within its text cap on this archive;
budget-exhaustion tests separately demonstrate explicit omissions.

Eight controlled corruptions per log exercise changed source bytes, changed facts,
forged source offsets/text, omitted inventory, invented job/wavefunction bindings,
and forged presentation coverage. All **80/80** were rejected; all genuine packets
validated. A digest-only baseline rejected **10/80**, the changed-source cases.
These constructed corruptions are synthetic and test specified mechanical
properties. They are not independent samples, hidden scientific labels, real
`other_job` audit findings, or estimates of field false-accept rates. No scientific
false-ready rate, selective risk, abstention quality or useful-ready coverage can
be measured without independent labels for those decisions.

## Boundaries and unknowns

The archive lacks recorded run/job-to-output associations and the `.gbw` files
mentioned by its inputs. Both bindings therefore stay `unavailable`. An input echo
or MORead filename cannot verify wavefunction identity, an orbital map, or a
reproducible job dependency. The validator checks content consistency with supplied
bytes; replacing both packet and source consistently needs an independent archive
binding to detect. Parser version changes require a separately versioned packet
implementation, rather than silently treating historical facts as current.

The new commands are operator-only and do not feed existing shadow decisions,
agents, evaluators, audit rules or promotion gates. No reference file, model,
solver, deployment or new credential was used. Provider/solver calls are zero;
model quality, calibration, costs and latency remain unmeasured. There is no
direct-Clef or Clairic-then-Clef result. No trained policy or confidence threshold
was introduced.

PR52's frozen protocol excludes formamide, acetone, cyclopropenone and
methylenecyclopropene. They are absent from this manifest and comparison. Before
that split was recovered, the read-only advisor probed historical acetone fixture
coverage, and both contexts encountered historical reference comparisons in the
fixture README. Those incidental exposures are disclosed and excluded from the
reported measurements and any scientific labels. The stopped report/protocol were
read to recover the boundary; neither PR46 nor PR52 was resumed or promoted.

## Smallest justified next experiment

First obtain independent, reference-free expert labels for evidence adequacy on
these three development lineages, with explicit reasons for missing evidence and
genuine assignment ambiguity. Archive the actual job/output/dependency bindings
needed by each question. Labels must distinguish claim support, character identity
and all-target readiness; the existing shadow `ready` proxy cannot substitute.

Only then freeze the labels, bounded model inputs and scoring protocol before
requesting a separate authorized model comparison. Compare direct deterministic
checks and direct Clef against Clairic-then-Clef on the same packets, preserving
source spans, exact requests/responses, schema/pass/truncation metadata and unknown
costs. Use a proper scoring rule and selective risk together with retained useful
coverage, grouped by molecule/lineage. No model agreement counts as independent
corroboration. Further hosted calls require an explicit budget and data-transmission
approval; none are proposed as part of this implementation.
