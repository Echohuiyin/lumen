"""Disk-backed caches for expensive deterministic operations.

Layer A: extract-ikconfig output (bzImage CONFIG_* dict)
Layer B: crash command output (CrashSessionManager.run_command memo)
Layer C: initramfs cpio.gz (create_initramfs.sh output)

Each cache lives under ~/.cache/lumen/<layer>/. Keys are content hashes
of the inputs so the cache auto-invalidates when inputs change. All
writes are best-effort — cache miss falls through to the uncached path.
"""
