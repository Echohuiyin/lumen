"""P0 regression tests for deterministic UAF/refcount path archiving."""

from pathlib import Path
import sys

project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

from agents.knowledge_base import _render_path_analysis_appendix


def test_path_appendix_keeps_scope_paths_exclusions_and_reproducer_target():
    appendix = _render_path_analysis_appendix(
        all_paths=["get -> error -> missing put", "put -> free -> stale use"],
        max_path="put -> free -> stale use",
        kernel_contract={
            "path_analysis_required": True,
            "max_likely_path_rationale": "vmcore shows final put",
            "reproduction_target_path": "put -> free -> stale use",
            "path_analysis_scope": {
                "kernel_commit": "v6.12", "kernel_config": "CONFIG_KASAN=y",
                "entry_points": ["ioctl"], "object_type": "struct foo",
                "concurrency_model": "close races ioctl",
            },
            "excluded_paths": [{"path": "balanced close", "rationale": "source has matching put"}],
        },
        semcode_path_analysis={
            "status": "blocked",
            "blocked_reason": "semcode index missing: /kernel/.semcode.db",
        },
    )
    for expected in (
        "v6.12", "get -> error -> missing put", "put -> free -> stale use",
        "vmcore shows final put", "balanced close", "source has matching put",
        "semcode index missing",
    ):
        assert expected in appendix


if __name__ == "__main__":
    test_path_appendix_keeps_scope_paths_exclusions_and_reproducer_target()
    print("path_analysis_archive OK")
