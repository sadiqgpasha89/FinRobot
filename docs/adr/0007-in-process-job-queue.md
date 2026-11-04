# ADR-0007: Jobs run on a bounded in-process worker pool

* Status: Accepted (revisit at scale-out) · Date: 2026-11-05

## Context
The binding constraint is one local GPU with one model slot: two runs in parallel go no faster, they only interleave model loads. Run volume is a few
per hour. A distributed queue would add a broker, a worker image and failure modes for no throughput gain.

## Decision
* `JobRunner`: a `ThreadPoolExecutor` (`FINROBOT_MAX_WORKERS`, default 1) with a bounded backlog (`FINROBOT_MAX_QUEUE`, default 20). A full queue returns
  `429` + `Retry-After` and records the rejected run.
* The run row is written **before** queueing, so clients can poll immediately.
* State is in Postgres, not in the process: after a restart `mark_orphaned_runs()` flags in-flight runs `interrupted`, and reports can be resumed.
* Prometheus metrics: queue depth, run durations and outcomes, HTTP latency by templated path.

## Alternatives considered
| Option | Why not (now) |
|---|---|
| Celery / RQ / Arq + Redis | broker + worker deployment and a second place for state, for no throughput benefit on one GPU |
| Temporal | see ADR-0001 / 0006 |
| Kubernetes Jobs per run | cold-start cost of loading models dwarfs the run |

## Consequences
* (+) One process to deploy; behaviour is easy to reason about; tests exercise it directly.
* (−) Not horizontally scalable: two API replicas would each own a queue and would not see each other's in-flight runs (a replica restart could mark
  another replica's live run `interrupted`). Run a single API replica, or move to a shared queue.
* **Trigger to revisit:** a hosted model removes the GPU constraint and run volume needs more than one worker, or a second replica is required for
  availability. The `service.execute_*` functions are already queue-agnostic; a worker would call them unchanged.
