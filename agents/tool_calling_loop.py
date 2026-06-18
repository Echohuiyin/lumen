"""Generic tool-calling loop for LangGraph agents.

Implements the standard LLM tool-calling pattern:
    LLM.bind_tools() -> AIMessage(tool_calls) -> Execute tools -> ToolMessage
    -> messages.append() -> Loop until no tool_calls
"""

import uuid
from typing import Any, Callable, List, Optional
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool


def find_tool(name: str, tools: List[StructuredTool]) -> Optional[StructuredTool]:
    """Find tool by name in tools list.

    Args:
        name: Tool name to find
        tools: List of StructuredTool instances

    Returns:
        Matching tool or None
    """
    for tool in tools:
        if tool.name == name:
            return tool
    return None


def execute_tool_calling_loop(
    llm: Any,
    messages: List[BaseMessage],
    tools: List[StructuredTool],
    max_iterations: int = 10,
    on_tool_call: Optional[Callable[[str, dict], None]] = None,
    verbose: bool = False,
) -> AIMessage:
    """Execute tool-calling loop until LLM returns final response.

    Args:
        llm: LLM instance (ChatOpenAI with bind_tools capability)
        messages: Initial message list (typically System + Human)
        tools: List of StructuredTool instances
        max_iterations: Maximum tool-calling iterations (prevent infinite loops)
        on_tool_call: Optional callback for logging (name, args)
        verbose: Print debug info to stdout

    Returns:
        Final AIMessage without tool_calls (or last response if max_iterations reached)

    Pattern:
        LLM -> AIMessage(tool_calls) -> Execute tools -> ToolMessage
        -> messages.append() -> LLM -> AIMessage(tool_calls) -> ...
        -> AIMessage(no tool_calls) -> Return
    """
    # Bind tools to LLM
    llm_with_tools = llm.bind_tools(tools)

    iteration = 0
    # Mutate caller's messages list in-place so they can access full
    # tool-call history for summary generation after the loop returns.
    current_messages = messages

    while iteration < max_iterations:
        # Invoke LLM with current message history
        if verbose:
            print(f"\n[Tool Loop] Iteration {iteration + 1}")
            print(f"  Messages: {len(current_messages)}")

        response = llm_with_tools.invoke(current_messages)

        # Check if LLM wants to call tools
        tool_calls = getattr(response, "tool_calls", None) or []

        if not tool_calls:
            # No tool calls - final response
            if verbose:
                print(f"[Tool Loop] Final response (no tool_calls)")
            return response

        if verbose:
            print(f"[Tool Loop] Tool calls: {len(tool_calls)}")
            for tc in tool_calls:
                tc_id = tc.get("id", "") if isinstance(tc, dict) else "N/A"
                tc_name = tc.get("name", "?") if isinstance(tc, dict) else str(tc)[:80]
                tc_keys = list(tc.keys()) if isinstance(tc, dict) else type(tc).__name__
                print(f"  - {tc_name}(id={tc_id}, keys={tc_keys})")

        # IMPORTANT: Append AIMessage with tool_calls to message list FIRST
        # This is required for ToolMessage to have a preceding message with tool_calls
        current_messages.append(response)

        # Execute each tool call
        for tool_call in tool_calls:
            tool_name = tool_call.get("name", "unknown")
            tool_args = tool_call.get("args", {})
            # Some model providers don't include "id" in tool_calls.
            # Generate a UUID and inject it back into the tool_call dict
            # so the AIMessage and ToolMessage share the same id.
            tool_call_id = tool_call.get("id")
            if not tool_call_id:
                tool_call_id = str(uuid.uuid4())
                tool_call["id"] = tool_call_id

            # Callback for logging
            if on_tool_call:
                on_tool_call(tool_name, tool_args)

            # Find matching tool
            tool = find_tool(tool_name, tools)

            if tool is None:
                # Tool not found - return error message
                tool_output = f"Error: Tool '{tool_name}' not found. Available tools: {[t.name for t in tools]}"
                if verbose:
                    print(f"  [Error] Tool not found: {tool_name}")
            else:
                # Execute tool
                try:
                    tool_output = tool.invoke(tool_args)
                    if verbose:
                        output_preview = str(tool_output)[:100]
                        print(f"  [Output] {output_preview}...")
                except Exception as e:
                    tool_output = f"Error executing {tool_name}: {str(e)}"
                    if verbose:
                        print(f"  [Error] {e}")

            # Create ToolMessage with result
            tool_message = ToolMessage(
                content=str(tool_output),
                tool_call_id=tool_call_id,
                name=tool_name,
            )
            current_messages.append(tool_message)

        iteration += 1

    # Max iterations reached - return last response
    if verbose:
        print(f"[Tool Loop] Max iterations ({max_iterations}) reached")
    return response


def format_tool_call_summary(tool_calls: List[dict]) -> str:
    """Format tool calls for logging output.

    Args:
        tool_calls: List of tool call dicts with 'name' and 'args'

    Returns:
        Formatted string for display
    """
    if not tool_calls:
        return ""

    lines = ["[工具调用计划]"]
    for tc in tool_calls:
        name = tc.get("name", "unknown")
        args = tc.get("args", {})
        args_str = ", ".join(f"{k}={v}" for k, v in args.items()) if args else ""
        lines.append(f"  - {name}({args_str})")
    return "\n".join(lines) + "\n"


def create_tool_call_messages(
    system_prompt: str,
    user_input: str,
    context_info: str = "",
) -> List[BaseMessage]:
    """Create initial message list for tool-calling loop.

    Args:
        system_prompt: Agent's system prompt (from prompt file)
        user_input: User's request/problem description
        context_info: Additional context (vmcore paths, etc.)

    Returns:
        [SystemMessage, HumanMessage] list
    """
    human_content = user_input
    if context_info:
        human_content = f"{user_input}\n\n{context_info}"

    return [
        SystemMessage(content=system_prompt),
        HumanMessage(content=human_content),
    ]