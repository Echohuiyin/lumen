"""工具专家独立测试脚本。

测试 crash_analysis、lock_analysis、kernel_log_analysis 三个专家的执行过程和输出结果。

使用方法：
    python tests/test_tool_experts.py --expert crash_analysis
    python tests/test_tool_experts.py --expert lock_analysis
    python tests/test_tool_experts.py --expert kernel_log_analysis
    python tests/test_tool_experts.py --all

环境变量（可选）：
    TEST_VMCORE: vmcore 文件路径
    TEST_VMLINUX: vmlinux 文件路径
"""

import argparse
import os
import sys
import tempfile
from pathlib import Path

# 确保项目根目录在 sys.path
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.tool_expert import tool_expert_node, _extract_vmcore_paths, _resolve_file_path
from agents.llm_display import get_expert_output_file, ensure_output_dir
from config import load_config, get_llm_with_config, load_prompt_from_file
from graph.rn_state import MaintenanceWorkflowState, ToolExpertResult

# 默认测试文件路径
DEFAULT_VMCORE = Path.home() / "lumen" / "Analysis-SKILL" / "test_outputs" / "deadlock_x86_64" / "vmcore.elf"
DEFAULT_VMLINUX = Path.home() / "lumen" / "Analysis-SKILL" / "test_outputs" / "deadlock_x86_64" / "vmlinux"


def get_test_paths() -> tuple[Path, Path]:
    """获取测试用的 vmcore 和 vmlinux 路径。"""
    vmcore = Path(os.environ.get("TEST_VMCORE", str(DEFAULT_VMCORE)))
    vmlinux = Path(os.environ.get("TEST_VMLINUX", str(DEFAULT_VMLINUX)))
    return vmcore, vmlinux


def build_test_user_input(vmcore_path: str, vmlinux_path: str, problem_type: str = "hung_task") -> str:
    """构建测试用的用户输入。"""
    templates = {
        "hung_task": """系统出现 hung task，进程 mysql 处于 D 状态阻塞超过 120 秒。
问题类型：hung task
环境：openEuler 22.03, kernel 6.6.0, x86_64, 64核
vmcore 文件：{vmcore}
vmlinux 文件：{vmlinux}
触发场景：高并发数据库写入时偶发。""",
        "deadlock": """系统出现死锁，两个进程互相阻塞。
问题类型：deadlock
环境：openEuler 22.03, kernel 6.6.0, x86_64
vmcore 文件：{vmcore}
vmlinux 文件：{vmlinux}
触发场景：并发执行 insmod 加载死锁模块时触发。""",
        "nullptr": """内核发生 NULL pointer dereference 崩溃。
问题类型：kernel panic
环境：openEuler 22.03, kernel 6.6.0, x86_64
vmcore 文件：{vmcore}
vmlinux 文件：{vmlinux}
触发场景：加载测试模块时触发。""",
    }
    return templates.get(problem_type, templates["hung_task"]).format(
        vmcore=vmcore_path, vmlinux=vmlinux_path
    )


def test_path_extraction():
    """测试路径提取功能。"""
    print("\n" + "=" * 60)
    print("测试路径提取功能")
    print("=" * 60)

    test_inputs = [
        # 中文冒号 + ~ 路径
        "vmcore 文件：~/lumen/test/vmcore",
        # 英文冒号 + 绝对路径
        "vmcore file: /home/user/vmcore",
        # 中文冒号 + 无路径
        "vmcore 文件：test_outputs/vmcore",
    ]

    for input_text in test_inputs:
        vmcore_path, _ = _extract_vmcore_paths(input_text)
        resolved = _resolve_file_path(vmcore_path)
        print(f"\n输入: {input_text}")
        print(f"提取: {vmcore_path}")
        print(f"解析: {resolved}")

    print("\n✓ 路径提取测试完成")


def test_crash_session():
    """测试 crash session 创建。"""
    print("\n" + "=" * 60)
    print("测试 Crash Session 创建")
    print("=" * 60)

    vmcore, vmlinux = get_test_paths()

    print(f"\nVmcore: {vmcore}")
    print(f"Vmlinux: {vmlinux}")
    print(f"Vmcore 存在: {vmcore.exists()}")
    print(f"Vmlinux 存在: {vmlinux.exists()}")

    if not vmcore.exists() or not vmlinux.exists():
        print("\n✗ 测试文件不存在，跳过 crash session 测试")
        return False

    try:
        from agents.crash_tools import get_or_create_crash_session, release_crash_session
        session = get_or_create_crash_session(vmcore, vmlinux)
        print("\n✓ Crash session 创建成功")

        # 执行测试命令
        result = session.run_command("sys")
        print(f"\nsys 命令输出预览:")
        print(result.output[:500] + "...")

        release_crash_session(vmcore, vmlinux)
        print("\n✓ Session 停止成功")
        return True

    except Exception as e:
        print(f"\n✗ Crash session 失败: {e}")
        return False


def test_expert_direct(expert_type: str, verbose: bool = True):
    """直接测试单个专家的执行过程。

    Args:
        expert_type: 专家类型 (crash_analysis/lock_analysis/kernel_log_analysis)
        verbose: 是否打印详细输出

    Returns:
        (success, output_length, output_preview)
    """
    print("\n" + "=" * 60)
    print(f"测试专家: {expert_type}")
    print("=" * 60)

    vmcore, vmlinux = get_test_paths()

    # 检查文件
    if expert_type in ("crash_analysis", "lock_analysis"):
        if not vmcore.exists() or not vmlinux.exists():
            print(f"\n✗ 测试文件不存在:")
            print(f"  vmcore: {vmcore} ({'存在' if vmcore.exists() else '不存在'})")
            print(f"  vmlinux: {vmlinux} ({'存在' if vmlinux.exists() else '不存在'})")
            return False, 0, ""

    # 构建测试状态
    user_input = build_test_user_input(str(vmcore), str(vmlinux), "deadlock")
    config = load_config("maintenance_config.json")

    # 设置输出目录
    ensure_output_dir()
    output_file = get_expert_output_file(expert_type)

    state = {
        "expert_type": expert_type,
        "user_input": user_input,
        "config": config,
        "config_path": "maintenance_config.json",
    }

    print(f"\n执行 {expert_type} 专家...")
    print(f"用户输入长度: {len(user_input)} 字符")
    print(f"输出文件: {output_file}")

    try:
        result = tool_expert_node(state)

        # 检查结果
        expert_results = result.get("expert_results", [])
        if not expert_results:
            print("\n✗ 无专家结果返回")
            return False, 0, ""

        # LangGraph 可能将 Pydantic 模型转换为 dict
        first_result = expert_results[0]
        if isinstance(first_result, dict):
            output = first_result.get("analysis_output", "")
        else:
            output = first_result.analysis_output
        output_length = len(output) if output else 0

        print(f"\n执行完成:")
        print(f"  输出长度: {output_length} 字符")
        print(f"  输出文件大小: {Path(output_file).stat().st_size if Path(output_file).exists() else 0} bytes")

        if verbose and output:
            preview = output[:500] + "..." if len(output) > 500 else output
            print(f"\n输出预览:")
            print(preview)

        # 检查输出文件
        if Path(output_file).exists():
            file_content = Path(output_file).read_text()
            print(f"\n输出文件内容长度: {len(file_content)} 字符")
            if verbose:
                print(f"输出文件预览:")
                print(file_content[:500] + "...")

        success = output_length > 100  # 有效输出应该有足够内容
        status = "✓" if success else "✗"
        print(f"\n{status} 测试结果: {'成功' if success else '失败'}")

        return success, output_length, output

    except Exception as e:
        print(f"\n✗ 执行失败: {e}")
        import traceback
        traceback.print_exc()
        return False, 0, str(e)


def test_all_experts():
    """测试所有专家。"""
    print("\n" + "=" * 60)
    print("测试所有工具专家")
    print("=" * 60)

    experts = ["crash_analysis", "lock_analysis", "kernel_log_analysis"]
    results = {}

    for expert_type in experts:
        success, length, output = test_expert_direct(expert_type, verbose=False)
        results[expert_type] = {
            "success": success,
            "output_length": length,
        }

    # 打印汇总
    print("\n" + "=" * 60)
    print("测试汇总")
    print("=" * 60)

    for expert_type, result in results.items():
        status = "✓" if result["success"] else "✗"
        print(f"{status} {expert_type}: 输出 {result['output_length']} 字符")

    all_passed = all(r["success"] for r in results.values())
    print(f"\n总体结果: {'全部通过 ✓' if all_passed else '有失败 ✗'}")

    return all_passed


def main():
    parser = argparse.ArgumentParser(description="工具专家独立测试")
    parser.add_argument(
        "--expert",
        choices=["crash_analysis", "lock_analysis", "kernel_log_analysis"],
        help="指定测试的专家类型",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="测试所有专家",
    )
    parser.add_argument(
        "--path-extraction",
        action="store_true",
        help="仅测试路径提取功能",
    )
    parser.add_argument(
        "--crash-session",
        action="store_true",
        help="仅测试 crash session 创建",
    )
    parser.add_argument(
        "--vmcore",
        help="指定 vmcore 文件路径",
    )
    parser.add_argument(
        "--vmlinux",
        help="指定 vmlinux 文件路径",
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="减少输出",
    )

    args = parser.parse_args()

    # 设置自定义路径
    if args.vmcore:
        os.environ["TEST_VMCORE"] = args.vmcore
    if args.vmlinux:
        os.environ["TEST_VMLINUX"] = args.vmlinux

    # 执行测试
    if args.path_extraction:
        test_path_extraction()
    elif args.crash_session:
        test_crash_session()
    elif args.all:
        test_all_experts()
    elif args.expert:
        test_expert_direct(args.expert, verbose=not args.quiet)
    else:
        # 默认运行所有测试
        test_path_extraction()
        test_crash_session()
        test_all_experts()


if __name__ == "__main__":
    main()