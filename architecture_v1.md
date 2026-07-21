# Lumen 架构方案 V1：证据溯源的确定性案件状态机

## 1. 目标与原则

Lumen 的目标不是构建“会聊天的内核专家群”，而是构建一个可复放、可审计、可扩展的内核问题调查系统。

V1 的核心原则：

```text
LLM 只能提案，不能认定事实；
工具只能产生观测，不能直接下结论；
状态机依据版本化规则和证据作唯一状态迁移；
最终结论必须可回溯到具体输入、工具执行和证据。
```

系统优先保证：分析结果可信、输出稳定、问题类型可逐步扩展、现有 P2/UAF 能力可平滑纳入，以及外部依赖异常时显式 `blocked`。

## 2. V1 总体结构

```text
┌────────────────────────────────────────────────────┐
│                输出层 / 人机接口                    │
│  deterministic report / JSON summary / LLM 解读     │
├────────────────────────────────────────────────────┤
│              决策层：确定性案件状态机               │
│  reducer + policy + validator + routing             │
├────────────────────────────────────────────────────┤
│              领域插件层                             │
│  UAF/refcount | deadlock | race | filesystem ...    │
├────────────────────────────────────────────────────┤
│              Worker 执行层                          │
│  semcode | crash | QEMU | eBPF | ftrace | LLM       │
├────────────────────────────────────────────────────┤
│              不可变证据账本                         │
│  Artifact | ToolRun | Observation | Hypothesis      │
│  Experiment | Decision | Attestation                │
└────────────────────────────────────────────────────┘
```

逻辑上分为五层；权限上只有三域：

| 权限域 | 能做什么 | 不能做什么 |
|---|---|---|
| 证据域 | 追加工件、工具执行、观测和人工证明 | 修改历史记录 |
| 控制域 | 校验、状态迁移、派发命令、产生结论 | 编造事实 |
| Worker 域 | 执行工具、提出假设、生成报告 | 直接改变案件结论 |

## 3. 最小核心数据模型

V1 不引入图数据库，也不追求完整通用知识图谱。每个 case 使用目录化的 append-only JSON 账本：

```text
cases/<case_id>/
  input/
  artifacts/
  runs/
  evidence.jsonl
  state.json
  report.md
```

### Artifact

原始输入和执行产物。

```json
{
  "id": "artifact:sha256:...",
  "kind": "kernel_source|vmcore|vmlinux|log|trace|binary|config",
  "path": "/absolute/path",
  "sha256": "...",
  "source": "input.txt",
  "created_at": "..."
}
```

### ToolRun

一次工具执行的可复放记录。

```json
{
  "id": "run:...",
  "tool": "semcode",
  "tool_version": "0.1.0",
  "input_refs": ["artifact:..."],
  "arguments": {"function": "foo_ioctl"},
  "started_at": "...",
  "finished_at": "...",
  "exit_code": 0,
  "status": "ok|failed|timeout|blocked",
  "output_refs": ["artifact:..."]
}
```

### Observation

工具从特定环境中得到的观测，不等同于宇宙意义上的事实。核心系统将其视为不透明的载荷（Payload）。

```json
{
  "id": "observation:...",
  "run_id": "run:...",
  "plugin_id": "uaf",
  "kind": "call_edge|stack_frame|kasan_report|lock_wait|trace_event",
  "plugin_payload": {
    "ref_delta": -1,
    "context": "foo_ioctl"
  },
  "scope": {
    "kernel_commit": "...",
    "kernel_config": "...",
    "environment": "qemu-x86_64"
  },
  "limitations": ["direct_callee_only"]
}
```

### Hypothesis

LLM、规则或人类提出的可证伪解释。

```json
{
  "id": "hypothesis:...",
  "statement": "foo_release 后 foo_ioctl 仍访问已释放对象",
  "source": "llm|rule|human",
  "evidence_refs": ["observation:..."],
  "assumptions": ["callback resolves to foo_release"],
  "status": "proposed|admitted|supported|refuted|blocked"
}
```

### Experiment

针对某个假设的可执行验证方案。核心系统不理解具体的触发机制和判定标准，只负责调度执行和透传参数。

```json
{
  "id": "experiment:...",
  "hypothesis_id": "hypothesis:...",
  "plugin_id": "uaf",
  "environment": {
    "runner": "qemu",
    "image": "..."
  },
  "plugin_payload": {
    "target_path_id": "semcode-...",
    "oracle_config": {
      "start_marker_required": true,
      "expected_signal": "BUG: KASAN"
    }
  },
  "status": "planned|running|completed|failed|blocked"
}
```

### Decision

唯一由确定性状态机生成。

```json
{
  "id": "decision:...",
  "subject": "hypothesis:...",
  "status": "supported|verified_in_scope|refuted_in_scope|blocked",
  "policy_version": "uaf-policy-v1",
  "evidence_refs": ["observation:...", "observation:..."],
  "rationale": "deterministic rule result"
}
```

### HumanAttestation

人工输入不能直接伪装成 Fact，而应带身份、范围和依据。

```json
{
  "id": "attestation:...",
  "author": "maintainer",
  "statement": "真实 arm64 板卡上寄存器 X 为 0",
  "artifact_refs": [],
  "scope": "physical-arm64-board",
  "confidence": "high"
}
```

## 4. 案件状态机

Case 总状态由 append-only 事件推导，不手工覆盖：

```text
RECEIVED
  → INPUT_VALIDATED
  → TRIAGED
  → ANALYZING
  → HYPOTHESIS_OPEN
  → EXPERIMENT_PLANNED
  → EXPERIMENT_RUNNING
  → EVIDENCE_EVALUATED
  → VERIFIED_IN_SCOPE | REFUTED_IN_SCOPE | INCONCLUSIVE | BLOCKED
  → ARCHIVED
```

关键规则：

```text
LLM 失败 / 超时 / 格式错误
  → 记录 worker failure
  → 不改变 hypothesis 或 case 的事实状态

semcode 缺失 / 索引缺失
  → BLOCKED
  → 记录缺失前置条件
  → 不允许文本猜测替代

QEMU 未启动
  → Experiment failed/blocked
  → 不得写“未复现”
  → 只能写“实验未执行”或“环境阻断”

QEMU 出现异常信号 (如 KASAN/Lockdep)
  → 仅新增 Observation
  → 仍需交由插件的 EvidencePolicy 进行判定（如匹配 START marker、目标上下文）
  → 状态机根据插件返回的 Decision 才可判定 verified_in_scope
```

结论等级统一为：

```text
observed
supported
verified_in_scope
refuted_in_scope
inconclusive
blocked
not_covered
```

其中 `not_covered` 是一等输出，不是报告末尾的免责声明。

## 5. LLM 的严格职责

LLM 只允许产生三类不可信提案：

```text
CandidateHypothesis
CandidateQuery
CandidateExperiment
```

LLM 可以：

- 将自然语言 Issue 解析为结构化查询；
- 从大量静态路径中推荐优先验证对象；
- 提议实验参数；
- 解释已有证据；
- 生成人类可读报告。

LLM 不可以：

- 写入 `Observation`；
- 修改 `Decision`；
- 创建不存在的路径或工具输出；
- 把 `not_covered` 转换成“无风险”；
- 改写失败实验为成功结论。

所有 LLM 产出先经过 schema、引用完整性和权限校验，才能成为 `Hypothesis` 或 `CandidateExperiment`。

## 6. 静态与动态分析闭环

静态和动态不再是线性上下游，而是围绕同一 Hypothesis 循环收敛。核心状态机负责驱动循环，领域插件负责具体判定：

```text
静态 Observation (Plugin 提取)
  → Hypothesis (Plugin 生成)
  → Experiment (Plugin 规划)
  → 动态 Observation (Plugin 提取)
  → Deterministic Decision (Plugin 判定)
  ├─ verified_in_scope：归档与修复分析
  ├─ refuted_in_scope：保留反证，切换候选
  ├─ inconclusive：扩展静态边或改进实验
  └─ blocked：列出缺失环境/工具/工件
```

以 UAF 插件的执行投影为例：

```text
semcode 路径：get → put → free → access
  ↓
候选假设：释放后访问可能成立
  ↓
QEMU 实验：触发器 + START marker + KASAN oracle
  ↓
动态观测：KASAN 是否在触发后发生、栈是否匹配 foo_ioctl
  ↓
状态机：接收插件 Decision (verified_in_scope / similar_only / refuted_in_scope / blocked)
```

P2 的 `UafAnalysisContract`、`net_delta`、`PathCoverage` 和因果复现 marker 应保留，逐步适配为该通用模型中的 UAF 插件投影。

## 7. 领域插件模型：控制反转与契约驱动

核心系统（状态机）必须保持极简，它不理解 UAF、死锁或竞态的具体语义。它只负责流程调度、状态流转和证据持久化。所有的领域知识必须被封装在插件中。

核心系统与插件之间通过严格的**接口契约 (Interface Contract)** 交互，实现控制反转：

1.  **事件提取契约**：核心系统执行工具，插件负责将工具的原始输出（Raw Output）解析为标准化的 `Observation`。
2.  **假设生成契约**：插件负责调用静态分析工具，并将结果转化为结构化的 `Hypothesis` 提交给核心系统。
3.  **实验规划契约**：核心系统提供环境信息，插件负责生成具体的动态验证方案（`Experiment`）。
4.  **证据判定契约 (核心)**：核心系统不负责判案。它将收集到的 `Observation` 交给插件的判定策略（Policy），插件必须返回一个明确的 `Decision`（如 `verified_in_scope`, `refuted_in_scope`），并附带支持该判决的证据引用。

```text
plugins/
  uaf/
    manifest.json
    event_extractor.py     # 实现事件提取契约
    static_analyzer.py     # 实现假设生成契约
    experiment_planner.py  # 实现实验规划契约
    evidence_policy.py     # 实现证据判定契约
    report_projector.py    # 将结论翻译为人类可读报告

  deadlock/
    ...
```

新增问题类型（如死锁）时，只需新增一个插件目录并实现上述契约，**核心状态机的代码无需任何修改**。

## 8. 输出稳定性设计

系统应有两种输出，且职责分离。

### 机器结论：唯一权威输出

```text
case_summary.json
```

由状态机模板化生成，内容包括：输入和环境指纹、工具版本、已验证/已反驳/未覆盖/阻断项、结论等级、全部证据引用，以及适用范围和限制。

只要证据账本和策略版本不变，该输出必须字节级稳定或语义稳定。

### 人类报告：解释性输出

```text
report.md
```

默认由模板基于 `case_summary.json` 渲染；LLM 可以提供说明性摘要，但必须位于单独章节，不能改变机器结论。

```text
机器结论：verified_in_scope
LLM 解读：解释为何该证据链支持该结论
```

## 9. 分阶段落地路线

### 阶段 A：证据账本适配，不重写现有流程

- `input.txt`、kernel source、vmcore、日志建立 Artifact；
- semcode/crash/QEMU 每次调用生成 ToolRun；
- P2 事件图生成 Observation；
- 现有 contract 保持不变，只新增 `evidence_refs`；
- 输出 `case_summary.json`。

验收：当前 UAF case 可以从现有 contract 映射为完整证据账本。

### 阶段 B：UAF 确定性状态机

- 将 UAF 路径、实验、KASAN、因果 marker 映射到状态迁移；
- 引入 `verified_in_scope / refuted_in_scope / blocked`；
- 保留所有失败实验、路径和未覆盖边界；
- `Knowledge Base` 从账本确定性生成附录。

验收：同一输入、同一工具版本、同一工件下，结论稳定。

### 阶段 C：实验平台化

- `ExperimentSpec`、环境指纹、oracle、预算、cleanup；
- 每次运行产生独立 Attempt；
- 支持实验重放；
- 区分“未触发”“未覆盖”“相似异常”“目标验证”。

验收：竞态、UAF 等失败实验可精确分类，不再只有 pass/fail。

### 阶段 D：第二个领域插件——死锁

- 引入 lock order / wait graph 事件；
- 接入 lockdep、crash task 状态和 QEMU hung-task；
- 使用同一状态机、同一 Experiment、同一 Decision 模型。

验收：UAF 和死锁共用核心，无需修改核心状态迁移语义。

### 阶段 E：插件化与经验固化

- LLM 发现的稳定模式，经人工和实验验证后固化为规则；
- 新规则进入插件的 analyzer/validator/policy；
- 对应场景中 LLM 权限进一步收缩。

## 11. 软件工程实践：可观测性与度量 (Observability & Metrics)

Lumen 作为一个工业级调查系统，必须具备白盒级别的可观测性，以支持系统的自我进化和排障。

### 11.1 核心度量指标 (Core Metrics)

系统必须暴露结构化的指标用于监控和告警：
- **业务结果指标**：Case 结案率分布（`verified`, `refuted`, `blocked` 占比）、平均结案时间 (MTTR)、假设命中率。
- **系统健康指标**：状态机各阶段流转耗时、各 Worker/工具执行成功率、错误码分布（精确统计 `blocked` 的具体原因，如 `QEMU_TIMEOUT`）。

### 11.2 分布式追踪与结构化日志

- **Trace ID 透传**：每个 Case 分配全局唯一的 Trace ID，贯穿状态机、插件和底层执行脚本。
- **Span 记录**：状态迁移、工具执行、LLM 调用必须生成 Span，记录耗时和输入输出指纹。
- **审计追踪 (Audit Trail)**：抛弃纯文本日志，采用结构化 JSON 日志。状态机的每一次判定（Decision）必须在日志中记录完整的证据引用链和策略版本。

### 11.3 持续集成与回归防护 (CI & Regression Gating)

Lumen 自身的演进绝不能破坏已有的分析能力：
- **黄金数据集**：维护包含历史真实 Case（输入指纹 + 预期结论）的测试集。
- **契约测试**：在 CI 阶段验证系统生成的机器结论（JSON）是否符合预期 Schema。
- **变更隔离**：升级领域插件的规则时，必须通过回归测试证明其未导致历史已证实 Case 发生退化。

## 12. V1 的边界

V1 明确不做：

- 全量代码知识图谱；
- 任意问题的自动根因证明；
- 多 Agent 自由协商；
- 让 LLM 直接改状态或写事实；
- 图数据库优先设计；
- 为“自动成功”而引入 fallback。

V1 追求的是：

```text
少量问题类型
严格证据链
稳定状态机
可复放实验
明确 unknown / blocked
可平滑扩展
```

这是一条可落地且可持续演进的路径：先将现有 UAF 能力变成第一个被严格治理的领域插件，再用死锁验证通用性，最后逐步扩大问题类型覆盖。
