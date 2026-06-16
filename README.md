# Lumen - 内核维护自动化工作流系统

基于 LangGraph 构建的独立多 Agent 工作流系统，自动化处理内核维护问题（死锁、panic、softlockup等），支持自迭代验证和能力持续提升。

---

## 快速开始

### 1. 环境准备

```bash
# 克隆项目
git clone https://github.com/your-org/lumen.git
cd lumen

# 安装依赖
pip install -r requirements.txt
```

### 2. 一键部署

```bash
# 运行部署脚本
bash deploy.sh
```

脚本会自动完成：
- ✓ 检查 Python 环境（≥3.8）
- ✓ 安装项目依赖
- ✓ 创建配置文件模板
- ✓ 初始化知识库目录

### 3. 配置

编辑 `maintenance_config.json`：

```json
{
  "default": {
    "api_key": "your-api-key-here",
    "base_url": "https://api.openai.com/v1",
    "model_name": "gpt-4o-mini"
  },
  "kernel": {
    "vmlinux_path": "/path/to/your/vmlinux"
  }
}
```

---

## 核心功能

### 1. 内核故障分析工作流

自动化分析内核故障，从输入校验到知识归档形成完整闭环。

**使用方法：**

```bash
python main.py --input "问题描述" --config maintenance_config.json
```

**工作流程：**

```
用户输入 → Validator校验 → PM分类 → 工具专家并行分析
  → 内核专家综合 → 测试专家验证 → 知识库归档
```

### 2. 自迭代验证系统

自动生成内核故障测试案例，验证专家分析能力，持续改进提示词。

**使用方法：**

```bash
python self_test_main.py --fault_type deadlock --max_iterations 5
```

**支持的故障类型：**
- `nullptr` - 空指针解引用
- `deadlock` - 死锁
- `softlockup` - 软锁定
- `panic` - 内核崩溃
- `stack_overflow` - 栈溢出

---

## 内嵌技能

项目包含完整的技能系统，无需外部依赖：

| 技能 | 功能 | 路径 |
|------|------|------|
| **kernel-fault-injection** | 故障注入测试 | `skills/kernel-fault-injection/` |
| **kernel-build** | 内核编译 | `skills/kernel-build/` |
| **qemu-test** | QEMU测试 | `skills/qemu-test/` |
| **vmcore-analyzer** | Crash分析 | `skills/vmcore-analyzer/` |
| **rag-case-retrieval** | 知识库检索 | `skills/rag-case-retrieval/` |
| **lock-analyzer** | 锁分析 | `skills/lock-analyzer/` |

---

## 项目结构

```
lumen/
├── skills/                    # 内嵌技能系统
│   ├── kernel-fault-injection/
│   ├── kernel-build/
│   ├── qemu-test/
│   ├── vmcore-analyzer/
│   ├── rag-case-retrieval/
│   ├── lock-analyzer/
│   └── shared/
│       └── aicrasher/         # Crash分析核心模块
│
├── agents/                    # Agent实现
│   ├── validator.py
│   ├── pm.py
│   ├── tool_expert.py
│   ├── kernel_expert.py
│   ├── test_expert.py
│   ├── crash_tools.py
│   └── self_test/
│
├── graph/                     # LangGraph图定义
├── prompts/                   # 提示词模板
│   ├── maintenance/
│   └── self_test/
│
├── main.py                    # 主工作流入口
├── self_test_main.py          # 自测试入口
├── config.py                  # 配置加载器
├── deploy.sh                  # 一键部署脚本
│
├── maintenance_config.json    # 主工作流配置
├── self_test_config.json      # 自测试配置
└── kernel_config.example.json # 内核配置示例
```

---

## Agent 职责

| Agent | 职责 | 关键能力 |
|-------|------|---------|
| **Validator** | 校验输入完整性 | 必填字段检查 |
| **PM** | 问题分类与分发 | Fan-out并行调度 |
| **工具专家** | 专业领域分析 | 知识库搜索、Crash分析、锁分析 |
| **内核专家** | 构造复现用例 | 综合分析、测试方案设计 |
| **测试专家** | 验证问题复现 | QEMU测试、内核构建 |
| **知识库生成** | 归档分析结果 | 结构化文档生成 |

---

## 环境依赖

运行完整功能需要以下环境：

1. **内核源码**：配置 `kernel_config.json` 中的路径
2. **QEMU**：用于内核测试和故障注入
3. **Crash工具**：用于vmcore分析（`/usr/bin/crash`）
4. **ChromaDB**：用于知识库向量检索（可选）

---

## 常见问题

**Q: 如何配置内核路径？**

编辑 `maintenance_config.json` 或 `kernel_config.json`：

```json
{
  "kernel": {
    "vmlinux_path": "/path/to/vmlinux",
    "source_dir": "/path/to/kernel/source"
  }
}
```

**Q: 技能脚本在哪里？**

所有技能脚本位于 `skills/<skill-name>/scripts/` 目录，例如：
- `skills/kernel-fault-injection/scripts/run_fault_injection.sh`
- `skills/rag-case-retrieval/scripts/retrieve_cases.py`

**Q: 如何添加新的工具专家？**

1. 在 `prompts/maintenance/` 创建提示词文件
2. 在配置文件 `tool_experts` 数组添加专家定义
3. 无需修改代码

---

## 技术栈

- **LangGraph**: 多Agent工作流编排
- **LangChain**: LLM调用框架
- **aicrasher**: Crash分析核心模块（内嵌）
- **QEMU**: 内核测试环境

---

## 许可证

Apache 2.0