from pathlib import Path

from agents.semcode_path_analysis import create_semcode_tools


def test_semcode_tool_adapter_exposes_bounded_source_queries():
    tools = create_semcode_tools(
        command="/tmp/semcode-mcp", args=[], kernel_source_path=str(Path("/tmp/linux")),
    )
    assert [tool.name for tool in tools] == [
        "semcode_find_function", "semcode_find_callers",
        "semcode_find_callees", "semcode_find_type",
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
