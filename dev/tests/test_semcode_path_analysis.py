"""P2 tests for deterministic semcode UAF/refcount event graphs."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

from agents.contracts import KernelExpertOutput
from agents.kernel_expert import _apply_semcode_path_analysis
from agents.semcode_path_analysis import (
    SemcodeMcpClient,
    SemcodeFunction,
    resolve_kernel_commit,
    resolve_kernel_source_for_commit,
    verify_semcode_target,
    analyze_uaf_paths,
    configured_semcode_timeout_sec,
    extract_semcode_entry_points,
    render_semcode_analysis_context,
    _without_database_args,
)
from agents.input_artifacts import parse_input_artifacts
from llm_config import get_llm_with_config, load_config


class _FixedSemcodeClient:
    """Dependency injection, not a fallback: fixture models a parsed MCP reply."""

    def __init__(self, target: str):
        self.target = target

    def _call(self, tool_name: str, arguments: dict) -> str:
        if tool_name == "list_branches":
            return (
                "=== Indexed Branches ===\n\n"
                f"  lumen-target/{self.target} ({self.target[:8]})\n"
                "    Status: up-to-date\n"
            )
        if tool_name == "indexing_status":
            return "=== Indexing Status ===\nStatus: Completed (1 files processed)\n"
        raise AssertionError(f"unexpected meta-tool: {tool_name}")

    def find_function(self, name: str) -> SemcodeFunction:
        assert name == "foo_ioctl"
        return SemcodeFunction(
            name=name,
            location="drivers/foo.c:42",
            direct_calls=("kref_get", "queue_work", "kref_put", "kfree", "foo_access"),
            body="""
                kref_get(&foo->ref);
                spin_lock(&foo->lock);
                if (bad)
                    goto err_put;
                queue_work(foo_wq, &foo->work);
            err_put:
                kref_put(&foo->ref, foo_release);
                kfree(foo);
                foo_access(foo);
            """,
        )


def _source_tree(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "linux"
    source.mkdir()
    (source / ".semcode.db").mkdir()
    (source / "base.c").write_text("int base(void) { return 0; }\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(
        ["git", "-C", str(source), "add", "base.c"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(source), "-c", "user.name=test", "-c", "user.email=test@example.invalid",
         "commit", "--allow-empty", "-qm", "fixture"],
        check=True,
    )
    executable = tmp_path / "semcode-mcp"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    return source, executable


def test_semcode_client_keeps_session_alive_during_background_index(tmp_path):
    """A transient indexing response is retried without closing the MCP server."""
    fake_server = tmp_path / "fake-semcode-mcp.py"
    fake_server.write_text(
        """#!/usr/bin/env python3
import json
import sys

seen = {}
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    request_id = request["id"]
    if request.get("method") == "initialize":
        result = {"content": [{"type": "text", "text": "initialized"}]}
    elif request.get("method") == "tools/call":
        name = request.get("params", {}).get("name")
        seen[name] = seen.get(name, 0) + 1
        if name == "find_function" and seen[name] == 1:
            text = "Database is currently being indexed (Analyzing files)."
        elif name == "find_function":
            text = "Function: foo_ioctl\\nFile: drivers/foo.c:42\\nBody:\\nfoo_access();\\n"
        elif name == "find_calls":
            text = "Direct calls:\\n1. foo_access\\n"
        elif name == "indexing_status":
            text = "=== Indexing Status ===\\nStatus: Completed (1 files processed)\\n"
        else:
            text = "ok"
        result = {"content": [{"type": "text", "text": text}]}
    else:
        result = {"content": [{"type": "text", "text": "ok"}]}
    print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}), flush=True)
""",
        encoding="utf-8",
    )
    fake_server.chmod(0o755)
    source = tmp_path / "linux"
    source.mkdir()
    (source / ".semcode.db").mkdir()
    client = SemcodeMcpClient(
        command=str(fake_server), args=(), kernel_source_path=str(source),
        git_sha="a" * 40, timeout_sec=3,
    )

    function = client.find_function("foo_ioctl")

    assert function.name == "foo_ioctl"
    assert function.location == "drivers/foo.c:42"
    assert function.direct_calls == ("foo_access",)


def test_semcode_timeout_is_deployment_configurable(monkeypatch):
    monkeypatch.setenv("LUMEN_SEMCODE_TIMEOUT_SEC", "480")
    assert configured_semcode_timeout_sec() == 480
    monkeypatch.setenv("LUMEN_SEMCODE_TIMEOUT_SEC", "bad")
    assert configured_semcode_timeout_sec() == 900


def test_semcode_client_keeps_parseable_functions_when_wrapper_is_missing(tmp_path):
    """Missing generated wrappers do not discard valid exact-commit entries."""
    source = tmp_path / "linux"
    source.mkdir()
    (source / ".semcode.db").mkdir()
    client = SemcodeMcpClient(
        command="/bin/true", args=(), kernel_source_path=str(source),
        git_sha="a" * 40,
    )
    calls = []

    def fake_call_many(requests):
        calls.append(list(requests))
        if len(calls) == 1:
            return [
                "Function: foo_ioctl\nFile: drivers/foo.c:42\nBody:\nfoo_access();\n",
                "Function 'generated_wrapper' not found at git SHA ...",
            ]
        return ["Direct calls:\n1. foo_access\n", ""]

    client._call_many = fake_call_many

    functions = client.find_functions(["foo_ioctl", "generated_wrapper"])

    assert [function.name for function in functions] == ["foo_ioctl"]


def test_semcode_path_analysis_reuses_exact_cached_evidence(tmp_path):
    """A complete exact-commit batch must avoid a second MCP function query."""
    source, executable = _source_tree(tmp_path)
    target = subprocess.check_output(
        ['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True,
    ).strip()
    cached = {
        'status': 'ok',
        'kernel_source': str(source.resolve()),
        'expected_kernel_commit': target,
        'failures': [],
        'entries': [{
            'function': 'foo_ioctl',
            'result': (
                'Function: foo_ioctl (git SHA: %s)\n'
                'File: drivers/foo.c:42\n'
                'Calls: 5 functions\n'
                '  1. kref_get\n'
                '  2. queue_work\n'
                '  3. kref_put\n'
                '  4. kfree\n'
                '  5. foo_access\n'
                'Body:\n'
                'kref_get(&foo->ref);\n'
                'queue_work(foo_wq, &foo->work);\n'
                'kref_put(&foo->ref, foo_release);\n'
                'kfree(foo);\n'
                'foo_access(foo);\n'
            ) % target,
        }],
    }

    class _NoFunctionQuery(_FixedSemcodeClient):
        def find_function(self, name: str) -> SemcodeFunction:
            raise AssertionError('cache miss unexpectedly queried MCP function')

    result = analyze_uaf_paths(
        kernel_source_path=str(source),
        entry_points=['foo_ioctl'],
        expected_kernel_commit=target,
        semcode_command=str(executable),
        client=_NoFunctionQuery(target),
        cached_evidence=cached,
    )

    assert result.status == 'ok'
    assert result.analysis is not None
    assert result.scope.entry_points == ['foo_ioctl']


def test_semcode_event_graph_calculates_deltas_and_declares_boundaries(tmp_path):
    source, executable = _source_tree(tmp_path)
    target = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True,
    ).strip()
    result = analyze_uaf_paths(
        kernel_source_path=str(source), entry_points=["foo_ioctl"],
        expected_kernel_commit=target,
        semcode_command=str(executable), client=_FixedSemcodeClient(target),
    )

    assert result.status == "ok"
    assert result.analysis is not None
    assert result.analysis.coverage.normal_paths_considered is True
    assert result.analysis.coverage.error_paths_considered is True
    assert result.analysis.coverage.async_paths_considered is True
    assert result.analysis.coverage.concurrency_paths_considered is True
    assert any(path.terminal_state == "uaf" for path in result.analysis.paths)
    for path in result.analysis.paths:
        assert path.net_delta == sum(event.ref_delta for event in path.events)
    assert any("direct callees only" in item for item in result.analysis.coverage.limitations)

    contract = _apply_semcode_path_analysis(
        KernelExpertOutput(status="blocked", build_status="skipped"), result,
    )
    assert contract.path_analysis_required is True
    assert contract.uaf_analysis is not None
    assert contract.max_likely_path in contract.all_possible_paths
    assert contract.reproduction_target_path == contract.max_likely_path




def test_semcode_resolves_unique_abbreviated_commit_to_full_sha(tmp_path):
    source, executable = _source_tree(tmp_path)
    target = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True,
    ).strip()
    abbreviated = target[:39]
    result = verify_semcode_target(
        kernel_source_path=str(source), expected_kernel_commit=abbreviated,
        semcode_command=str(executable), client=_FixedSemcodeClient(target),
    )
    assert result["status"] == "ok"
    assert result["resolved_commit"] == target
    assert result["evidence"][0]["expected_commit"] == target
    assert result["evidence"][0]["declared_commit"] == abbreviated


def test_semcode_scope_uses_declared_target_commit(tmp_path):
    source, executable = _source_tree(tmp_path)
    target = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True,
    ).strip()
    result = analyze_uaf_paths(
        kernel_source_path=str(source), entry_points=["foo_ioctl"],
        expected_kernel_commit=target,
        semcode_command=str(executable), client=_FixedSemcodeClient(target),
    )
    assert result.status == "ok"
    assert result.scope.kernel_commit == target


def test_kernel_commit_rejects_unresolvable_prefix(tmp_path):
    source, _ = _source_tree(tmp_path)
    result = verify_semcode_target(
        kernel_source_path=str(source), expected_kernel_commit="deadbee",
        semcode_command="/bin/true",
        client=object(),
    )
    assert result["status"] == "blocked"
    assert "cannot uniquely resolve" in result["blocked_reason"]


def test_semcode_requires_explicit_entry_point_and_never_falls_back(tmp_path):
    source, executable = _source_tree(tmp_path)
    result = analyze_uaf_paths(
        kernel_source_path=str(source), entry_points=[], semcode_command=str(executable),
    )
    assert result.status == "blocked"
    assert "entry point" in result.blocked_reason
    assert result.analysis is None


def test_semcode_blocks_when_target_object_is_not_indexed(tmp_path):
    source, executable = _source_tree(tmp_path)
    target = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True,
    ).strip()
    result = verify_semcode_target(
        kernel_source_path=str(source), expected_kernel_commit=target,
        semcode_command=str(executable),
        client=type("NoIndex", (), {
            "_call": lambda self, name, arguments: (
                "No branches have been indexed yet." if name == "list_branches"
                else "Status: Not started"
            )
        })(),
    )
    assert result["status"] == "blocked"
    assert "not proven" in result["blocked_reason"]


def test_kernel_source_is_pinned_to_target_head_with_readable_tree(tmp_path):
    source, _ = _source_tree(tmp_path)
    target = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True,
    ).strip()
    (source / "later.c").write_text("int later(void) { return 0; }\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(source), "-c", "user.name=test", "-c", "user.email=test@example.invalid",
         "add", "later.c"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(source), "-c", "user.name=test", "-c", "user.email=test@example.invalid",
         "commit", "-qm", "later"],
        check=True,
    )
    pinned = resolve_kernel_source_for_commit(
        str(source), target, workspace_root=str(tmp_path / "work"),
    )
    assert Path(pinned) != source
    assert subprocess.check_output(
        ["git", "-C", pinned, "rev-parse", "HEAD"], text=True,
    ).strip() == target
    assert (Path(pinned) / "base.c").is_file()
    assert not (Path(pinned) / "later.c").is_file()
    assert (Path(pinned) / ".semcode.db").is_symlink()



def test_configured_worktree_root_overrides_session_directory(tmp_path, monkeypatch):
    source, _ = _source_tree(tmp_path)
    target = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True,
    ).strip()
    (source / "later.c").write_text("int later(void) { return 0; }\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(source), "add", "later.c"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(source), "-c", "user.name=test", "-c", "user.email=test@example.invalid",
         "commit", "-qm", "later"],
        check=True,
    )
    configured = tmp_path / "configured-worktrees"
    monkeypatch.setenv("LUMEN_KERNEL_SOURCE_WORKTREE_ROOT", str(configured))

    pinned = resolve_kernel_source_for_commit(
        str(source), target, workspace_root=str(tmp_path / "session"),
    )

    assert str(configured / "kernel-source-worktrees") in pinned
    assert Path(pinned).is_dir()
    assert subprocess.check_output(
        ["git", "-C", pinned, "rev-parse", "HEAD"], text=True,
    ).strip() == target


def test_entry_point_extraction_accepts_only_explicit_function_evidence():
    entries = extract_semcode_entry_points(
        "function: foo_ioctl\nCall Trace: bar_release+0x1a/0x40",
        "ordinary prose should not create an entry point",
    )
    assert entries == ["foo_ioctl", "bar_release"]




def test_entry_point_extraction_accepts_declared_title_target():
    entries = extract_semcode_entry_points(
        "Bug Promote: title=KASAN use-after-free write in j1939_sock_pending_del subsystem=can"
    )
    assert entries == ["j1939_sock_pending_del"]


def test_entry_point_extraction_accepts_structured_stack_evidence():
    entries = extract_semcode_entry_points(
        '{"report_evidence":{"top_stack":["strlen","smack_log_callback","audit_log_format"]}}'
    )
    assert entries == ["strlen", "smack_log_callback", "audit_log_format"]


def test_entry_point_extraction_normalises_compiler_stack_suffixes():
    entries = extract_semcode_entry_points(
        "Call Trace: smack_log_callback.cold+0x1a/0x40\nfunction: audit_log_format.isra.0"
    )
    assert entries == ["audit_log_format", "smack_log_callback"]


def test_entry_point_extraction_accepts_quoted_json_fields():
    entries = extract_semcode_entry_points(
        '{"function":"foo_ioctl","frame":"bar_release+0x1a"}'
    )
    assert entries == ["foo_ioctl", "bar_release"]




def test_semcode_args_do_not_duplicate_source_binding():
    args = _without_database_args([
        "--legacy", "-d", "old.db", "--git-repo", "old-tree",
        "--database=new.db", "--git-repo=new-tree", "--lazy",
    ])
    assert args == ["--legacy", "--lazy"]


def test_semcode_path_analysis_online_llm_roundtrip():
    """Live gate: semcode evidence must remain intelligible to the configured LLM."""
    input_file = os.environ.get("LUMEN_P2_ONLINE_INPUT", "")
    if not input_file:
        pytest.fail("LUMEN_P2_ONLINE_INPUT must point to an input.txt with kernel_source")
    text = Path(input_file).read_text(encoding="utf-8")
    artifacts = parse_input_artifacts(text, validate_paths=True)
    config = load_config("config.json")
    semcode = config["agents"]["kernel_expert"]["semcode_mcp"]
    result = analyze_uaf_paths(
        kernel_source_path=artifacts.kernel_source_path,
        entry_points=["kref_put"],
        semcode_command=semcode["command"], semcode_args=semcode.get("args", []),
    )
    assert result.status == "ok", result.blocked_reason
    assert result.analysis is not None
    llm = get_llm_with_config(config["default"], agent_name="p2_online_contract")
    response = llm.invoke([
        SystemMessage(content="Read the supplied JSON evidence. Return only its case_id."),
        HumanMessage(content=render_semcode_analysis_context(result)),
    ])
    assert result.analysis.case_id in (response.content or "")
