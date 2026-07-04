# Lumen - 内核维护自动化工作流系统

基于 LangGraph 构建的多 Agent 工作流系统，自动化处理内核维护问题（死锁、panic、softlockup等），聚焦问题定位与根因分析。

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
- ✓ 提供使用示例

### 3. 配置 API Key

编辑 `config.json`：

```json
{
  "default": {
    "api_key": "your-api-key-here",
    "base_url": "https://api.openai.com/v1",
    "model_name": "gpt-4o-mini"
  }
}
```

---

## 核心功能

### 1. 内核故障分析工作流

自动化分析内核故障，从输入校验到知识归档形成完整闭环。

**使用方法：**

```bash
# 基础用法
python main.py --input "问题描述" --config config.json

# 示例：分析死锁问题
python main.py --input "
系统出现死锁现象：
- 进程A持有锁L1，等待锁L2
- 进程B持有锁L2，等待锁L1
- vmcore已保存到 /tmp/vmcore
- vmlinux 文件：/usr/lib/debug/lib/modules/5.10.0/vmlinux
- boot_kernel 文件：/path/to/linux/arch/x86/boot/bzImage
- 内核版本: 5.10.0
" --config config.json
```

说明：`vmlinux` 用于 crash/vmcore 符号分析；QEMU 验证需要可启动的 `boot_kernel`，例如 x86_64 的 `arch/x86/boot/bzImage` 或 ARM64 的 `arch/arm64/boot/Image`。
兼容说明：旧命令中传入 `maintenance_config.json` 时，如果该文件不存在，系统会自动回退到 `config.json`。

**工作流程：**

```
用户输入 → Validator校验 → PM分类 → 工具专家并行分析
  → 内核专家综合 → 测试专家验证 → 知识库归档
```

---

## 配置说明

### 主工作流配置（config.json）

```json
{
  "default": {
    "backend": "openai",           // 后端类型: openai/cli/http
    "api_key": "your-api-key",
    "base_url": "https://api.openai.com/v1",
    "model_name": "gpt-4o-mini",
    "temperature": 0
  },
  "agents": {
    "validator": { "model_name": "gpt-4o-mini" },
    "pm": { "model_name": "gpt-4o" },
    "kernel_expert": { "model_name": "gpt-4o" },
    "test_expert": { "model_name": "gpt-4o" }
  },
  "tool_experts": [
    { "type": "knowledge_search", "name": "知识库搜索" },
    { "type": "lock_analysis", "name": "锁分析" },
    { "type": "crash_analysis", "name": "Crash分析" },
    { "type": "kernel_log_analysis", "name": "内核日志分析" }
  ],
  "workflow": {
    "max_test_attempts": 3       // 测试验证最大尝试次数
  }
}
```

---

## 项目结构

```
lumen/
├── main.py                    # 主工作流入口
├── deploy.sh                  # 一键部署脚本
├── config.py                  # 配置加载器
│
├── config.json                # 主工作流配置
├── requirements.txt           # Python依赖
│
├── agents/                    # Agent实现
│   ├── validator.py           # 输入校验
│   ├── pm.py                  # 问题分类
│   ├── tool_expert.py         # 工具专家（MCP集成）
│   ├── kernel_expert.py       # 内核专家
│   ├── test_expert.py         # 测试专家
│   ├── crash_tools.py         # Crash MCP工具
│   ├── tool_calling_loop.py   # MCP工具调用循环
│   └── backends.py            # LLM后端（OpenAI/CLI/HTTP）
│
├── graph/                     # LangGraph图定义
│   ├── rn_state.py            # 状态定义
│   ├── rn_router.py           # 路由逻辑
│   └── rn_workflow.py         # 工作流构建
│
├── prompts/                   # 提示词模板
│   └── maintenance/           # 主工作流提示词
│       ├── validator.md
│       ├── pm.md
│       ├── kernel_expert.md
│       ├── lock_analysis.md
│       └── crash_analysis.md
│
└── knowledge_base/            # 知识库存储（自动生成）
```

---

## Agent 职责

| Agent | 职责 | 关键能力 |
|-------|------|---------|
| **Validator** | 校验输入完整性 | 必填字段检查 |
| **PM** | 问题分类与分发 | Fan-out并行调度 |
| **工具专家** | 专业领域分析 | MCP工具调用、知识库搜索 |
| **内核专家** | 构造复现用例 | 综合分析、测试方案设计 |
| **测试专家** | 验证问题复现 | QEMU测试、内核构建 |
| **知识库生成** | 归档分析结果 | 结构化文档生成 |

---

## 高级特性

### MCP工具集成

工具专家支持MCP（Model Context Protocol）工具调用：

```bash
# 配置MCP服务器（aicrasher）
# 在config.json中:
"skills": {
  "vmcore_analyzer": {
    "mcp_server": "aicrasher"
  }
}
```

### CLI后端（本地LLM）

```json
{
  "kernel_expert": {
    "backend": "cli",
    "cli_command": "claude -p",
    "cli_timeout": 180
  }
}
```

### LangGraph Studio调试

```bash
langgraph dev
# 在Studio中选择 graph: maintenance
```

---

## 常见问题

**Q: 如何查看历史分析案例？**

查看 `knowledge_base/` 目录下的 `.md` 文件，每个案例包含问题描述、分析过程和解决方案。

**Q: 如何添加新的工具专家？**

1. 在 `prompts/maintenance/` 创建提示词文件
2. 在配置文件 `tool_experts` 数组添加专家定义
3. 无需修改代码

**Q: 测试验证失败会怎样？**

工作流支持循环重试（`max_test_attempts`），失败后反馈给内核专家重新分析，超过限制后输出改进建议。

---

## 技术栈

- **LangGraph**: 多Agent工作流编排
- **LangChain**: LLM调用框架
- **MCP**: 工具调用协议（aicrasher）
- **QEMU**: 内核测试环境

---

## 许可证

Apache 2.0
