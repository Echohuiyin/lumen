"""Input-file runtime artifact fields remain visible to the workflow."""

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.input_artifacts import parse_input_artifacts
from project import format_user_input, parse_input_file


def test_parse_input_preserves_qemu_runtime_declarations(tmp_path: Path):
    input_file = tmp_path / "input.txt"
    input_file.write_text(
        "\n".join(
            [
                "Bug Promote: Linux maintenance case",
                "rootfs: /tmp/debian.img",
                "reproducer: /tmp/repro.syz",
                "qemu_extra_cmdline: no-kvmapf no-steal-acc init=/root/lumen-init",
                "kernel_source: /tmp/linux",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    fields = parse_input_file(str(input_file))
    assert fields["rootfs"] == "/tmp/debian.img"
    assert fields["reproducer"] == "/tmp/repro.syz"
    assert fields["qemu_extra_cmdline"] == "no-kvmapf no-steal-acc init=/root/lumen-init"
    rendered = format_user_input(fields)
    assert "qemu_extra_cmdline: no-kvmapf no-steal-acc init=/root/lumen-init" in rendered

    contract = parse_input_artifacts(rendered, validate_paths=False)
    assert contract.rootfs_path == "/tmp/debian.img"
    assert contract.reproducer_path == "/tmp/repro.syz"
    assert contract.qemu_extra_cmdline == "no-kvmapf no-steal-acc init=/root/lumen-init"


if __name__ == "__main__":
    test_parse_input_preserves_qemu_runtime_declarations(Path("/tmp"))
    print("input_artifacts OK")
