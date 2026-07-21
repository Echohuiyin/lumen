from pathlib import Path
import os
import pytest

from agents.semcode_path_analysis import create_semcode_tools


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


@pytest.mark.online
def test_real_semcode_adapter_query_when_index_is_deployed():
    source = Path(os.path.expanduser(os.environ.get("LUMEN_KERNEL_SOURCE", "~/linux-next")))
    binary = Path(os.environ.get(
        "LUMEN_SEMCODE_MCP",
        "Analysis-SKILL/tools/semcode/target/release/semcode-mcp",
    ))
    if not source.is_dir() or not (source / ".semcode.db").exists() or not binary.is_file():
        pytest.skip("real Semcode index/binary is not deployed")
    tools = create_semcode_tools(
        command=str(binary), args=[], kernel_source_path=str(source),
    )
    result = tools[0].invoke({"name": "kfree"})
    assert '"function": "kfree"' in result
    assert '"location"' in result
