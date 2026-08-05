from agents.contracts import TestResultContract as _TestResultContract
from agents.test_expert import _promote_guest_capability_block


def test_guest_sve_unavailable_marker_is_a_terminal_capability_block(tmp_path):
    ssh_output = tmp_path / "ssh-command.log"
    ssh_output.write_text(
        "LUMEN_REPRO_BLOCKED reason=sve_unavailable prctl=51 errno=22\n"
        "sve_prctl=unavailable errno=22 (Invalid argument)\n",
        encoding="utf-8",
    )
    result = _TestResultContract(
        status="failed", code="FAILED_SIGNAL_NOT_FOUND", summary="no target signal",
        kernel_feedback="revise trigger",
        artifacts={"ssh_output": str(ssh_output)},
    )

    promoted = _promote_guest_capability_block(result)

    assert promoted.status == "blocked"
    assert promoted.code == "BLOCKED_GUEST_SVE_UNAVAILABLE"
    assert "SVE userspace ABI" in promoted.summary
    assert promoted.artifacts["capability_evidence"].startswith("ssh_output:")
