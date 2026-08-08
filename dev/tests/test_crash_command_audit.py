from __future__ import annotations

import hashlib
import json

from agents.crash_command_audit import CrashCommandLedger, validate_crash_command


def test_allowlist_accepts_read_only_crash_pipeline():
    assert validate_crash_command("log | tail -n 200").allowed
    assert validate_crash_command("struct mutex.owner ffff888012340000 -x").allowed


def test_allowlist_rejects_shell_syntax_and_unknown_commands():
    shell = validate_crash_command("bt -a; cat /etc/passwd")
    unknown = validate_crash_command("arbitrary_command")
    assert not shell.allowed
    assert not unknown.allowed
    assert any("shell" in error or "redirection" in error for error in shell.errors)


def test_ledger_persists_hash_and_sequence(tmp_path):
    ledger = CrashCommandLedger(tmp_path / "expert.txt")
    first = ledger.record(expert="crash_analysis", command="sys", success=True, output="kernel\n")
    second = ledger.record(expert="crash_analysis", command="bt", success=False, error="failed")
    assert first["sequence"] == 1
    assert second["sequence"] == 2
    assert first["output_sha256"] == hashlib.sha256(b"kernel\n").hexdigest()
    entries = [json.loads(line) for line in ledger.ledger_path.read_text().splitlines()]
    assert [entry["sequence"] for entry in entries] == [1, 2]
    assert entries[0]["output_file"]


def test_evidence_id_is_stable_for_same_command_and_output(tmp_path):
    ledger = CrashCommandLedger(tmp_path / "expert.txt")
    first = ledger.record(expert="crash_analysis", command="sys", success=True, output="same")
    second = ledger.record(expert="crash_analysis", command="sys", success=True, output="same")
    assert first["evidence_id"] == second["evidence_id"]
    assert first["evidence_id"].startswith("crash-")
