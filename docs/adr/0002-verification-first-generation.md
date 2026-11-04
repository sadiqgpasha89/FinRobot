# ADR-0002: Numbers are registered, cited and verified by code

* Status: Accepted (carried over from 0.1, unchanged in 0.2.0) · Date: 2026-11-05

## Context
An equity research answer is worthless if one number is wrong, and LLMs produce wrong numbers fluently. Live runs of 0.1 found, among others: a
router that invented a year; "3 bps" for a 3.4-point margin change; a real figure labelled with the wrong quarter; net debt divided by one
quarter's EBITDA (11.0x instead of 2.93x) with every input real and cited. Prompting alone fixed none of these durably.

## Decision
Make a wrong number *unable to reach the reader* rather than unlikely:
1. Every figure and passage an agent can see is **registered** in a `FactBook` under an id (`F#`, `S#`) by code.
2. All arithmetic is a tool (`compute_metric`, `calc`); `calc` only accepts registered ids, **infers the unit** and tracks the **period basis**.
3. Periods are parsed from the question **in code**; tools take `quarters_ago`, never a year.
4. A deterministic **Evaluator** ties every unit-bearing number in the prose to a registered fact or a cited passage, and checks that named periods
   are covered by the cited facts.
5. A failed draft gets **one** repair attempt with the exact issues; what still fails is **deleted**. A wrong-period answer becomes "no verified answer".
6. A second, model-based **grounding check** covers qualitative claims the numeric check cannot see.

## Alternatives considered
| Option | Why not |
|---|---|
| Larger model + better prompt | not deterministic; each fixed failure re-appeared in a new shape |
| LLM-as-judge only | a judge shares the generator's failure modes; kept only for qualitative claims, where nothing deterministic exists |
| Function calling without registration | no way to know a number in prose came from a tool |
| Structured output for the whole answer | numbers still originate in the model; ties nothing to sources |

## Consequences
* (+) 12 of 12 figures in a live end-to-end answer tied to database rows; each past failure has a regression test.
* (+) Verification outcomes are measurable: they become Langfuse/LangSmith **scores** (`verification_ok`, `figures_tied_ratio`, ...).
* (−) The system sometimes returns "no verified answer" instead of an answer (about 1 in 6–8 simple figure questions with gpt-oss:20b). This is
  the intended trade: silence over a confident error.
* (−) Qualitative wording can still slip past the grounding check; it is a model, not a proof. The README says so.
