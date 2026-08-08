# Lumen v2.0-dev P0/P1 addendum (2026-08-08)

## Contract handoff fix

The Codex maintenance skill can emit an explicit `KERNEL_CONTRACT` with
`schema_version=1`, nested root-cause fields, object-shaped call-chain frames,
and `{path: ...}` source-file entries. The workflow now normalizes only this
marked contract into the internal Pydantic shape. It does not infer a kernel
cause or add guest actions. Source-commit, userspace-C static, path, and QEMU
call-chain gates remain unchanged.

The Codex watchdog accepts a stable `status=ok`/`status=ready` userspace-C
handoff or a precise terminal `blocked` contract. It stops only the stalled
CLI tail in the unique invocation workdir; this is not a model/provider
fallback. The downstream validators remain authoritative.

## Root-cause scoring fix

The deterministic evaluator now accepts `function:` and `Bug Promote: ... in
FUNCTION` declarations, uses the retained source evidence list when a later
contract flattened `root_cause`, excludes first-hand log/runtime paths from
the source-evidence denominator, and compares call-chain symbols without
offset punctuation. This prevents a correct source-grounded diagnosis from
being scored as unsupported while preserving the human-review boundary.

## R0 observation: smack case

Case `044fdf24e96093584232` completed two real QEMU/SSH/guest-compile loops.
Both serial logs lacked the target fault after `LUMEN_REPRO_START`; the second
loop stopped at the no-progress gate (`BLOCKED_PROGRESS_GATE`). The qcow2
overlays, serial logs, QEMU logs, state files, contracts, and C sources remain
under the case archive. Re-evaluating the archived contract with the corrected
scorer gives 91/100 (`supported`); this is a diagnosis result, not a
reproduction pass.

## R0 observation: JFS contract-shape regression

Case `0a89a7b56db04c21a656` initially produced a source-backed JFS diagnosis,
but the handoff was blocked before QEMU because the versioned contract used
object-shaped `original_call_chain`/`strict_ordered_core` entries. The adapter
accepted only string frames, so Pydantic rejected the complete contract and
the workflow replaced it with an empty blocked envelope. This was a P0 data-
loss/false-blocking bug, not an environment failure. The minimal fix converts
only explicit `frame`/`required_signature` fields to the internal string ABI,
preserves `reproducer.source_dir` and `entry_source`, and derives the first
fault signal from the first explicit strict signature. It does not infer the
missing xtree-corruption precursor or add any fixture action.

The next JFS turn used the same explicit versioned contract under the alias
`contract_type=KERNEL_CONTRACT`, and correctly declared `status=blocked` because
the only source-backed mount path needs `CAP_SYS_ADMIN` and a pre-existing
approved corrupted JFS image. The alias was initially not recognized, which
again hid the precise limitation behind an empty envelope. The follow-up
normalization accepts only that exact marker, reverses an explicitly declared
`syscall_entry_to_fault` core into the runtime fault-to-entry order, and keeps
the blocked contract terminal. It still does not turn an ABI reachability probe
into a reproduction attempt.

## R0 observation: bcachefs source-path normalization

Case `56edda805363e0a093b8` produced a precise non-privileged ABI boundary
analysis, but its C source was declared as an absolute path inside the
authenticated Codex `source_dir`. The existing source gate intentionally
accepts only relative files under that directory, so the path spelling caused
an early false block. The minimal adapter fix strips the declared directory
prefix only when it is an exact in-directory prefix; outside paths remain
unchanged and continue to fail closed. No source or runtime action is inferred.

The second bcachefs turn used the same complete shape without a top-level
marker and was therefore also reduced to an empty envelope. The follow-up
adapter accepts only a materialized multi-field contract (`status`, nested
root-cause, original chain, oracle, and userspace reproducer), maps its
explicit primary frames/crash signatures, and normalizes descriptive compiler
labels. This preserves the source-proven `CAP_SYS_ADMIN` block while keeping
arbitrary unstructured JSON rejected.

The J1939 contract additionally exposed two harmless schema variants: its
original stack used `report_stack_top_to_bottom`, while `required_signatures`
mixed a KASAN headline with function symbols and `arguments.default` carried
the executable vector. The adapter now performs only lexical classification
and explicit-default selection, preserving the strict frame order without
inventing a trigger or treating a generic frame as a fault signal.

The subsequent J1939 materialized contract separated the report stack under
`fault_report_call_trace` and stored the serial evidence in `log_signatures`;
the adapter now retains those explicit fields as the call-chain and signal
inputs. Allocation/free chains remain evidence-only and are not merged into
the runtime oracle.
