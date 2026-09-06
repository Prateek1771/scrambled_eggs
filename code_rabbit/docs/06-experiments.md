# 06 — Experiments

Every measurement in this document is defined **before the code exists**, with its pass bar
written down in advance. That ordering is the point. A project whose entire argument is
*verify, do not trust* cannot decide after the fact which numbers it wanted.

The claim under test is narrow and should be read carefully:

> **A mandatory proof gate drives false positives to approximately zero, at a cost in recall
> that is worth measuring rather than assuming.**

Nothing here claims TRIBUNAL is a better reviewer than anything else. It claims the gate does a
specific thing, at a specific price, and the price is the interesting half.

## What is being held constant

| Held fixed | Why |
|---|---|
| Target repository, pinned SHA | Repository difficulty dominates every metric |
| Mutation set (committed patch + answer key) | The ablations must vary one thing |
| Model, temperature, seed | Otherwise the model is a confound |
| Container image digest | Dependency drift silently changes results |
| `N=3` re-runs per commit | Flake sensitivity affects verdict counts directly |

Every result is reported with the manifest that produced it, and the LLM cache that makes it
replayable is committed alongside — see [04](04-agents.md). Anyone can re-derive these numbers
without an API key.

---

## E1 — Precision and recall against the answer key

**Question.** How many injected defects does the gated reviewer prove, and does it ever prove
something that is not there?

**Method.** One gym run. `K=15` mutations spanning all six classes from [03](03-gym.md).
Score with the traceback-first matcher.

**Reported.**

| Metric | Definition |
|---|---|
| Precision | hits / (hits + false positives) |
| Recall | hits / K |
| F1 | harmonic mean |
| Suppression ratio | graveyard size / proven count |
| Per-class recall | The six mutation classes, separately |

**Pass bar, committed in advance:**

- **Precision ≥ 0.95.** The differential rule should make false positives structurally rare. If
  precision lands below this, the gate is not doing what the design says it does, and that is a
  finding worth publishing.
- **Recall ≥ 0.40.** Deliberately modest. The gate is expected to cost recall and this is a
  floor for "the approach is viable at all," not a target.
- **Per-class recall reported without aggregation.** Boundary and inverted-predicate mutations
  are expected to score far above state-leak and resource-leak ones. An aggregate that hides
  that spread would be the most misleading number this project could publish.

**What would falsify the design.** Precision below 0.9 with a correctly implemented differential
rule would mean the rule does not prevent what [02](02-burden-of-proof.md) claims it prevents.

---

## E2 — The ablation that actually matters

**Question.** What does the proof gate cost?

This is the headline experiment. E1 without E2 is a number with no baseline.

**Method.** The identical reviewer, the identical hypotheses, the identical run — with the gate
removed. Every hypothesis that `hypothesise` produces is published as a comment, exactly as a
conventional reviewer would. Because the LLM cache makes hypothesis generation deterministic,
**both arms see literally the same hypotheses**, which is a cleaner control than most published
comparisons in this space manage.

**Reported.**

| | Gated | Ungated |
|---|---|---|
| Comments published | | |
| Hits | | |
| False positives | | |
| Precision | | |
| Recall | | |
| Comments a human must read | | |

**Pass bar:**

- **Ungated precision should land somewhere near the category norm.** If the ungated arm scores
  above ~0.6, the gym is too easy and the whole comparison is flattering itself. That would be a
  problem with the benchmark, not a triumph.
- **The recall delta is the deliverable.** Whatever it is, it gets published. A gate that costs
  60% of recall for perfect precision is a genuinely different product with a genuinely narrower
  market, and pretending otherwise would be the exact dishonesty this project is built against.

**This is the experiment most likely to embarrass the design, which is why it is second.**

---

## E3 — Cost per proven finding

**Question.** Is proof affordable?

**Method.** Instrument E1's run for tokens, dollars, wall clock and container count. Report per
proven finding, not per run — a run that proves nothing has infinite cost per finding, and that
should be visible.

**Reported.**

| Metric | Unit |
|---|---|
| Dollars per proven finding | USD |
| Seconds per proven finding | wall clock |
| Containers per proven finding | count |
| Fraction of spend on dismissed hypotheses | % |
| Cache hit rate on a repeat run | % (expected: 100) |

**Pass bar:**

- **Under $0.10 per proven finding** on the fast tier. Above roughly $0.50 the design is a
  research curiosity rather than something anyone would run per pull request.
- **Under 60 seconds per proven finding**, single-machine, with adjudications fanned out.
- **The spend-on-dismissals fraction is expected to be the majority**, and that is fine. It is
  the price of the guarantee and it should be stated as such rather than minimised.

---

## E4 — The silence test

**Question.** Can it shut up?

**Method.** The `K=0` control from [03](03-gym.md). The reviewer is handed a diff with
formatting-only changes and no injected defect.

**Pass bar: exactly zero published findings. Not "few." Zero.**

There is nothing to find. Any `PROVEN` output is a false positive with no available excuse, and
a single one fails the experiment outright.

**Reported alongside:** the graveyard size. A large graveyard on a clean diff is not a failure —
it is the gate visibly doing its job, and it is the single best illustration of the whole thing.
A reviewer that generated 30 hypotheses about nothing and published none of them is exactly the
behaviour being argued for.

**Prerequisite.** Run health must pass first. A flaky target suite produces phantom base/head
differences and would fail E4 for reasons that have nothing to do with the reviewer.

---

## E5 — Flake sensitivity

**Question.** Is `N=3` enough, and what does raising it buy?

**Method.** Re-run E1 at `N=1`, `N=3`, `N=5`. Compare verdict distributions.

**Reported.** Verdict counts per N; findings that change verdict between settings; wall-clock
cost per additional repetition.

**Expected and pre-declared:** `N=1` will publish some findings that `N=3` quarantines. Those
are false positives that repetition catches, and their count is the justification for the
default. `N=5` is expected to be nearly identical to `N=3`, and if it is not, the target
repository is unstable and E1's numbers are void.

**This experiment exists to make the `N=3` default an evidenced choice rather than a guess**, and
to bound the claim: [02](02-burden-of-proof.md) admits `N=3` will miss a rare race, and this is
where that admission gets a number attached to it.

---

## E6 — Robustness of the intent check

**Question.** How often does the gate convict a deliberate behaviour change?

This targets the design's dominant remaining false-positive channel, named in
[02](02-burden-of-proof.md).

**Method.** A second mutation set where every change is an *intentional* behaviour change —
correct, deliberate, and described in an accompanying pull request summary. The correct output
is zero bug findings; anything proven should be routed to `BEHAVIOUR_CHANGE`.

**Reported.** Count misfiled as bugs; count correctly routed; count suppressed by the
"tests changed in the diff are authority" rule versus by the description read.

**Pass bar:** no hard bar, and that is deliberate. This is the weakest part of the design and the
experiment exists to size the weakness honestly. A misfiling rate above ~20% would mean the
intent handling needs to be a document of its own rather than a section.

---

## What is not measured, and why

- **No comparison against CodeRabbit, Greptile or any shipping product.** That requires accounts,
  installations and a fair harness for tools with different interfaces, and an unfair comparison
  is worse than none. It is M7 in [07](07-roadmap.md), and until then no competitive claim is
  made anywhere in these documents.
- **No claim of generality.** One repository. Every number is a measurement of TRIBUNAL on
  `semver`, and saying otherwise from a single target would be indefensible.
- **No developer study.** Whether proven findings *feel* more trustworthy to a human is the
  question that actually determines whether this is a product, and it needs users, not a gym.
- **No performance-regression detection.** Timing assertions on a laptop are not measurements.
  Deferred, and listed as such.

## Relationship to the Martian benchmark

The [Martian Code Review Bench](https://www.coderabbit.ai/blog/coderabbit-tops-martian-code-review-benchmark)
is the right external validation for this design, and the two measure genuinely different
things:

| | Martian | TRIBUNAL's gym |
|---|---|---|
| Ground truth | Did a developer act on the comment | An injected defect with a witness |
| Scale | ~300,000 real pull requests | 15 mutations in one repository |
| Realism | **Real work, real reviewers** | Synthetic, from a chosen taxonomy |
| Controllable | No | **Yes** — that is its only advantage |
| Needs accounts | Yes | No |

Martian open-sourced its dataset, judge prompts and evaluation pipeline. That makes it the
natural next step once the harness works: run the gated reviewer against their corpus and report
precision on the same axis as everyone else.

Until that is done, **no number in this document is comparable to a Martian score**, and none of
them are presented as if they were.

## The honest limits

- **N is tiny.** Fifteen mutations in one repository. Every figure here has error bars wide
  enough to swallow most of the differences being discussed, and the raw counts in
  [05](05-ui.md) are shown rather than percentages for exactly this reason.
- **The answer key is synthetic**, so recall is recall against a chosen distribution of
  runtime-observable defects. It is not recall against real bugs and is never labelled as such.
- **The ungated arm in E2 is a strawman by construction.** It is this system with its gate
  disabled — not a product anyone ships. It isolates the gate's effect and it is not a
  competitive benchmark.
- **The Saboteur and the Reviewer share a model family** and may share blind spots, which would
  inflate recall. The hand-authored M1 mutation set is a partial control and does not resolve it.
- **Cost figures are model-dependent and will be wrong within months.** They are reported with
  the model name and date attached, and should be read as an order of magnitude.
- **Every one of these experiments is run by the person who designed the system.** The committed
  cache and pinned manifests exist so that a skeptical reader can re-derive the numbers rather
  than take them on faith, which is the only real answer to this problem.
