"""Offline routing checks for the bounded Kernel Expert/Test Expert loop."""

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from graph.rn_router import route_after_test


def _state(**updates):
    state = {
        "tryout_count": 1,
        "max_tryouts": 10,
        "call_chain_consistent": False,
        "test_attempt_contract": {"status": "failed", "code": "FAILED_CALL_CHAIN_MISMATCH"},
    }
    state.update(updates)
    return state


def test_first_consistent_call_chain_finishes_immediately():
    assert route_after_test(_state(call_chain_consistent=True)) == "knowledge_base"


def test_nonmatching_call_chain_returns_to_kernel_expert_before_limit():
    assert route_after_test(_state(tryout_count=9)) == "kernel_expert"


def test_tenth_nonmatching_call_chain_finishes_as_failure():
    assert route_after_test(_state(tryout_count=10)) == "knowledge_base"


def test_environment_block_finishes_without_more_tryouts():
    assert route_after_test(_state(
        test_attempt_contract={"status": "blocked", "code": "BLOCKED_BASE_IMAGE_MISSING"},
    )) == "knowledge_base"


if __name__ == "__main__":
    for test in (
        test_first_consistent_call_chain_finishes_immediately,
        test_nonmatching_call_chain_returns_to_kernel_expert_before_limit,
        test_tenth_nonmatching_call_chain_finishes_as_failure,
        test_environment_block_finishes_without_more_tryouts,
    ):
        test()
    print("test_test_loop OK")
