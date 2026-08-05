from agents.contracts import TestResultContract as _TestResultContract
from agents.test_expert import _promote_guest_capability_block


def test_mkfs_ocfs2_missing_binary_is_a_terminal_capability_block(tmp_path):
    ssh_output = tmp_path / "ssh-command.log"
    ssh_output.write_text(
        "LUMEN_SETUP_BLOCKED:mkfs.ocfs2:No such file or directory\n",
        encoding="utf-8",
    )
    result = _TestResultContract(
        status="failed", code="FAILED_SIGNAL_NOT_FOUND", summary="no target signal",
        kernel_feedback="revise trigger",
        artifacts={"ssh_output": str(ssh_output)},
    )

    promoted = _promote_guest_capability_block(result)

    assert promoted.status == "blocked"
    assert promoted.code == "BLOCKED_GUEST_OCFS2_TOOL_MISSING"
    assert "mkfs.ocfs2" in promoted.summary
    assert promoted.artifacts["capability_evidence"].startswith("ssh_output:")
