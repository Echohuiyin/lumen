"""Input-file runtime artifact fields remain visible to the workflow."""

from pathlib import Path
import sys
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.input_artifacts import _resolve_input_path, parse_input_artifacts
from project import format_user_input, parse_input_file


def test_parse_input_rejects_unsanitized_reproducer_but_keeps_runtime_contract(tmp_path: Path):
    input_file = tmp_path / "input.txt"
    input_file.write_text(
        "\n".join(
            [
                "Bug Promote: Linux maintenance case",
                "rootfs: /tmp/debian.img",
                "test_assets_dir: /tmp/lumen-test-assets",
                "crash_report: /tmp/report.txt",
                "reproducer: /tmp/repro.syz",
                "qemu_extra_cmdline: no-kvmapf no-steal-acc init=/root/lumen-init",
                'qemu_recipe: {"machine":"q35,accel=kvm","cpu":"host","smp":"8","timeout_sec":300}',
                "maintenance_notes: use fork workers and a blocking userspace sendmsg sequence",
                "kernel_source: /tmp/linux",
                "expected_kernel_commit: bdf56c7580d267a123cc71ca0f2459c797b76fde",
                "fix_commit: 83b67cc9be9223183caf91826d9c194d7fb128fa",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="BLOCKED_INPUT_REPRODUCER_PRESENT"):
        parse_input_file(str(input_file))

    input_file.write_text(
        input_file.read_text(encoding="utf-8").replace("reproducer: /tmp/repro.syz\n", ""),
        encoding="utf-8",
    )
    fields = parse_input_file(str(input_file))
    assert fields["rootfs"] == "/tmp/debian.img"
    assert fields["test_assets_dir"] == "/tmp/lumen-test-assets"
    assert fields["qemu_extra_cmdline"] == "no-kvmapf no-steal-acc init=/root/lumen-init"
    assert fields["maintenance_notes"] == "use fork workers and a blocking userspace sendmsg sequence"
    rendered = format_user_input(fields)
    assert "qemu_extra_cmdline: no-kvmapf no-steal-acc init=/root/lumen-init" in rendered
    assert "maintenance_notes: use fork workers and a blocking userspace sendmsg sequence" in rendered

    contract = parse_input_artifacts(rendered, validate_paths=False)
    assert contract.rootfs_path == "/tmp/debian.img"
    assert contract.crash_report_path == "/tmp/report.txt"
    assert contract.reproducer_path == ""
    assert contract.test_assets_dir == "/tmp/lumen-test-assets"
    assert contract.qemu_extra_cmdline == "no-kvmapf no-steal-acc init=/root/lumen-init"
    assert contract.qemu_recipe == {
        "machine": "q35,accel=kvm", "cpu": "host", "smp": "8", "timeout_sec": 300,
    }
    assert contract.expected_kernel_commit == "bdf56c7580d267a123cc71ca0f2459c797b76fde"
    assert contract.fix_commit == "83b67cc9be9223183caf91826d9c194d7fb128fa"


if __name__ == "__main__":
    test_parse_input_preserves_qemu_runtime_declarations(Path("/tmp"))
    print("input_artifacts OK")


def test_resolve_input_path_expands_environment_variables(tmp_path, monkeypatch):
    artifact = tmp_path / "artifact.log"
    artifact.write_text("kernel log\n", encoding="utf-8")
    monkeypatch.setenv("LUMEN_TEST_ARTIFACT_DIR", str(tmp_path))

    resolved = _resolve_input_path("$LUMEN_TEST_ARTIFACT_DIR/artifact.log")
    assert resolved == artifact.resolve()

    contract = parse_input_artifacts(
        "log: $LUMEN_TEST_ARTIFACT_DIR/artifact.log",
        validate_paths=True,
    )
    assert contract.log_path == "$LUMEN_TEST_ARTIFACT_DIR/artifact.log"
    assert contract.status == "ok"

    unresolved = _resolve_input_path("$LUMEN_UNSET_ARTIFACT/artifact.log")
    assert "$LUMEN_UNSET_ARTIFACT" in str(unresolved)

def test_parse_input_expands_kernel_source_environment(tmp_path: Path, monkeypatch):
    source_dir = tmp_path / "linux"
    source_dir.mkdir()
    monkeypatch.setenv("LUMEN_TEST_KERNEL_SOURCE", str(source_dir))
    input_file = tmp_path / "input.txt"
    input_file.write_text(
        "kernel_source: ${LUMEN_TEST_KERNEL_SOURCE}\n",
        encoding="utf-8",
    )

    fields = parse_input_file(str(input_file))
    assert fields["kernel_source"] == str(source_dir)

def test_parse_optional_fix_evidence_fields():
    contract = parse_input_artifacts(
        "fix_commit: 83b67cc9be9223183caf91826d9c194d7fb128fa\n"
        "fix_patch_path: /tmp/linkwatch.patch\n",
        validate_paths=False,
    )
    assert contract.fix_commit == "83b67cc9be9223183caf91826d9c194d7fb128fa"
    assert contract.fix_patch_path == "/tmp/linkwatch.patch"
    assert {item["field"] for item in contract.evidence} >= {
        "fix_commit", "fix_patch_path",
    }

def test_parse_source_snapshot_manifest_path():
    contract = parse_input_artifacts(
        "kernel_source: /tmp/linux\n"
        "source_snapshot_manifest: /tmp/source-snapshot.json\n",
        validate_paths=False,
    )
    assert contract.source_snapshot_manifest_path == "/tmp/source-snapshot.json"
    assert {
        item["field"] for item in contract.evidence
    } >= {"kernel_source_path", "source_snapshot_manifest_path"}
