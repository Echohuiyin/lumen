#!/usr/bin/env python3
"""Static checks for maintenance-agent responsibilities and tool capabilities."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


CAPABILITY_FILE = PROJECT_ROOT / "agent_capabilities.json"
CONFIG_FILE = PROJECT_ROOT / "config.json"

REQUIRED_AGENT_FIELDS = {
    "role",
    "prompt_file",
    "required_inputs",
    "optional_inputs",
    "outputs",
    "tools",
    "downstream_consumers",
}

RUNTIME_TOOL_FACTORIES = {
    "kernel_expert": ("agents.kernel_tools", "create_kernel_tools"),
    "test_expert": ("agents.qemu_tools", "create_qemu_tools"),
}

CRASH_TOOL_AGENTS = {"crash_analysis", "lock_analysis"}

PROMPT_FORBIDDEN_CLAIMS = {
    "kernel_expert": [
        "/kernel-testcase-generator",
        "kernel-testcase-generator skill",
    ],
    "test_expert": [
        "Bash 工具",
        "Read 工具",
        "Write 工具",
        "/kernel-build",
        "/qemu-test",
        "实际调用 Bash 工具",
    ],
    "kernel_log_analysis": [
        "已绑定 crash 工具",
        "run_crash_command:",
    ],
    "lock_analysis": [
        "/kernel-build",
        "/qemu-test",
    ],
}

PROMPT_REQUIRED_TERMS = {
    "crash_analysis": ["collect_baseline", "run_crash_command", "run_crash_commands"],
    "lock_analysis": ["collect_baseline", "run_crash_command", "run_crash_commands"],
    "kernel_expert": ["write_file", "compile_module", "search_files", "bash", "KERNEL_CONTRACT"],
    "test_expert": ["run_qemu_test_plan", "check_qemu_available", "create_initramfs", "boot_kernel", "analyze_boot_log"],
}


class CheckError(Exception):
    """Raised when the capability check finds invalid configuration."""


def load_capabilities(path: Path = CAPABILITY_FILE) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _tool_names(tools: list[Any]) -> list[str]:
    return sorted(str(getattr(tool, "name", "")) for tool in tools if getattr(tool, "name", ""))


def _runtime_tool_names(agent_name: str) -> list[str] | None:
    if agent_name in RUNTIME_TOOL_FACTORIES:
        module_name, factory_name = RUNTIME_TOOL_FACTORIES[agent_name]
        module = __import__(module_name, fromlist=[factory_name])
        factory = getattr(module, factory_name)
        return _tool_names(factory())

    if agent_name in CRASH_TOOL_AGENTS:
        from agents.crash_tools import create_crash_tools

        return _tool_names(create_crash_tools(session=object()))

    return None


def _configured_tool_experts(config_path: Path = CONFIG_FILE) -> set[str]:
    if not config_path.exists():
        return set()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    configured = {item.get("type", "") for item in config.get("tool_experts", []) if isinstance(item, dict)}
    legacy = config.get("agents", {}).get("tool_expert", {})
    if isinstance(legacy, dict):
        configured.update(legacy.keys())
    return {item for item in configured if item}


def validate_capabilities(capabilities: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    agents = capabilities.get("agents")
    if not isinstance(agents, dict) or not agents:
        raise CheckError("agent_capabilities.json must contain a non-empty 'agents' object")

    agent_names = set(agents)
    for agent_name, spec in agents.items():
        if not isinstance(spec, dict):
            errors.append(f"{agent_name}: spec must be an object")
            continue

        missing = sorted(REQUIRED_AGENT_FIELDS - set(spec))
        if missing:
            errors.append(f"{agent_name}: missing fields: {', '.join(missing)}")
            continue

        prompt_file = PROJECT_ROOT / spec["prompt_file"]
        if not prompt_file.exists():
            errors.append(f"{agent_name}: prompt file not found: {spec['prompt_file']}")
        else:
            prompt_text = prompt_file.read_text(encoding="utf-8")
            for fragment in PROMPT_FORBIDDEN_CLAIMS.get(agent_name, []):
                if fragment in prompt_text:
                    errors.append(f"{agent_name}: prompt has forbidden stale tool claim: {fragment}")
            for term in PROMPT_REQUIRED_TERMS.get(agent_name, []):
                if term not in prompt_text:
                    errors.append(f"{agent_name}: prompt does not reference required runtime term: {term}")

        for list_field in ("required_inputs", "optional_inputs", "outputs", "tools", "downstream_consumers"):
            if not isinstance(spec.get(list_field), list):
                errors.append(f"{agent_name}: {list_field} must be a list")

        for downstream in spec.get("downstream_consumers", []):
            if downstream == "tool_expert":
                continue
            if downstream not in agent_names:
                errors.append(f"{agent_name}: unknown downstream consumer: {downstream}")

        runtime_tools = _runtime_tool_names(agent_name)
        if runtime_tools is not None:
            declared_tools = sorted(spec.get("tools", []))
            if declared_tools != runtime_tools:
                errors.append(
                    f"{agent_name}: declared tools {declared_tools} do not match runtime tools {runtime_tools}"
                )

    configured_tool_experts = _configured_tool_experts()
    unknown_configured = sorted(configured_tool_experts - agent_names)
    if unknown_configured:
        errors.append(f"config.json references unknown tool experts: {', '.join(unknown_configured)}")

    return errors


def format_summary(capabilities: dict[str, Any]) -> str:
    agents = capabilities["agents"]
    lines = ["Agent capability summary:"]
    for agent_name in sorted(agents):
        spec = agents[agent_name]
        tools = ", ".join(spec.get("tools", [])) or "none"
        outputs = ", ".join(spec.get("outputs", []))
        lines.append(f"- {agent_name}: tools=[{tools}] outputs=[{outputs}]")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print machine-readable result")
    args = parser.parse_args()

    capabilities = load_capabilities()
    errors = validate_capabilities(capabilities)

    if args.json:
        print(json.dumps({"ok": not errors, "errors": errors}, ensure_ascii=False, indent=2))
    else:
        print(format_summary(capabilities))
        if errors:
            print("\nErrors:")
            for error in errors:
                print(f"- {error}")

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
