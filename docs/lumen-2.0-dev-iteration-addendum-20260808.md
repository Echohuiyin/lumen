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
