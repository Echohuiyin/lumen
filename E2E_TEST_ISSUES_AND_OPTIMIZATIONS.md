# E2E 测试问题与优化点记录

## 测试执行时间: 2026-06-22

## 最终验证结果 (2026-06-22 第三次 E2E 测试)

**Workflow 完整流程**: validator → pm → tool_experts(3) → kernel_expert → test_expert(3次尝试) → knowledge_base

| 检查项 | 状态 | 说明 |
|--------|------|------|
| 所有阶段执行 | ✓ 通过 | 6个阶段全部执行到 |
| kernel_expert 工具调用 | ✓ 通过 | 0 次 bash crash，使用 write_file/compile_module |
| test_expert QEMU 启动 | ✓ 通过 | bzImage 正确识别，QEMU 启动成功 |
| knowledge_base 归档 | ✓ 通过 | Markdown + Chroma 导入成功 |
| 信号匹配 | ✗ FAILED | FAILED_SIGNAL_NOT_FOUND（需要 test_script 加载 reproducer 模块） |
| E2E 测试数 | 3次 | 测试验证 3 次 |

## Workflow 流程验证

| 阶段 | 专家 | 状态 | 问题 |
|------|------|------|------|
| Validator | 规则验证 | ✓ 通过 | 无 |
| PM | 路由选择 | ✓ 通过 | 无 |
| Tool Experts | crash工具 | ✓ 通过 | 15次迭代成功 |
| kernel_expert | 用例构造 | ✓ 已修复 | 正确使用 write_file/compile_module/create_directory |
| test_expert | 验证测试 | ✓ 通过 | QEMU 成功启动，FAILED_SIGNAL_NOT_FOUND（需提供 test_script） |
| knowledge_base | 归档 | ✓ 通过 | 成功生成知识库条目 |

---

## 发现的问题

### 问题 1: kernel_expert 工具调用方式不一致

**现象**:
```
[内核专家] 执行工具: bash(command=crash ~/code/OLK-6.6/vmlinux ~/lumen/test_outputs/deadlock_fault/vmcore.elf -i <(echo "log") 2>&1 | tail -200, timeout=120)
```

**预期**: kernel_expert 应使用 `kernel_tools` 的 StructuredTool（create_directory, write_file, compile_module 等）创建复现用例

**实际**: kernel_expert 使用 bash 工具直接调用 crash 命令进行分析

**根因**: kernel_expert 的 prompt/context_info 误导 LLM 使用 bash 调用 crash

**修复措施**:
- context_info 明确说明 "NEVER use bash to call crash, gdb, or any analysis tools"
- context_info 改为引导使用 kernel_tools（write_file, compile_module, create_directory）
- 新增 `_extract_evidence_summary()` 从 tool_experts evidence 提取关键信息（arch, panic, 锁分析）
- 在 user_content 中添加 input_artifacts_contract 中的文件路径信息
- 为 LLM 响应为空时添加 fallback 自动生成 kernel_contract
- max_iterations 从 15 增加到 20

**验证**: E2E 重跑后 kernel_expert 0 次 bash crash 调用，改用 write_file(5次)、compile_module、create_directory 等 kernel_tools

---

### 问题 2: kernel_contract 未生成，test_expert 阻塞

**现象**:
```
QEMU TEST STATUS: blocked
CODE: BLOCKED_NO_BOOT_KERNEL
TEST PASSED: False
SUMMARY: No bootable kernel image was provided.
```

**根因**: kernel_expert 未从 input_artifacts_contract 中提取 boot_kernel_path

**修复措施**:
- 在 user_content 中添加 input_artifacts_contract 中的文件路径（vmcore_path, vmlinux_path, boot_kernel_path）
- context_info 强调 KERNEL_CONTRACT 必须包含 ALL required fields（target_arch, boot_kernel_path, test_script_path, expected_signal）
- 新增空响应 fallback：自动从 input_artifacts 和 expert_results 生成 kernel_contract
- 优化 `_validate_kernel_contract_artifacts()` 的提示信息

**当前状态**: kernel_contract 已生成，但由于 vmlinux 是 ELF 格式而非可启动的 bzImage，status 为 "blocked"。workflow 正确路由到 knowledge_base。需要提供 bootable kernel 才能通过 test_expert 完整验证。

---

### 问题 3: bash 工具直接调用 crash 效率低

**现象**: kernel_expert 多次执行 bash 命令调用 crash：
```
bash(command=crash ... -i <(echo "ps") 2>&1 | tail -100)
bash(command=crash ... <<'EOF' ps EOF)
```

**问题**:
1. 每次调用都启动新的 crash 进程（开销大）
2. 绕过了 crash_tools 的 session 管理（共享 session）
3. 输出解析复杂（需要大量 grep/sed 过滤）

**对比**: tool_experts 使用 crash_tools：
```python
session = get_or_create_crash_session(vmcore, vmlinux)
tools = create_crash_tools(session)
# 15次迭代，session 共享，输出结构化
```

**优化建议**:
- kernel_expert 不应该直接调用 crash（职责混乱）
- 如果需要 crash 信息，从 tool_experts 的 evidence 中获取

---

### 问题 4: workflow 传递链断裂

**现象**:
- kernel_expert → test_expert 传递失败
- test_expert 无法获取 boot_kernel_path
- workflow 在 test_expert 阻塞后未到达 knowledge_base

**状态传递检查**:
```python
# route_after_kernel 检查
contract = state.get("kernel_contract") or {}
if contract.get("status") == "ok" and all(contract.get(field) for field in required):
    return "test_expert"
return "knowledge_base"  # contract 不完整
```

**优化建议**:
- 确保 kernel_expert 生成完整 contract
- 或者添加降级路径：如果 kernel_contract 不完整，尝试从 state 中其他字段提取信息

---

## 可优化点

### 优化 1: kernel_expert 职责重新定义

**当前职责**:
- 分析 expert_results
- 创建复现用例
- 生成 kernel_contract

**建议调整**:
- 从 expert_results.evidence 提取关键信息（arch, panic 原因）
- 使用 kernel_tools 创建 reproducer 目录和文件
- 不直接执行 crash 分析（依赖 tool_experts 结果）

**Prompt 调整示例**:
```markdown
你是内核专家，负责根据 crash 分析结果创建复现用例。

已有信息：
- tool_experts 已完成 crash 分析，evidence 中有 sys/ps/bt/log 输出
- 从 evidence 中提取：kernel arch, panic 原因, 模块名

你的任务：
1. 从 evidence 中提取关键信息
2. 使用 kernel_tools 创建 reproducer 目录
3. 创建测试模块源码（如果需要）
4. 创建 Makefile
5. 输出 kernel_contract（必须包含 target_arch, boot_kernel_path）

不要直接调用 crash 工具！使用已有的 expert_results。
```

---

### 优化 2: kernel_contract 生成逻辑改进

**当前**: kernel_expert 需要手动生成 contract

**建议**: 从 state 中自动提取并填充：
```python
def _generate_kernel_contract(state):
    artifacts = state.get("input_artifacts_contract", {})
    expert_results = state.get("expert_results", [])
    
    # 从 evidence 提取 arch
    for result in expert_results:
        for ev in result.get("evidence", []):
            if ev.get("kind") == "crash_command" and "sys" in ev.get("command", ""):
                output = ev.get("output_excerpt", "")
                # 解析 MACHINE: x86_64
                ...
    
    return {
        "target_arch": arch,
        "boot_kernel_path": artifacts.get("boot_kernel_path") or artifacts.get("vmlinux_path"),
        "test_script_path": "...",
        "expected_signal": "...",
    }
```

---

### 优化 3: test_expert 降级逻辑

**当前**: 没有 boot_kernel 直接阻塞

**建议**: 添加降级路径：
```python
def test_expert_node(state):
    contract = state.get("kernel_contract", {})
    
    # 降级：尝试从 state 其他字段获取信息
    if not contract.get("boot_kernel_path"):
        artifacts = state.get("input_artifacts_contract", {})
        boot_kernel = artifacts.get("boot_kernel_path") or artifacts.get("vmlinux_path")
        if boot_kernel:
            # 尝试使用 vmlinux（需要转换）
            ...
    
    # 最终降级：纯分析模式
    if not boot_kernel:
        return {
            "test_result": "BLOCKED_NO_BOOT_KERNEL",
            "test_passed": False,
            "final_response": "无法验证，需要 bootable kernel",
        }
```

---

### 优化 4: 减少重复 crash 调用

**当前**: 多个专家都调用 crash：
- tool_experts: lock_analysis, crash_analysis, kernel_log_analysis
- kernel_expert: 通过 bash 再次调用 crash

**建议**:
- tool_experts 的 evidence 应包含所有必要信息
- kernel_expert 不再调用 crash，直接使用 evidence
- 统一 crash session 管理（get_or_create_crash_session）

---

## 测试通过的功能 ✓

1. **Validator 规则验证** - 正确识别 kernel panic 信号
2. **PM 路由选择** - deadlock → lock_analysis, panic → crash_analysis
3. **Tool Experts 工具调用** - 15次迭代，session 管理
4. **Crash 命令执行** - sys, ps, bt, log 成功
5. **路径提取** - vmcore 和 vmlinux 正确解析
6. **证据收集** - structured evidence 正常工作

---

## 待修复的问题

| 优先级 | 问题 | 影响 | 状态 |
|--------|------|------|------|
| P1 | kernel_expert 工具调用混乱 | 重复分析，效率低 | ✓ 已修复 |
| P1 | kernel_contract 未生成 | workflow 路由异常 | ✓ 已修复（自动 fallback） |
| P2 | workflow 传递断裂 | knowledge_base 未执行 | ✓ 已修复 |
| P3 | bash 直接调用 crash | 绕过 session 管理 | ✓ 已修复 |
| P4 | test_expert 因无 bootable kernel 跳过 | 无法做 QEMU 验证 | ⏳ 需提供 bzImage |

### 未解决问题: test_expert 跳过

**原因**: vmcore 分析场景中只有 vmlinux（ELF debug symbols），没有 bootable bzImage。QEMU 需要 bootable kernel 镜像才能启动。

**解决方案选项**:
1. 在 input_artifacts 中提供 bootable kernel 路径（如 `/boot/vmlinuz-*` 或编译生成的 bzImage）
2. 如果系统有 `/boot/vmlinuz-$(uname -r)`，可以用作 boot kernel
3. 或者在 workflow 配置中允许 test_expert 降级为纯分析模式（跳过 QEMU 测试）

---

## 下一步行动

1. 修复 kernel_expert 的工具调用逻辑（使用 kernel_tools 而非 bash）
2. 确保 kernel_contract 必填字段生成
3. 添加 kernel_contract 自动填充逻辑（从 evidence/state 提取）
4. 重新运行 E2E 测试验证修复效果