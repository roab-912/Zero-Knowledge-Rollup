# Experiment 3: observed stage dominance

Source: `/home/r24barbi/Zero-Knowledge-Rollup/bench-out/20261104_131449`. Setup is excluded. Each prover is a separate
pipeline; verification is kept with its source backend. Successful repetitions
are aggregated with population SD. Failed/incomplete proving repetitions are
discarded together. Sizes missing any selected stage are excluded (manifest).

`01_stage_times` compares mean durations with one SD. `02_stage_dominance`
shows T_stage/max(T_stage), with stars for the largest mean(s).
`stage_metrics.csv`, `dominance.csv`, and `transitions.csv` give the underlying
values. Transitions are brackets between measured sizes, without interpolation,
confidence intervals or a claim about the first integer crossing. Pairwise
crossings below another stage's envelope are distinguished from dominant-stage
switches. The tie tolerance is numerical, not a statistical significance test.

`05_proving_vs_verify` displays proof, witness and verification separately
when the source contains split measurements. The proof curve excludes witness
calculation. Legacy fullprove stays combined and is labeled proof (fullprove);
no witness curve is inferred from that combined duration.
`proving_comparison.csv` separately compares the complete witness+proof phase
(same definition as Exp 1) with verification.
`proving_transitions.csv` contains these phase crossings. The proving SD is
computed after summing steps within each repetition, preserving covariance.
The combined phase is excluded from the decomposed stage envelope to avoid
counting both a total and its components as independent stages.

`03_derived_rates` compares N/max(T_stage), an ideal pipeline bound requiring
independent stage allocations and overlap, with N/sum(T_stage), a sequential
estimate. Shared CPU/IO work can invalidate the independent-stage assumption.
The current ZKRollup holds a lock over compute_batch, including settlement;
it does not implement the ideal pipeline. Release intervals are optional,
explicit assumptions, never inferred from setup or inclusion latency.

ZK command timings include process/Node/Docker startup. Local timers run inside
a worker after imports and fixture initialization. The worker uses real
Executor, State, Batch, Transaction and DataAvailability classes and the shared
input writer. The workload has N independent transfers and 2N accounts.
Executor logging remains part of its measured cost. Account count, compression,
logging and filesystem caching affect results. These local operations are
isolated microbenchmarks, not a live trace or the same batch as a legacy run.
Very short local CPU timings may be zero at the platform clock resolution
(notably on Windows); this does not establish absence of CPU work.

`04_resources_*` shows CPU work and available sampled RSS/IO, separately from
elapsed times. Local operation memory/IO are unavailable, not zero. Their
worker's process metrics including imports/startup remain in source steps.csv.
Docker system-scope counters are retained in CSV but omitted from resource
plots because they include other host processes. RAM is occupancy, not work
converted to seconds. No CPU, disk or network capacity is calibrated here.

Local verification is not L1 verification time. L1 submission, RPC, inclusion,
blob propagation, KZG and finality are unmeasured. Use live instrumentation or
the existing settlement benchmarks for those; inclusion latency alone does
not establish a service capacity. Synthetic SNARK and local workloads must
match, and allocations must be controlled, before testing physical resource
bottleneck predictions from the paper.

Local rollup stages absent from this dataset: **True**.
Legacy fullprove stays explicitly combined; witness/proof are never invented.
