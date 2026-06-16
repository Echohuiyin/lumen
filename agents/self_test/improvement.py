"""改进 Agent：根据评估结果生成具体的改进建议，并自动执行。

分析评估中发现的问题，生成可执行的改进方案，
包括 prompt 改进、流程优化、专家能力增强等。
关键增强：自动修改 prompt 文件并 git commit，支持回退。
"""

import subprocess
from datetime import datetime
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from agents.llm_display import call_llm_with_display
from config import get_llm_with_config, load_prompt_from_file, PROJECT_ROOT


def improvement_agent_node(state: dict) -> dict:
    """改进节点：根据评估结果生成改进方案并自动执行。

    输入：
    - evaluation_details: 评估详情
    - gaps_found: 发现的差距
    - evaluation_score: 评估分数
    - iteration_count: 当前迭代次数
    - auto_apply: 是否自动应用改进（默认 True）

    输出：
    - improvement_report: 改进报告文件路径
    - actionable_changes: 可执行的改进列表
    - applied_changes: 已应用的改进列表
    - should_continue: 是否继续迭代
    """
    config = state.get("config", {})
    agent_config = config.get("agents", {}).get("improvement", {})
    default_config = config.get("default", {})
    auto_apply = state.get("auto_apply", True)  # 默认自动应用

    llm = get_llm_with_config(agent_config, default_config=default_config, agent_name="improvement")

    evaluation_details = state.get("evaluation_details", "")
    gaps_found = state.get("gaps_found", [])
    evaluation_score = state.get("evaluation_score", 0)
    iteration_count = state.get("iteration_count", 1)
    max_iterations = state.get("max_iterations", 10)
    fault_type = state.get("fault_type", "unknown")

    user_content = f"""## 评估结果

评分: {evaluation_score}/100
迭代次数: {iteration_count}/{max_iterations}
故障类型: {fault_type}

### 发现的差距
{chr(10).join(f"- {gap}" for gap in gaps_found) if gaps_found else '无明显差距'}

### 详细评估报告
{evaluation_details}

## 改进任务

基于评估结果，请生成以下改进方案。改进必须是**精确的、可自动执行的**：

### 1. Prompt 改进（优先级最高）

必须精确指定：
- Agent: 要修改哪个 agent 的 prompt
- File: prompt 文件的相对路径（如 `prompts/maintenance/kernel_expert.md`）
- Section: 要修改的具体段落（如"分析路径"或"根因定位"）
- Action: 增加内容/修改内容/删除内容
- Content: 具体内容（如果是增加或修改）

示例格式：
```
PROMPT_CHANGE:
- Agent: kernel_expert
  File: prompts/maintenance/kernel_expert.md
  Section: "根因定位"段落
  Action: 增加
  Content: |
    **必须从 Call Trace 提取具体函数名和偏移**
    例如：`crash_init+0x5` 表示 crash_init 函数偏移 5 字节处
  Reason: 评估发现仅说"空指针错误"未定位具体位置
```

### 2. 流程优化

精确描述要添加/修改的节点和边。

### 3. 专家增强

精确描述要修改的专家 prompt。

### 4. 知识库补充

提供完整的知识库文档内容。

判断是否需要继续迭代：
- 如果评分 >= 90，建议停止迭代
- 如果迭代次数已达上限，建议停止
- 如果连续 2 次迭代评分无提升，建议停止

输出格式：

IMPROVEMENT_TYPE: prompt/flow/expert/knowledge/none

PROMPT_CHANGES:
```prompt_change
- Agent: <agent_name>
  File: <file_path>
  Section: <section_name>
  Action: 增加/修改/删除
  Content: |
    <具体内容>
  Reason: <改进原因>
```
... (每个改进用 prompt_change 代码块包裹)

FLOW_CHANGES:
- Change: <具体流程调整>
  Reason: <调整原因>
  Expected: <预期效果>
...

EXPERT_ENHANCEMENTS:
```expert_enhancement
- Expert: <expert_type>
  File: <file_path>
  Section: <section_name>
  Action: 增加/修改/删除
  Content: |
    <具体内容>
  Reason: <增强原因>
```
...

KNOWLEDGE_ADDITIONS:
```knowledge_doc
Title: <案例标题>
FaultType: <故障类型>
Content: |
  <完整的知识库文档内容>
```
...

CONTINUE_ITERATION: yes/no
REASON: <原因>
"""

    system_prompt = load_prompt_from_file(
        agent_config.get("prompt_file", "prompts/self_test/improvement.md")
    )

    response = call_llm_with_display(
        "改进专家", "生成改进方案", llm,
        [SystemMessage(content=system_prompt), HumanMessage(content=user_content)],
    )

    text = response.content.strip()

    # 解析改进结果
    actionable_changes = _parse_actionable_changes(text)

    # 自动应用改进
    applied_changes = []
    if auto_apply and actionable_changes:
        applied_changes = _apply_changes(actionable_changes, state)

    # 保存改进报告
    improvement_report = _save_improvement_report(state, text, applied_changes)

    # 判断是否继续迭代
    should_continue = _should_continue_iteration(text, evaluation_score, iteration_count, max_iterations)

    return {
        "improvement_report": improvement_report,
        "actionable_changes": actionable_changes,
        "applied_changes": applied_changes,
        "should_continue": should_continue,
    }


def _parse_actionable_changes(text: str) -> list[dict]:
    """从改进文本中解析可执行的改进列表。

    支持代码块格式的精确解析。
    """
    import re
    changes = []

    # 解析 Prompt 改进（新格式：代码块）
    prompt_blocks = re.findall(r"```prompt_change\n(.*?)```", text, re.DOTALL)
    for block in prompt_blocks:
        change = _parse_prompt_block(block)
        if change:
            changes.append(change)

    # 解析专家增强（新格式：代码块）
    expert_blocks = re.findall(r"```expert_enhancement\n(.*?)```", text, re.DOTALL)
    for block in expert_blocks:
        change = _parse_expert_block(block)
        if change:
            changes.append(change)

    # 解析知识库文档
    knowledge_blocks = re.findall(r"```knowledge_doc\n(.*?)```", text, re.DOTALL)
    for block in knowledge_blocks:
        change = _parse_knowledge_block(block)
        if change:
            changes.append(change)

    # 兼容旧格式：列表格式
    if not changes:
        # 解析 Prompt 改进（旧格式）
        prompt_match = re.search(r"PROMPT_CHANGES:\s*\n((?:- .+\n?)+)", text)
        if prompt_match:
            prompt_text = prompt_match.group(1)
            for line in prompt_text.strip().split("\n"):
                if line.strip().startswith("- Agent:"):
                    agent_match = re.search(r"Agent:\s*(\w+)", line)
                    file_match = re.search(r"File:\s*(\S+)", line)
                    if agent_match:
                        changes.append({
                            "type": "prompt",
                            "agent": agent_match.group(1),
                            "file": file_match.group(1) if file_match else "",
                            "details": line,
                        })

    return changes


def _parse_prompt_block(block: str) -> dict | None:
    """解析 prompt_change 代码块。"""
    import re

    agent_match = re.search(r"Agent:\s*(\w+)", block)
    file_match = re.search(r"File:\s*(\S+)", block)
    section_match = re.search(r"Section:\s*(.+)", block)
    action_match = re.search(r"Action:\s*(增加|修改|删除)", block)
    content_match = re.search(r"Content:\s*\n(.*?)(?:\n  Reason:|\Z)", block, re.DOTALL)
    reason_match = re.search(r"Reason:\s*(.+)", block)

    if agent_match and file_match:
        return {
            "type": "prompt",
            "agent": agent_match.group(1),
            "file": file_match.group(1),
            "section": section_match.group(1).strip() if section_match else "",
            "action": action_match.group(1) if action_match else "增加",
            "content": content_match.group(1).strip() if content_match else "",
            "reason": reason_match.group(1).strip() if reason_match else "",
        }
    return None


def _parse_expert_block(block: str) -> dict | None:
    """解析 expert_enhancement 代码块。"""
    import re

    expert_match = re.search(r"Expert:\s*(\w+)", block)
    file_match = re.search(r"File:\s*(\S+)", block)
    section_match = re.search(r"Section:\s*(.+)", block)
    action_match = re.search(r"Action:\s*(增加|修改|删除)", block)
    content_match = re.search(r"Content:\s*\n(.*?)(?:\n  Reason:|\Z)", block, re.DOTALL)
    reason_match = re.search(r"Reason:\s*(.+)", block)

    if expert_match and file_match:
        return {
            "type": "expert",
            "expert": expert_match.group(1),
            "file": file_match.group(1),
            "section": section_match.group(1).strip() if section_match else "",
            "action": action_match.group(1) if action_match else "增加",
            "content": content_match.group(1).strip() if content_match else "",
            "reason": reason_match.group(1).strip() if reason_match else "",
        }
    return None


def _parse_knowledge_block(block: str) -> dict | None:
    """解析 knowledge_doc 代码块。"""
    import re

    title_match = re.search(r"Title:\s*(.+)", block)
    fault_type_match = re.search(r"FaultType:\s*(\w+)", block)
    content_match = re.search(r"Content:\s*\n(.*?)(?:\n```|\Z)", block, re.DOTALL)

    if title_match and content_match:
        return {
            "type": "knowledge",
            "title": title_match.group(1).strip(),
            "fault_type": fault_type_match.group(1) if fault_type_match else "",
            "content": content_match.group(1).strip(),
        }
    return None


def _apply_changes(changes: list[dict], state: dict) -> list[dict]:
    """自动应用改进。

    应用顺序：
    1. Prompt 改进
    2. 专家增强
    3. 知识库补充

    每个类型的改进完成后 git commit。
    """
    applied = []
    iteration_count = state.get("iteration_count", 1)
    fault_type = state.get("fault_type", "unknown")

    # 按类型分组
    prompt_changes = [c for c in changes if c["type"] == "prompt"]
    expert_changes = [c for c in changes if c["type"] == "expert"]
    knowledge_changes = [c for c in changes if c["type"] == "knowledge"]

    # 应用 Prompt 改进
    if prompt_changes:
        for change in prompt_changes:
            result = _apply_prompt_change(change)
            if result.get("success"):
                applied.append({
                    "type": "prompt",
                    "file": change["file"],
                    "action": change["action"],
                    "commit": result.get("commit_hash", ""),
                    "message": result.get("commit_message", ""),
                })

        # Git commit prompt changes
        if applied:
            commit_msg = f"Auto-apply prompt improvements for {fault_type} (iter {iteration_count})"

            # 检查是否有未提交的更改
            try:
                subprocess.run(["git", "add", "prompts/"], check=True, cwd=str(PROJECT_ROOT))
                result = subprocess.run(
                    ["git", "commit", "-m", commit_msg],
                    capture_output=True,
                    text=True,
                    cwd=str(PROJECT_ROOT),
                )
                if result.returncode == 0:
                    # 提取 commit hash
                    commit_hash = result.stdout.split("\n")[0].split(" ")[1] if result.stdout else ""
                    for a in applied:
                        a["commit"] = commit_hash[:8]
                    print(f"\n[改进应用] Prompt 改进已提交: {commit_hash[:8]}")
                else:
                    print(f"\n[改进应用] Git commit 失败: {result.stderr}")
            except subprocess.CalledProcessError as e:
                print(f"\n[改进应用] Git 操作失败: {e}")

    # 应用专家增强
    if expert_changes:
        for change in expert_changes:
            result = _apply_prompt_change(change)  # 专家 prompt 也是 prompt 文件
            if result.get("success"):
                applied.append({
                    "type": "expert",
                    "file": change["file"],
                    "action": change["action"],
                    "commit": "",
                    "message": f"Enhanced {change['expert']} expert",
                })

        # Git commit expert enhancements
        if applied:
            commit_msg = f"Auto-apply expert enhancements for {fault_type} (iter {iteration_count})"
            try:
                subprocess.run(["git", "add", "prompts/maintenance/"], check=True, cwd=str(PROJECT_ROOT))
                subprocess.run(["git", "commit", "-m", commit_msg], check=True, cwd=str(PROJECT_ROOT))
                print(f"\n[改进应用] 专家增强已提交")
            except subprocess.CalledProcessError as e:
                print(f"\n[改进应用] Git 操作失败: {e}")

    # 应用知识库补充
    if knowledge_changes:
        for change in knowledge_changes:
            result = _apply_knowledge_change(change, state)
            if result.get("success"):
                applied.append({
                    "type": "knowledge",
                    "file": result.get("file", ""),
                    "action": "新增",
                    "commit": "",
                    "message": f"Added knowledge doc: {change['title']}",
                })

    return applied


def _apply_prompt_change(change: dict) -> dict:
    """应用单个 prompt 改进。"""
    file_path = PROJECT_ROOT / change["file"]

    if not file_path.exists():
        return {"success": False, "error": f"File not found: {file_path}"}

    action = change.get("action", "增加")
    section = change.get("section", "")
    content = change.get("content", "")

    try:
        # 读取当前内容
        current_content = file_path.read_text(encoding="utf-8")

        # 根据 action 执行修改
        if action == "增加":
            # 在文件末尾或指定 section 后增加
            if section:
                # 查找 section 并在其后增加
                import re
                # 尝试找到 section（如 "## 根因定位" 或 "根因定位"）
                section_pattern = re.compile(
                    rf"(##\s*{re.escape(section)}.*?\n(?:.*?\n)*?)(?=##|\Z)",
                    re.MULTILINE
                )
                match = section_pattern.search(current_content)
                if match:
                    # 在 section 结束位置插入
                    insert_pos = match.end()
                    new_content = (
                        current_content[:insert_pos] +
                        "\n" + content + "\n" +
                        current_content[insert_pos:]
                    )
                else:
                    # section 未找到，在文件末尾增加
                    new_content = current_content + "\n\n" + content + "\n"
            else:
                # 无指定 section，在文件末尾增加
                new_content = current_content + "\n\n" + content + "\n"

        elif action == "修改":
            # 替换指定 section
            if section:
                import re
                section_pattern = re.compile(
                    rf"(##\s*{re.escape(section)}.*?\n(?:.*?\n)*?)(?=##|\Z)",
                    re.MULTILINE
                )
                match = section_pattern.search(current_content)
                if match:
                    new_content = (
                        current_content[:match.start()] +
                        f"## {section}\n\n{content}\n\n" +
                        current_content[match.end():]
                    )
                else:
                    # section 未找到，在文件末尾增加
                    new_content = current_content + "\n\n## " + section + "\n\n" + content + "\n"
            else:
                return {"success": False, "error": "修改需要指定 section"}

        elif action == "删除":
            # 删除指定 section
            if section:
                import re
                section_pattern = re.compile(
                    rf"##\s*{re.escape(section)}.*?\n(?:.*?\n)*?(?=##|\Z)",
                    re.MULTILINE
                )
                new_content = section_pattern.sub("", current_content)
            else:
                return {"success": False, "error": "删除需要指定 section"}

        else:
            return {"success": False, "error": f"Unknown action: {action}"}

        # 写入修改后的内容
        file_path.write_text(new_content, encoding="utf-8")

        print(f"\n[改进应用] {action} prompt: {change['file']}")
        if section:
            print(f"  Section: {section}")
        if change.get("reason"):
            print(f"  Reason: {change['reason']}")

        return {"success": True}

    except Exception as e:
        return {"success": False, "error": str(e)}


def _apply_knowledge_change(change: dict, state: dict) -> dict:
    """应用知识库补充。"""
    kb_config = state.get("config", {}).get("knowledge_base", {})
    output_dir = PROJECT_ROOT / kb_config.get("output_dir", "knowledge_base")
    output_dir.mkdir(parents=True, exist_ok=True)

    fault_type = change.get("fault_type", "unknown")
    title = change.get("title", "Untitled")
    content = change.get("content", "")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{fault_type}_{timestamp}.md"

    file_path = output_dir / filename

    # 写入知识库文档
    doc_content = f"""# {title}

故障类型: {fault_type}
生成时间: {timestamp}
迭代次数: {state.get('iteration_count', 1)}

{content}
"""
    file_path.write_text(doc_content, encoding="utf-8")

    print(f"\n[改进应用] 知识库文档已添加: {filename}")

    return {"success": True, "file": str(file_path)}


def _should_continue_iteration(text: str, score: int, iteration: int, max_iterations: int) -> bool:
    """判断是否应该继续迭代。"""
    import re

    # 检查迭代次数上限
    if iteration >= max_iterations:
        return False

    # 检查评分
    target_score = 90
    if score >= target_score:
        return False

    # 从文本中提取建议
    match = re.search(r"CONTINUE_ITERATION:\s*(yes|no)", text, re.IGNORECASE)
    if match:
        return match.group(1).lower() == "yes"

    # 默认：如果评分低于目标且未达上限，继续迭代
    return score < target_score


def _save_improvement_report(state: dict, content: str, applied_changes: list[dict]) -> str:
    """保存改进报告到文件。"""
    output_dir = PROJECT_ROOT / "self_test_reports"
    output_dir.mkdir(parents=True, exist_ok=True)

    iteration_count = state.get("iteration_count", 1)
    fault_type = state.get("fault_type", "unknown")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"improvement_{fault_type}_iter{iteration_count}_{timestamp}.md"

    file_path = output_dir / filename

    # 添加应用记录
    applied_section = "\n\n## 已应用的改进\n\n"
    for change in applied_changes:
        applied_section += f"- [{change['type']}] {change['file']}: {change['action']}\n"
        if change.get("commit"):
            applied_section += f"  Commit: {change['commit']}\n"
        if change.get("message"):
            applied_section += f"  Message: {change['message']}\n"

    full_content = content + applied_section
    file_path.write_text(full_content, encoding="utf-8")

    return str(file_path)


def rollback_last_prompt_changes(commit_hash: str) -> bool:
    """回退最近的 prompt 改进。

    用于在改进导致劣化时回退。
    """
    try:
        result = subprocess.run(
            ["git", "revert", "--no-commit", commit_hash],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
        )
        if result.returncode == 0:
            print(f"\n[回退] 已回退 commit: {commit_hash}")
            return True
        else:
            print(f"\n[回退] Git revert 失败: {result.stderr}")
            return False
    except Exception as e:
        print(f"\n[回退] 回退失败: {e}")
        return False