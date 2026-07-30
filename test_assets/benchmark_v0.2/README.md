# Benchmark v0.2 E2E fixture

`benchmark_inputs_v0.2.jsonl` is copied from the ARM host's
`~/kernel-benchmark-validation-v0.2/dataset/benchmark_inputs_v0.2.jsonl`.
It is the model-facing, report-time-only input set: eight fixed Linux kernel
problem reports with no Gold labels, repair commits, source URLs, or post-hoc
adjudication evidence.

The upstream benchmark metadata and scoring protocol remain authoritative:
`BENCHMARK_VALIDATION_v0.2.md`, `DATASET_CARD.md`, and `RUBRIC.md` in
`~/kernel-benchmark-validation-v0.2` on the benchmark host.

This fixture is an offline E2E contract check for the lumen workflow's input
boundary. It is not a QEMU reproducer set. The benchmark directory contains
report summaries and repro metadata, but no C/syz source files, kernel images,
or vmcore/log assets. Therefore these eight records cannot directly exercise
the QEMU runner or prove call-chain reproduction. A future QEMU E2E run must
attach the corresponding source/log/image assets while keeping Gold data out
of the model-facing input.
