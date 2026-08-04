from types import SimpleNamespace

from agents.tool_expert import (
    _disassemble_vmlinux,
    _extract_disassembly_targets,
    _extract_register_hints,
)


def test_disassembly_is_optional_when_vmlinux_is_unavailable(tmp_path):
    log = "general protection fault, PC is at gadget_dev_open+0x14/0x80"
    assert _disassemble_vmlinux(str(tmp_path / "missing-vmlinux"), log) is None


def test_extracts_fault_frame_and_register_hints():
    log = """
    general protection fault
    PC is at gadget_dev_open+0x14/0x80
    LR: __fput+0x20/0x90
    FAR: 0x8 x0: 0x0 SP: 0xffff000000001000
    """
    assert _extract_disassembly_targets(log)[:2] == ["gadget_dev_open", "__fput"]
    hints = _extract_register_hints(log)
    assert hints["far"] == "0x8"
    assert hints["x0"] == "0x0"


def test_disassembly_uses_deterministic_objdump_evidence(tmp_path, monkeypatch):
    vmlinux = tmp_path / "vmlinux"
    vmlinux.write_bytes(b"ELF placeholder")
    monkeypatch.setenv("LUMEN_OBJDUMP", "/opt/objdump")

    def fake_run(command, **kwargs):
        assert command[:3] == ["/opt/objdump", "-d", "--line-numbers"]
        assert "--disassemble=gadget_dev_open" in command
        assert str(vmlinux) in command
        return SimpleNamespace(returncode=0, stdout="gadget_dev_open:\n\tldr x0, [x1]", stderr="")

    monkeypatch.setattr("agents.tool_expert.subprocess.run", fake_run)
    log = "general protection fault\nPC is at gadget_dev_open+0x14/0x80\nFAR: 0x8"
    evidence = _disassemble_vmlinux(str(vmlinux), log)

    assert evidence is not None
    assert evidence["kind"] == "vmlinux_disassembly"
    assert evidence["status"] == "ok"
    assert evidence["targets"] == ["gadget_dev_open"]
    assert "ldr x0" in evidence["output"]
    assert evidence["register_hints"]["far"] == "0x8"
