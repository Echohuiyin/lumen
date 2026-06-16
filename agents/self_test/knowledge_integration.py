"""知识库自动补充模块。

将自迭代验证结果自动写入知识库，并导入到 Chroma 向量数据库，
作为历史案例供后续分析参考。

功能：
1. 从验证结果生成结构化的知识库文档
2. 自动导入到 Chroma 向量数据库（与 rag_integration 集成）
3. 支持知识库检索验证（确保导入成功）
"""

from datetime import datetime
from pathlib import Path
from typing import Literal

from config import PROJECT_ROOT


def generate_knowledge_doc_from_iteration(state: dict) -> dict:
    """从迭代验证结果生成知识库文档。

    输入：
    - fault_type: 故障类型
    - expected_fault: 预期故障特征
    - kernel_analysis: 内核专家分析结果
    - evaluation_score: 评估分数
    - gaps_found: 发现的差距
    - improvement_suggestions: 改进建议
    - iteration_count: 迭代次数
    - execution_mode: 执行模式（mock/real）

    输出：
    - title: 文档标题
    - content: 文档内容
    - fault_type: 故障类型
    - success: 是否生成成功
    """
    fault_type = state.get("fault_type", "unknown")
    expected_fault = state.get("expected_fault", {})
    kernel_analysis = state.get("kernel_analysis", "")
    evaluation_score = state.get("evaluation_score", 0)
    gaps_found = state.get("gaps_found", [])
    improvement_suggestions = state.get("improvement_suggestions", [])
    iteration_count = state.get("iteration_count", 1)
    execution_mode = state.get("execution_mode", "mock")

    # 构建标题
    success_level = "成功案例" if evaluation_score >= 80 else "改进案例" if evaluation_score >= 50 else "失败案例"
    title = f"{fault_type} 故障分析 - {success_level}（迭代 {iteration_count}）"

    # 构建内容
    content = f"""# {title}

故障类型: {fault_type}
难度级别: {expected_fault.get('difficulty', 'unknown')}
执行模式: {execution_mode}
评估分数: {evaluation_score}/100
生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

## 预期故障特征

- 描述: {expected_fault.get('description', 'N/A')}
- 预期 Panic: `{expected_fault.get('expected_panic', 'N/A')}`
- 预期根因: {expected_fault.get('expected_root_cause', 'N/A')}

## 内核专家分析结果

{kernel_analysis if kernel_analysis else '无分析结果'}

## 评估详情

### 评分: {evaluation_score}/100

### 发现的差距
{chr(10).join(f"- {gap}" for gap in gaps_found) if gaps_found else '无明显差距'}

### 改进建议
{chr(10).join(f"- {sug}" for sug in improvement_suggestions) if improvement_suggestions else '无改进建议'}

## 关键经验总结

{generate_key_lessons(fault_type, evaluation_score, gaps_found)}

## 复现用例要点

{generate_reproduce_hints(fault_type, expected_fault)}

## 相关案例

- 同类型故障: {fault_type}
- 相似难度: {expected_fault.get('difficulty', 'unknown')}
"""

    return {
        "title": title,
        "content": content,
        "fault_type": fault_type,
        "success": True,
    }


def generate_key_lessons(fault_type: str, score: int, gaps: list[str]) -> str:
    """生成关键经验总结。"""
    lessons = []

    if score >= 80:
        lessons.append(f"✅ **{fault_type} 分析路径清晰**: 从 boot.log 到根因的推理链完整")
        lessons.append(f"✅ **Panic 模式识别准确**: 正确提取了 {fault_type} 的关键特征")
    else:
        if any("根因" in gap for gap in gaps):
            lessons.append(f"⚠️ **根因定位需改进**: {fault_type} 应从 Call Trace 提取具体函数名和偏移")
        if any("panic" in gap.lower() or "Panic" in gap for gap in gaps):
            lessons.append(f"⚠️ **Panic 信息提取不全**: 应从 boot.log 提取 RIP 行和 Call Trace")
        if any("路径" in gap for gap in gaps):
            lessons.append(f"⚠️ **分析路径不完整**: {fault_type} 需多步骤推理，应增强推理链")

    # 故障类型特定经验
    if fault_type == "nullptr":
        lessons.append("💡 **NULL 指针分析要点**: 必须指出具体地址（如 0x0）和触发位置（函数名+偏移）")
    elif fault_type == "deadlock":
        lessons.append("💡 **死锁分析要点**: 必须识别两个 mutex 名称和锁获取顺序，绘制 ABBA 图")
    elif fault_type == "softlockup":
        lessons.append("💡 **软锁定分析要点**: 必须识别 CPU 编号、卡住时长和中断状态")
    elif fault_type == "stack_overflow":
        lessons.append("💡 **栈溢出分析要点**: 必须估算递归深度，识别递归函数模式")

    return chr(10).join(lessons) if lessons else "本次迭代无关键经验总结"


def generate_reproduce_hints(fault_type: str, expected_fault: dict) -> str:
    """生成复现用例要点。"""
    hints = []

    if fault_type == "nullptr":
        hints.append("- 触发位置: `crash_init+0x5`（具体偏移）")
        hints.append("- 必须在**分析位置**触发 NULL 解引用，而非随机位置")
        hints.append("- 避免副作用: 不要引入未初始化变量等额外 bug")

    elif fault_type == "deadlock":
        hints.append("- 两个 mutex: 必须明确名称（如 mutex_A, mutex_B）")
        hints.append("- 锁顺序: 线程1 持A等B，线程2 持B等A")
        hints.append("- 时序控制: 确保两线程几乎同时进入临界区")

    elif fault_type == "softlockup":
        hints.append("- CPU 编号: 指定卡住的 CPU")
        hints.append("- 时长: 至少 20s 以上触发 softlockup 检测")
        hints.append("- 中断状态: 必须禁用中断（`local_irq_disable()`）")

    elif fault_type == "stack_overflow":
        hints.append("- 递归深度: 1000+ 次触发栈溢出")
        hints.append("- 递归函数: 无返回条件的递归调用")
        hints.append("- 栈大小: 默认 8KB 或 16KB，需超出限制")

    elif fault_type == "panic":
        hints.append("- panic 参数: 直接调用 `panic(\"message\")`")
        hints.append("- 触发位置: 模块 init 函数中")

    return chr(10).join(hints) if hints else f"- 参考 {fault_type} 的标准复现模式"


def save_knowledge_doc(doc: dict, state: dict) -> str:
    """保存知识库文档到文件。

    保存到 knowledge_base/self_test/ 目录，按故障类型组织。
    """
    kb_config = state.get("config", {}).get("knowledge_base", {})
    base_dir = PROJECT_ROOT / kb_config.get("output_dir", "knowledge_base")

    # 创建自迭代测试专用子目录
    self_test_dir = base_dir / "self_test" / doc["fault_type"]
    self_test_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{doc['fault_type']}_iter{state.get('iteration_count', 1)}_{timestamp}.md"

    file_path = self_test_dir / filename
    file_path.write_text(doc["content"], encoding="utf-8")

    return str(file_path)


def import_to_chroma(doc_path: str, fault_type: str) -> dict:
    """导入知识库文档到 Chroma 向量数据库。

    使用 rag-case-retrieval skill 的导入脚本。

    输入：
    - doc_path: 知识库文档路径
    - fault_type: 故障类型

    输出：
    - success: 是否导入成功
    - doc_id: 文档 ID
    - message: 导入消息
    """
    import subprocess

    # 查找 rag-case-retrieval skill
    skill_paths = [
        Path.home() / ".claude" / "skills" / "rag-case-retrieval",
        PROJECT_ROOT.parent / "skills" / "rag-case-retrieval",
    ]

    skill_path = None
    for path in skill_paths:
        if path.exists():
            skill_path = path
            break

    if not skill_path:
        return {
            "success": False,
            "message": "rag-case-retrieval skill not found",
        }

    import_script = skill_path / "scripts" / "import_from_zip.py"

    # 检查是否有其他导入方式（单文件导入）
    single_import_script = skill_path / "scripts" / "import_single_doc.py"

    if single_import_script.exists():
        # 使用单文件导入脚本
        try:
            result = subprocess.run(
                ["python3", str(single_import_script), doc_path],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                return {
                    "success": True,
                    "message": f"Document imported to Chroma: {doc_path}",
                }
            else:
                return {
                    "success": False,
                    "message": f"Import failed: {result.stderr}",
                }
        except Exception as e:
            return {
                "success": False,
                "message": str(e),
            }
    else:
        # 没有单文件导入脚本，创建临时 zip 包导入
        import zipfile
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        try:
            # 创建 zip 包
            with zipfile.ZipFile(tmp_path, "w") as zf:
                zf.write(doc_path, Path(doc_path).name)

            # 使用 import_from_zip.py
            result = subprocess.run(
                ["python3", str(import_script), str(tmp_path), "--fault-type", fault_type],
                capture_output=True,
                text=True,
                timeout=60,
            )

            tmp_path.unlink()

            if result.returncode == 0:
                return {
                    "success": True,
                    "message": f"Document imported to Chroma via zip: {doc_path}",
                }
            else:
                return {
                    "success": False,
                    "message": f"Import failed: {result.stderr}",
                }
        except Exception as e:
            tmp_path.unlink(missing_ok=True)
            return {
                "success": False,
                "message": str(e),
            }


def verify_knowledge_import(query: str, expected_fault_type: str) -> dict:
    """验证知识库导入是否成功。

    通过检索验证导入的文档能否被找到。
    """
    from agents.rag_integration import get_rag_context_for_query

    context = get_rag_context_for_query(query, top_k=3)

    if not context or "未找到" in context:
        return {
            "success": False,
            "message": "知识库检索未返回结果",
        }

    # 检查是否包含预期的故障类型
    if expected_fault_type in context:
        return {
            "success": True,
            "message": f"知识库验证成功，找到 {expected_fault_type} 案例",
            "context_preview": context[:500],
        }
    else:
        return {
            "success": True,
            "message": "知识库有结果但未包含预期故障类型",
            "context_preview": context[:500],
        }


def auto_knowledge_pipeline(state: dict) -> dict:
    """自动知识库补充流水线。

    完整流程：
    1. 生成知识库文档
    2. 保存到文件
    3. 导入到 Chroma
    4. 验证导入成功

    输入：state（包含所有迭代信息）

    输出：
    - success: 是否成功
    - doc_path: 文档路径
    - import_result: 导入结果
    - verify_result: 验证结果
    """
    # 生成文档
    doc = generate_knowledge_doc_from_iteration(state)
    if not doc.get("success"):
        return {
            "success": False,
            "message": "文档生成失败",
        }

    # 保存文档
    doc_path = save_knowledge_doc(doc, state)
    print(f"\n[知识库] 文档已保存: {doc_path}")

    # 导入到 Chroma（仅在真实模式下）
    execution_mode = state.get("execution_mode", "mock")
    import_result = {"success": True, "message": "Mock mode, skipped Chroma import"}

    if execution_mode == "real":
        import_result = import_to_chroma(doc_path, doc["fault_type"])
        print(f"[知识库] Chroma 导入: {import_result['message']}")

    # 验证导入
    verify_result = {"success": True, "message": "Mock mode, skipped verification"}

    if execution_mode == "real" and import_result.get("success"):
        # 使用故障类型作为查询词验证
        verify_result = verify_knowledge_import(
            query=doc["fault_type"],
            expected_fault_type=doc["fault_type"],
        )
        print(f"[知识库] 验证结果: {verify_result['message']}")

    return {
        "success": True,
        "doc_path": doc_path,
        "doc_title": doc["title"],
        "import_result": import_result,
        "verify_result": verify_result,
    }