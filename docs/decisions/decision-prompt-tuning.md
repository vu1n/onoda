# Decision-model wording is tuned against grader labels, offline, and only the wording changes

**Status at migration:** draft. This migration does not change that status.


<a id="tune-wording-only"></a>
## Tune only the question wording, against labels the grader produced

`onoda decide tune` (`onoda.decision_tune`) runs GEPA over a decision model's question
`instructions` and scores each candidate by the probability the model puts on the expected
answers of labelled examples. It is reusable for any System One task: examples are
`onoda.decision-example/1` JSONL (id, state, expected answer per question) plus a questions
template. For VTE, `onoda vte shadow-dataset` labels each job a claim cites from the grader
(`ready` yes when a claim from it was identified; no when it is cited but no citing claim was
identified, or uncited and the parser saw it fail; uncited jobs that finished are left out), and `onoda vte shadow --instructions` uses
the tuned wording and records its digest.

**Why.** The owner wants the shadow levers to get better (2026-10-03), and the shadow wording
had measurable biases (A68). Tuning only the wording keeps a choice question's options (the
ladder rules) single-sourced, so GEPA tuning the ladder and tuning the shadow questions never
drift apart; labelling from the grader keeps the grader the only judge.

### Invariant
- Only `<question>.instructions` components are tunable; any other key is refused.
- Labels are grader outcome words, never reference values; the tuner never grades a run.
- Every live setting is explicit and capped: `--enable-live-decisions`, decision-call,
  reflection-call and dollar caps; reflection fails closed on cost (`LiveReflection`).
- Answers are cached by request sha256, so a rerun replays and spends nothing.
- The tuner writes files (result, best wording, answer cache), not store records: it is
  research tooling over shadow answers that no grader or gate reads
  (`decision-models-shadow-only`).

### Carried forward from the assumptions register

#### A70: How is a decision model's question wording tuned?

Register status: decided (default, decision tune). Owner milestone: decision tune.

`onoda decide tune` (`onoda.decision_tune`), reusable for any System One task: a dataset of examples (`onoda.decision-example/1` JSONL: id, state, expected answer per question) and a questions template; only each question's `instructions` is tunable, so a choice question's options (for VTE, the ladder rules) stay single-sourced. Search is GEPA (the project's one optimizer), reflection through `LiveReflection` (OpenRouter, fail-closed cost). Score per example: mean probability the model put on the expected answers; the reported best is the highest mean on a held-out split chosen by hash of (seed, id). Answers are cached on disk by request sha256, so reruns replay and an interrupted search resumes; decision calls, reflection calls and reported dollars are capped, and a dropped connection is retried twice. For VTE, `onoda vte shadow-dataset` labels each job a claim cites from the grader: `ready` yes when a claim from it was identified; no when it is cited but no citing claim was identified (rejected or unidentified), or uncited and the parser saw it fail (no normal termination, SCF or CASSCF not converged); uncited jobs that finished are left out (Vu, 2026-10-08: the grader had rejected no claim, so the old rule gave one class). Labels are outcome words and parser status, never reference values. `onoda vte shadow --instructions` uses tuned wording and records its digest. The tuner writes files (result, best wording, cache), not store records: it is research tooling over shadow answers that no grader or gate reads (A67).
