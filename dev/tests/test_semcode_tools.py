from pathlib import Path
import os
import pytest

from agents.semcode_path_analysis import create_semcode_tools, extract_semcode_entry_points


def test_semcode_tool_adapter_exposes_bounded_source_queries():
    tools = create_semcode_tools(
        command="/tmp/semcode-mcp", args=[], kernel_source_path=str(Path("/tmp/linux")),
    )
    assert [tool.name for tool in tools] == [
        "semcode_find_function", "semcode_find_callers",
        "semcode_find_callees", "semcode_find_type",
        "semcode_find_callchain",
    ]
    for tool in tools:
        schema = tool.args_schema.model_json_schema()
        assert set(schema["properties"]) == {"name"}


def test_semcode_tool_adapter_does_not_expose_shell_arguments():
    tools = create_semcode_tools(
        command="/tmp/semcode-mcp", args=["--unexpected"], kernel_source_path="/tmp/linux",
    )
    for tool in tools:
        assert "command" not in tool.args_schema.model_fields
        assert "args" not in tool.args_schema.model_fields


def test_entry_point_extraction_reads_nested_expert_result_text():
    points = extract_semcode_entry_points(expert_results=[{
        "structured_output": {"summary": "KASAN in uaf_ioctl+0x139/0x280"},
    }])
    assert points == ["uaf_ioctl"]


@pytest.mark.online
def test_real_semcode_adapter_query_when_index_is_deployed():
    source = Path(os.path.expanduser(os.environ.get("LUMEN_KERNEL_SOURCE", "~/linux-next")))
    binary = Path(os.environ.get(
        "LUMEN_SEMCODE_MCP",
        "Analysis-SKILL/tools/semcode/target/release/semcode-mcp",
    ))
    if not source.is_dir() or not (source / ".semcode.db").exists() or not binary.is_file():
        pytest.skip("real Semcode index/binary is not deployed")
    expected = os.environ.get("LUMEN_SEMCODE_GIT_SHA", "")
    if not expected:
        pytest.skip("LUMEN_SEMCODE_GIT_SHA must identify the indexed snapshot")
    tools = create_semcode_tools(
        command=str(binary), args=[], kernel_source_path=str(source),
        expected_kernel_commit=expected,
    )
    symbol = os.environ.get("LUMEN_SEMCODE_PROBE_SYMBOL", "vfs_read")
    result = tools[0].invoke({"name": symbol})
    assert f'"function": "{symbol}"' in result
    assert '"location"' in result
