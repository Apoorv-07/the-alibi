# Alibi — evaluation report

- corpus: 20 ingest blocks, 9 gold tasks, 4 planted deadline conflicts, 1 planted injection payload(s)

## Extraction + reconciliation

| variant | claims | coverage | claim recall | gold date present | verifier pass | reconciled | conflicts found | review | ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| oracle | 14 | 1.0 | 0.929 | 1.0 | 1.0 | 9/9 | 4/4 | 0 | 9 |
| rules_only | 2 | 0.0 | 0.0 | 0.0 | 1.0 | 0/9 | 0/4 | 0 | 1 |
| rules_line | 10 | 0.667 | 0.714 | 0.667 | 1.0 | 5/9 | 3/4 | 0 | 11 |
| naive_attribution | 25 | 1.0 | 1.0 | 0.778 | 1.0 | 2/9 | 4/4 | 0 | 4 |

### Attribution is the difference between a receipt and a guess

Linking each date to the *nearest* obligation in *its own document* emits **14** claims and reconciles **9/9**. Attributing every date on a line to every task on that line — what a regex-only extractor does — emits **25** and reconciles **2/9**. It is not merely noisier: a wrong *earlier* date silently wins `earliest_safe`, so over-attribution corrupts the fail-safe policy itself.

### Per-task reconciliation (oracle)

| task | reconciled | expected | ok | rule | sources |
|---|---|---|---|---|---:|
| dbms_lab4 | 2026-10-11 | 2026-10-11 | True | earliest_safe | 2 |
| os_assign3 | 2026-10-12 | 2026-10-12 | True | earliest_safe | 2 |
| daa_assign4 | 2026-10-16 | 2026-10-16 | True | earliest_safe | 1 |
| os_ia2 | 2026-10-15 | 2026-10-15 | True | earliest_safe | 1 |
| dbms_ia2 | 2026-10-16 | 2026-10-16 | True | earliest_safe | 2 |
| dbms_project | 2026-10-30 | 2026-10-30 | True | earliest_safe | 1 |
| sih_reg | 2026-09-24 | 2026-09-24 | True | earliest_safe | 2 |
| sih_idea | 2026-10-05 | 2026-10-05 | True | earliest_safe | 1 |
| sih_present | 2026-10-09 | 2026-10-09 | True | earliest_safe | 1 |

No-model lower bound: 2 rule claims (0 of them dated), verifier pass 1.0, reconciled 0/9. `coverage` 0.0 is the honest number for 'what does zero LLM get you on this corpus' — the answer is: policy-table facts, not dates, which is why the rule path and the model path are complementary rather than alternatives.

## Feasibility (ours, CP-SAT)

5/5 planted infeasible weeks flagged; **2/5** have a single policy-permitted lever that restores feasibility and **3/5** correctly report 'no remedy, reduce scope'.

**Control (why this matters):** with the student-waivable protections switched off (never-skip + review spacing) and everything else identical, **5/5** become feasible. For those 5 weeks the correct message is *'your attendance rule makes this week impossible'*, not *'you have too much to do'* — different advice, different action, and it is why the unsat core is reported rather than a generic infeasibility error.

| case | status | shortfall (h) | remedies | working remedies | core lines | feasible if safety relaxed |
|---|---|---:|---:|---:|---:|---:|
| overload-1 | INFEASIBLE | 4.0 | 3 | 0 | 1 | True |
| overload-2 | INFEASIBLE | 3.0 | 3 | 0 | 1 | True |
| overload-3 | INFEASIBLE | 2.5 | 3 | 0 | 1 | True |
| overload-4 | INFEASIBLE | 2.0 | 3 | 2 | 1 | True |
| overload-5 | INFEASIBLE | 1.5 | 3 | 2 | 1 | True |

## Baseline: baseline naive transcription

The checker flagged **5/5** plans as unexecutable (n=4 plan lines parsed; 0 means the generator produced nothing gradeable).

| case | flagged broken | hours claimed | hours required | worst overrun (h) | lines parsed | first line |
|---|---|---:|---:|---:|---:|---|
| overload-1 | True | 17.0 | 17.0 | 3.5 | 4 | 'Sun 11 Oct: DBMS Lab 4 (6.0 hours)' |
| overload-2 | True | 17.0 | 17.0 | 3.0 | 4 | 'Sun 11 Oct: DBMS Lab 4 (6.0 hours)' |
| overload-3 | True | 17.0 | 17.0 | 2.5 | 4 | 'Sun 11 Oct: DBMS Lab 4 (6.0 hours)' |
| overload-4 | True | 17.0 | 17.0 | 2.0 | 4 | 'Sun 11 Oct: DBMS Lab 4 (6.0 hours)' |
| overload-5 | True | 17.0 | 17.0 | 1.5 | 4 | 'Sun 11 Oct: DBMS Lab 4 (6.0 hours)' |

Read this as a validation of the *checker*: a plan is executable only if each day's claimed hours fit the legal budget after mandatory sessions. `lines_parsed == 0` for a placeholder bot means the checker could grade nothing — that row proves nothing about models.

## Injection guardrail

payload detected by the content screen: **False**

| case | promoted | reason | as expected |
|---|---|---|---|
| legal claim, same source | True | grounded | True |
| payload under allowed predicate | True | grounded | False |
| payload as venue | False | instructional_value_refused | True |
| honest correction is still allowed | True | grounded | True |

## Provenance — read before quoting anything above

- **oracle**: deterministic oracle through alibi.link — proves the ledger/reconciler/verifier chain, NOT a model result. No model needed a key to run here.
- **rules_only**: deterministic preparse only; this is the measured answer to 'do you even need an LLM for this corpus?'
- **rules_line**: the shipped no-model path (alibi.taskfacts line-level attribution, same verifier, same reconciler); no oracle, no model, no tuning per document
- **naive_attribution**: control condition: same corpus, same verifier, same reconciler, attribution by line instead of by proximity. Isolates the cost of skipping alibi.link.
- **ours**: CP-SAT analyze() + two controls on 5 planted overloads — executed here, no model
- **baseline_naive_transcription**: PLACEHOLDER plan generator (no model), used to validate the *checker*. It is not evidence about ChatGPT — that row needs --plan-bot llm with a provider on the laptop.
- **injection**: executed here against alibi.ground + alibi.router (no model needed)

## Still unproven (do not claim these on a slide)

- Real-model extraction accuracy (needs a provider: `--llm ollama` / a Gemini key).
- Real ChatGPT/Claude/Gemini plan quality (`--plan-bot llm`). The naive row only validates the grader.
- Latency and cost per artifact on the target hardware (needs the 6 GB GPU).
- Any corpus larger than this 4-artifact demo set.
