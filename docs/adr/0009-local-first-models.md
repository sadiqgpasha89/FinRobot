# ADR-0009: Local models by default; hosted providers by configuration

* Status: Accepted · Date: 2026-11-05

## Context
Filings are public, but analyst questions, assumptions and drafts may not be. The development machine has a 16 GB GPU; cost per run should be zero. Quality on long
reasoning is better on hosted frontier models.

## Decision
Default to Ollama (`gpt-oss:20b` for figures/fact-checks, `gemma4:e4b` for narrative, `nomic-embed-text` for search). Any role can move to a hosted
provider by a prefix in its model string, with the provider's LangChain package as an optional extra. The two-role split exists because the exact-number role and
the prose role have different best models and, on one GPU, different load costs.

## Alternatives considered
| Option | Why not |
|---|---|
| Hosted only | cost and data-egress concerns for a research tool used on private questions |
| One model for everything | worse prose or worse tool use; and verification prefers a model that is not also the writer |

## Consequences
* (+) Runs cost nothing; nothing leaves the machine unless tracing is enabled.
* (+) Live check in 0.2.0: full question → verified answer in 32 s on gpt-oss:20b + gemma4:e4b (RTX 4090 laptop), 14.7k input / 1.2k output tokens.
* (−) Local models are less reliable at tool use than hosted ones (the documented ~1-in-8 "no verified answer"). Moving the figures role to a hosted model is the first
  lever to pull if that rate matters; the evals measure it.
