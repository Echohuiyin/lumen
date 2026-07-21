# Lumen 开发纲要与规格

## 1. 目标与边界

Lumen 是面向 Linux 内核问题定位、复现和归档的多专家工作流。目标不是保证所有问题自动复现，而是保证：分析证据完整、失败可定位、复现结论可信、结果可归档。

范围包括：输入解析、vmcore/日志/知识检索分析、内核路径分析、QEMU 复现、知识库归档。真实 LLM、QEMU、vmcore、内核源码和交叉编译工具均属于外部不稳定依赖，必须显式处理。

非目标：用 LLM 替代 crash、QEMU、源码工具的确定性结果；把未验证推测写成已复现结论；为通过测试而放宽真实环境或契约要求。

## 2. 架构与职责

```text
input.txt
  → Validator → PM
  → Tool Experts（crash / lock / log / knowledge）
  → Kernel Expert Claude loop（分析 → PoC → 常驻 SSH QEMU 验证）
  → Knowledge Base → Final Response
```

| 组件 | 职责 | 输出事实源 |
|---|---|---|
| Validator | 解析并校验输入 artifact、架构与内核源码 | `input_artifacts_contract` |
| PM | 规则化分类并选择工具专家 | `required_experts` |
| Tool Experts | 采集 crash、锁、日志或历史案例证据；每个结果独立落盘 | `expert_results[].structured_output.artifacts.expert_output_file` |
| Kernel Expert | 一个 Claude loop 按路径读取原始日志和专家结果，构造 PoC 并调用受限 runner | `kernel_contract`、`test_contract` |
| Persistent QEMU runner | 只复用同 kernel/rootfs/arch/recipe 身份的 guest，经 SSH 上传执行 PoC、检查串口证据 | `persistent_test_contract.round-<NN>.json` |
| Knowledge Base | 归档事实、证据、路径附录和测试结果 | `knowledge_file`、`final_response` |

## 3. 开发规格与约束

### 3.0 项目约束

- 约束1：每次修改前都要考虑是否会引入问题，如果有机制变更，要考虑变更的影响。
- 约束2：新增功能都要增加对应单元和在线LLM测试，并且加入门禁，门禁全部通过后才可以pass。
- 约束3：no fallback code， 异常情况优先选择报错。
- 约束4：no monkey patch
- 约束5：Bugfix和开发新功能都要严格开发范围来，不做任何无关修改。修改过程中发现的和修改无关问题仅记录。
- 约束6：一键部署脚本要看护。

### 3.1 配置和输入

- `input.txt` 是用户输入和 `kernel_source` 的唯一来源；`kernel_source` 必须是绝对路径。
- `log:` 是原始日志的唯一文件路径来源；内核专家只接收该路径，按需直接读取，不接收复制的日志正文。
- `vmcore` 与可读取的 `log` 不分别强制，但至少必须存在一个；缺少 log 时，日志专家必须从可读取的 `vmcore`/`vmlinux` 提取完整日志、原子落盘并传递该文件路径。
- `.env`、环境变量和默认目录不得成为第二个 kernel source 配置来源；进程环境变量仅可由已解析的 `input.txt` 派生。
- `boot_kernel_path` 与 ELF `vmlinux` 职责不同；禁止将 ELF `vmlinux` 传给 QEMU 作为启动镜像。
- 目标架构必须在 contract 中明确为 `x86_64`、`arm64` 或 `arm32`，不得由宿主架构静默猜测。

### 3.2 专家和工具

- `agent_capabilities.json`、prompt、运行时工具绑定必须一致，并由 `dev/scripts/check_agent_contracts.py` 校验。
- 能由脚本或规则完成的动作不交给 LLM：artifact 校验、QEMU 参数执行、crash session 管理、路径 contract 校验属于确定性职责。
- LLM 只负责证据归纳、策略选择和面向人的说明；其文本不能覆盖工具证据。
- 内核专家只接收工具专家结果文件路径；各结果文件是可单独审阅和迭代的稳定接口。
- 复现验证只接受结构化 `execution_steps`；runner 按步骤生成受限脚本。不得提交或执行自由 shell 的 `test.sh`。
- 外部动作必须有超时、结构化状态和可理解的失败原因；瞬时错误才能有限重试。
- 一个内核专家 Claude loop 最多执行 9 个完整闭环；每个闭环必须是“分析→修改 PoC→持久 SSH-QEMU 验证”，并连续写入 `persistent_test_contract.round-01.json` 至 `round-09.json`。最终结论只可取最后一轮的确定性 contract，所有前轮结果必须归档。

### 3.3 Contract 与路由

- 下游只消费上游结构化 contract；顶层兼容字段只能由 contract 派生。
- contract 缺字段、类型错误、artifact 不存在或类型不匹配时进入 `blocked`，不得进入 QEMU。
- `ok` 只表示已通过该节点后置校验；`degraded`、`blocked`、`skipped`、`inconclusive` 必须保留原因和已有证据。
- 任何失败都不得删除此前已提交的证据、路径或归档产物。

### 3.4 UAF / 引用计数分析

UAF、kref、refcount、引用泄漏和增减不平衡问题必须输出 `UafAnalysisContract`：

- `ReferenceEvent`：get / put / transfer / free / access 事件及引用变化。
- `RefcountPath`：稳定 path ID、事件、`net_delta`、终态、证据和未知点。
- `PathCoverage`：正常、错误回滚、转移、异步、并发路径的覆盖声明，以及未解析的间接调用。
- `max_likely_path_id` 必须属于候选路径；`reproduction_target_path_id` 必须等于最大可能路径。
- `kernel_commit`、config、入口、对象类型和并发模型必须明确；排除路径必须记录依据。
- 重试只能按 path ID 增加路径和证据，不得删除已发现或已排除的路径。

兼容期内旧 `all_possible_paths: list[str]` 和 marker 仍可读取，但会转换为 `legacy_unstructured`；结构化 contract 是新的事实源。

### 3.5 复现可信度

UAF/refcount 的受限 runner 必须在 SSH 执行结构化 `execution_steps` 前向串口写入：

```text
LUMEN_REPRO_START:<case-id>:<path-id>
LUMEN_REPRO_END:<case-id>:<path-id>:<status>
```

只有同时满足以下条件才能标记 `PASSED_REPRODUCED`：

```text
QEMU 已启动
∧ REPRO_START 已出现
∧ 目标信号出现在 START 之后
∧ 信号上下文匹配目标模块、函数或对象
```

启动期异常、回显 expected signal、无关 WARNING/KASAN、缺少目标上下文都属于假阳性，必须返回失败及原因。

### 3.6 资源、持久化与错误

- crash session 使用共享 lease；每条成功、异常、超时和提前返回路径都必须 release。
- 常驻 QEMU guest 的身份必须绑定 boot kernel、rootfs、架构和 recipe 的摘要；身份变化时受控轮换，不得复用旧 guest。
- 每次 PoC 上传到新的 guest 临时目录；串口、SSH 输出和结果 JSON 必须保留。
- crash 启动只对 `TRANSIENT` 错误做节点内有限重试；无效输入、权限、认证和 contract 错误不重试。
- Agent 输出、session 元数据和知识库文件必须原子写入，避免半写产物被下游误用。
- 语义化错误至少包含：类别、代码、原因、是否可重试和下一步动作。

## 4. 最终输出与归档

Knowledge Base 和 CLI 最终响应必须包含确定性附录，不依赖 LLM 是否复述：

```text
分析范围
所有候选路径
已排除路径及依据
最大可能路径和选择依据
复现目标
复现结果与假阳性检查
未覆盖范围和下一步建议
```

归档必须保留原始结构化 UAF contract。QEMU 缺失、复现失败、环境跳过和工具失败均不得删除路径分析。

## 5. 已完成的迭代记录

| 时间 | 内容 | 结果 |
|---|---|---|
| 2026-06-21 | 能力清单、输入 artifact contract、kernel source 校验、Kernel→Test contract | 已完成 |
| 2026-07-14 | P0：路径保留与范围约束、P0 contract 校验、确定性归档附录、原子写和语义错误 | 已完成 |
| 2026-07-17 | P1：结构化 UAF 路径、因果复现标记、crash 启动局部重试与 session lease | 已完成 |
| 2026-07-21 | 合并 Kernel/Test Expert；Claude loop + 常驻 SSH QEMU；原始日志和工具专家结果路径化 | 已完成（真实 QEMU E2E 待部署环境） |

已修复的关键问题：prompt/工具能力漂移、ELF `vmlinux` 被误作 QEMU 镜像、输入路径多来源、Kernel Expert 重试覆盖路径、LLM 归档遗漏路径、既有有效 contract 被宿主 headers 缺失阻断、kernel-log 异常路径泄漏 crash session。

## 6. 当前状态

### 已具备

- `kernel_contract`、`test_contract`、`input_artifacts_contract` 与专家能力静态校验。
- x86_64 / arm64 / arm32 artifact 与 QEMU 路径的基础校验。
- UAF/refcount 的 P0/P1 结构化证据、路径单调合并、因果复现判定和最终归档。
- crash session 引用计数管理、原子持久化、离线静态检查与单元测试。
- 单一 Claude 分析/PoC/验证 loop；x86_64/arm64 Debian SSH guest 的部署配方与身份隔离。

### 已知缺口

- P2 已提供 semcode 直接 callee 事件图；尚无全内核完整性证明、跨回调/间接调用解析或可配置的有界传递闭包。
- checkpoint 仅保留输出，不支持跨进程恢复、按节点重放或内容寻址失效。
- 全局 `_session_dir` 等进程级状态尚未完全消除；并发 workflow 隔离需要专项验证。
- LLM、RAG、MCP、QEMU 尚无统一 deadline、取消传播和依赖级熔断。
- 常驻 QEMU 的真实 x86_64/arm64 E2E 依赖本机完成 debootstrap 镜像部署，未在缺少该依赖的离线环境中替代执行。

## 7. 后续开发计划

### P2：路径分析自动化与覆盖质量（已完成 v1）

1. 基于 semcode `find_function`/`find_calls` 构建直接 callee 的 get/put/transfer/free/access 事件图。
2. 自动生成正常、显式错误分支、异步转移和锁/RCU 并发候选；未识别到的类别明确记录为限制，不伪称覆盖。
3. 自动计算 `net_delta`，识别 free 后的已命名 access 候选，并归档未解析的间接调用。
4. 路径、源码位置、commit、覆盖限制和 blocked 原因写入 `UafAnalysisContract`/`semcode_path_analysis`；semcode 不可用时阻断，不回退至 LLM 或源码文本猜测。

源码定位与结构化路径分析分层：凡需要进行内核源码定位的分析，默认优先使用 semcode；UAF、refcount、引用泄漏等生命周期问题在此基础上额外执行本 P2 路径分析。其他问题是否执行结构化路径分析，由对应问题插件的契约决定。

验收：测试 UAF 模块可确定性生成路径与 `net_delta`，明确未覆盖边界，并保持 raw P2 结果到最终归档。

### P2：可恢复工作流

1. 引入持久化 `NodeExecutionContract` 和 `run_manifest.json`。
2. 在 postcondition 通过后提交 checkpoint；支持 `--resume-session` 和 `--replay-node`。
3. 缓存 key 纳入输入、上游 contract、代码、prompt、工具版本和相关配置。
4. 缓存使用原子写、校验和并发保护；禁止缓存瞬时失败和未校验产物。

验收：节点失败后只重跑未提交节点；上游输入或版本变化时正确失效下游。

### P2：外部依赖可靠性与隔离

1. 统一 LLM、RAG、MCP、crash、QEMU 的 preflight、error envelope、deadline 和取消传播。
2. 增加依赖级熔断与有限退避，杜绝整链无条件重跑。
3. 消除进程级 session / kernel source 状态，所有会话参数经 state/contract 显式传递。
4. 增加并发 workflow、超时、缓存损坏和 crash session 异常注入测试。

验收：一次瞬时故障只重试对应节点；不同 session 的输出、源码和 crash 会话不串扰。

## 8. 开发与验收门禁

每次修改遵守最小粒度原则：不改变无关路由，不为测试降低真实约束，新增行为必须同时增加相应负向测试。

```bash
venv/bin/python -m compileall -q agents graph dev/scripts dev/tests
venv/bin/python dev/scripts/check_agent_contracts.py
venv/bin/python dev/scripts/run_static_checks.py
venv/bin/python -m pytest -q
```

真实 LLM/QEMU E2E 作为显式环境门禁运行；离线单元测试不得依赖 API key、真实 vmcore 或 QEMU。
