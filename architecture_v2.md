# Lumen 架构方案 V2：事件溯源、策略约束与插件治理

> 本文是 V1 经抽象审查后的下一版架构方案。它定义目标模型、边界与分阶段演进，不授权直接进行代码实现。

## 1. V2 的定位

Lumen 不是多 Agent 自由协作系统，而是一个以不可变证据为基础、以确定性状态机为控制中心、以领域插件扩展问题语义的调查系统。

```text
LLM：不可信提案者
工具：可审计观测者
插件：受治理的领域策略提供者
Core：唯一的命令发起者、事件写入者与状态迁移者
```

V2 的首要目标：在静态分析、动态实验、LLM 和人工审阅均存在不确定性的情况下，保持结论可信、输出稳定、历史可复放、问题类型可扩展。

## 2. 不可违反的规则

```text
1. LLM 只能产生候选提案，不能产生 Observation 或 Decision。
2. 工具输出首先是 Observation，而不是无条件的事实或结论。
3. 只有 Core 可以追加改变案件状态的 Decision 事件。
4. 所有历史事件 append-only；修正、重跑和规则升级创建新事件或新 Revision。
5. 没有可引用证据，不能形成正向结论。
6. 没有满足前置条件，不能执行实验。
7. 缺工具、缺环境、缺覆盖、工具执行失败时必须显式 blocked/not_covered，禁止任何形式的 fallback 猜测（No Fallback）。遇到失败直接报错，不确定的事情就说不确定，终止当前分支的推导。
```

## 3. 总体结构与权限域

```text
                 ┌─────────────────────────────┐
                 │       人类 / LLM             │
                 │ 提案、查询、解释、审阅        │
                 └──────────────┬──────────────┘
                                │ Candidate
                                ▼
┌────────────────────────────────────────────────────────┐
│                  核心案件机器 Core                       │
│ Command admission → dispatch → event append             │
│ reducer → policy invocation → decision issuance         │
└───────┬─────────────────┬───────────────────────┬───────┘
        │ Command         │ Raw output             │ Policy input
        ▼                 ▼                        ▼
┌──────────────┐  ┌──────────────┐        ┌─────────────────┐
│ Tool Runner  │  │ Evidence Log │        │ Domain Plugin   │
│ semcode/QEMU │  │ immutable    │        │ parser/planner  │
│ crash/eBPF   │  │ append-only  │        │ pure policy     │
└──────────────┘  └──────────────┘        └─────────────────┘
```

系统逻辑上分层，但权限上只分三域：

| 权限域 | 权限 | 明确禁止 |
|---|---|---|
| 证据域 | 提交 Raw Output、证明材料 | 修改或删除历史、直接写入账本 |
| 控制域 | 准入 Command、派发 Worker、执行 pure reducer、唯一追加所有 Event | 编造领域事实 |
| Worker 域 | 工具执行、领域解析、策略计算、LLM 提案、报告解释 | 直接改变案件结论 |

## 4. 命令、事件、状态与决策分离

V2 的关键不是一个可修改的 `state.json`，而是事件溯源：

```text
Command：请求系统执行某动作
Event：已经发生的不可变记录
State：由 Event 经 pure reducer 推导出的物化视图
Orchestrator：监听 State 变化，触发副作用（如派发工具、调用 Policy）
Decision：Core 对符合策略的 PolicyEvaluation 追加的事件
```

示例：

```text
Command: RunSemcode(foo_ioctl)
  → ToolRunStarted
  → ToolRunCompleted
  → ObservationRecorded

Command: RunExperiment(e-17)
  → ExperimentAttemptStarted
  → ExperimentAttemptCompleted
  → ObservationRecorded

PolicyEvaluation: UAF 因果条件全部满足
  → Core 校验引用和版本
  → DecisionIssued(verified_in_scope)
```

`state.json`、`case_summary.json` 和报告均可由事件账本重建，不能作为事实来源。

## 5. 多聚合状态模型与统一词汇表

真实案件同时存在多个假设和实验，不能用一条线性 Case 状态覆盖。V2 定义三个聚合，并统一状态代数：

```text
Case：输入范围、整体归档与最终结案
Hypothesis：候选根因的支持、反证和覆盖状态
Experiment：验证计划、环境、执行 Attempt 与观测
```

```text
Case:
OPEN → INVESTIGATING → CONCLUDED | PAUSED | ARCHIVED

Hypothesis:
PROPOSED → ADMITTED → INVESTIGATING
                     → VERIFIED | REFUTED | INCONCLUSIVE | BLOCKED | PAUSED

Experiment:
PLANNED → ADMITTED → RUNNING
                   → COMPLETED | FAILED | TIMEOUT | PAUSED
```

**聚合规则 (Aggregation Rules)**：
- **Case CONCLUDED**：当且仅当至少 1 个 Hypothesis 达到 `VERIFIED`，且所有运行中的 Experiment 已终止；或所有 Hypothesis 均达到 `REFUTED/INCONCLUSIVE/BLOCKED`。
- **Case PAUSED**：当全局预算耗尽或核心依赖熔断时触发，等待人工干预恢复。
- **Hypothesis 终态映射**：Policy 返回的 `verified_in_scope` 映射为 `VERIFIED`；`refuted_in_scope` 映射为 `REFUTED`；缺失覆盖映射为 `INCONCLUSIVE`；语义错误映射为 `BLOCKED`。

Case 的最终状态由聚合规则产生，而不是由最后一个 Worker 的文本输出决定。

## 6. 不可变事件账本

V2 初期不引入图数据库。每个 Case Revision 使用目录化、append-only 的事件账本：

```text
cases/<case_id>/<revision_id>/
  input/
  artifacts/
  runs/
  events.jsonl
  projections/
    state.json
    case_summary.json
    report.md
```

最小事件集合：

```text
CaseCreated
ArtifactRegistered
ToolRunRequested / Started / Completed
ObservationRecorded
HypothesisProposed / Admitted
ExperimentPlanned / Admitted
ExperimentAttemptStarted / Completed
PolicyEvaluated
DecisionIssued
HumanAttestationRecorded
CaseArchived
```

每个事件使用 Core 统一信封：

```json
{
  "event_id": "sha256:...",
  "case_id": "case:...",
  "case_revision": "rev:...",
  "event_type": "ObservationRecorded",
  "actor": {"kind": "core|runner|plugin|llm|human", "id": "..."},
  "schema_version": "core-event-v1",
  "plugin_id": "uaf",
  "plugin_version": "uaf-policy-1.3.0",
  "input_refs": ["artifact:...", "event:..."],
  "payload": {},
  "created_at": "...",
  "integrity_hash": "..."
}
```

插件 payload 可保持领域不透明；事件来源、版本、引用、哈希、权限和生命周期必须由 Core 统一理解。
（务实注：初期 `integrity_hash` 仅针对单事件 payload 计算，不强制要求严格的链式哈希，以避免并发写入冲突；事件顺序由全局时钟或自增 ID 保证。）

## 7. 最小领域对象

| 对象 | 含义 | 可由谁提出/写入 |
|---|---|---|
| Artifact | 输入、源码、日志、vmcore、配置、二进制、trace | Core 注册 |
| ToolRun | 一次执行的参数、版本、状态和原始产物 | Runner 提交，Core 记录 |
| Observation | 工具对特定范围的观测 | 插件提取，Core 验证后追加 |
| Hypothesis | 可证伪的候选解释 | LLM、规则、人类或插件提案；Core 准入 |
| Experiment | 假设对应的声明式实验 | 插件/LLM 提案；Core 准入 |
| PolicyEvaluation | 插件纯策略计算结果 | 插件产生；Core 验证 |
| Decision | 对假设或案件的正式判定 | 仅 Core 追加 |
| HumanAttestation | 有来源、范围和责任的人工证明 | 人类提交（需带签名与覆写矩阵），Core 追加 |

Observation 不是绝对真理。它必须记录工具版本、输入范围和限制，例如 semcode 的 `direct_callee_only`、QEMU 的架构和配置、crash 的符号匹配状态。

## 8. 插件治理：领域语义在插件，判决写入在 Core

插件不能直接返回并写入 `Decision`。它只能返回 `PolicyEvaluation`；Core 在验证后发出正式 Decision。

插件具有四项职责：

```text
Parser
  Raw tool output → Observation proposal

Planner
  已有 evidence → Command / Experiment proposal

Policy
  Evidence refs + plugin payload → PolicyEvaluation

Projector
  Case summary → 领域报告片段
```

插件不得：

```text
直接启动未批准工具
直接追加事件或修改状态
直接写最终 Decision
在 Policy 中调用网络、LLM、未声明文件或当前时间
依赖账本外的隐式状态
```

Policy 是纯函数：

```text
PolicyEvaluation = evaluate(
  plugin_version,
  declared_evidence_refs,
  normalized_plugin_payloads
)
```

```json
{
  "subject": "hypothesis:h1",
  "proposed_status": "verified_in_scope",
  "required_evidence_refs": ["observation:o1", "observation:o2"],
  "unmet_conditions": [],
  "limitations": ["indirect callback not covered"],
  "policy_rule_ids": ["uaf.causal-reproduction.v1"],
  "derivation_trace": [
    {"step": "check_start_marker", "passed": true, "refs": ["observation:o1"]},
    {"step": "check_kasan_context", "passed": true, "refs": ["observation:o2"]}
  ]
}
```

Core 必须验证：插件版本允许、证据引用存在且属于当前 Revision、状态在 manifest 白名单、正向结论条件已满足、规则输入输出可复放。插件必须提供 `derivation_trace`（推导树），拒绝黑盒判定。特别注意：**反证（refuted_in_scope）同样需要证明实验覆盖了目标路径且 oracle 有效，绝不能仅凭“未观察到信号”就得出反证结论**。验证通过后，才写入 `DecisionIssued`。

## 9. Plugin Manifest 与稳定演进

插件通过版本化 manifest 接入：

```text
PluginManifest
  plugin_id
  plugin_version
  compatible_core_versions
  payload_schema_versions
  supported_artifact_kinds
  supported_observation_kinds
  command_kinds
  experiment_kinds
  decision_statuses
  policy_rule_ids
  resource_limits
  required_capabilities
```

稳定性规则：

```text
Core Event Envelope 只向后兼容演进；
插件 payload 由插件版本管理；
旧 Case Revision 永远用记录的 plugin/policy/tool 版本复放；
规则升级不得改写旧 Decision；
重新分析必须创建新 Revision。
```

## 10. Case Revision

下列变化不能覆盖历史 Case，而应创建新的 Revision：

```text
kernel source commit 变化
input.txt 的关键工件或路径变化
kernel config 变化
semcode / crash / QEMU 版本变化
plugin policy 变化
新增会改变判定范围的人工证明
```

```text
Case C-42
  R1：linux commit A，uaf-policy 1.0
  R2：linux commit B，uaf-policy 1.0
  R3：linux commit B，uaf-policy 1.1
```

同一 Revision 的输出应稳定；新 Revision 可以产生不同结论，但必须说明变化的输入、工具或规则来源。

## 11. 动态实验：Attempt 是一等对象

Experiment 不等于一次运行。尤其对竞态、时序和硬件问题，每一次执行都是独立 Attempt：

```text
Experiment E-17
  A-1：QEMU 未启动，blocked
  A-2：触发器执行，但未覆盖目标交错
  A-3：出现异常，但上下文不匹配
  A-4：触发器后出现目标异常，路径匹配
```

Attempt 结果应区分：

```text
environment_invalid
workload_not_started
target_condition_not_covered
similar_signal_only
target_signal_observed
runner_timeout
tool_failure
```

Experiment 的聚合状态由 Attempt 事件推导，任何失败 Attempt 均不得覆盖其他 Attempt 的证据。

## 12. 静态与动态的收敛闭环

```text
静态 Observation
  → Hypothesis
  → Experiment
  → 动态 Observation
  → Plugin PolicyEvaluation
  → Core Decision
  ├─ verified_in_scope：进入归档/修复分析
  ├─ refuted_in_scope：保留反证，切换候选
  ├─ inconclusive：扩展路径或调整实验
  └─ blocked/not_covered：记录精确缺口
```

当前 P2/UAF 是第一个领域投影：

```text
semcode: get → put → free → access
  → UAF Hypothesis
  → QEMU + START marker + KASAN oracle
  → 动态 Observation
  → uaf PolicyEvaluation
  → Core DecisionIssued
```

`UafAnalysisContract`、`net_delta`、`PathCoverage` 和因果 marker 保持为 UAF 插件 payload，不应被立即推倒重写。

## 13. 拆分当前 Kernel Expert 与 No Fallback 纪律

当前 Kernel Expert 同时汇总证据、分析源码、提出根因、选择路径、设计实验、写复现器、编译和解释结果，负荷过大且上下文相互污染。更严重的是，它经常在工具失败时进行隐式的 Fallback 猜测。

V2 将其拆成受 Core 调度的专职 Worker，并严格执行 **No Fallback（禁止回退猜测）** 纪律：
- **工具失败即终态**：如果工具（如 semcode, QEMU）执行失败、超时或返回非 0 退出码，Core 必须立即将当前 Hypothesis 标记为 `BLOCKED` 或 `INCONCLUSIVE`，**绝对禁止**将错误日志发给 LLM 让其猜测原因或继续推导。
- **显式声明“不知道”**：插件在遇到无法解析的代码（如复杂函数指针）、缺失的环境或未触发的条件时，必须在 `PolicyEvaluation` 中显式返回 `not_covered_reasons` 或 `unknown_factors`。不确定的事情就说不确定，绝对禁止插件或 LLM 进行脑补。Core 看到此字段，即禁止得出正向结论。

```text
工具专家
  ↓ 原始 Observation
证据整理器 Evidence Assembler
  ↓ 确定性 Evidence Projection
静态路径分析器 Static Analyzer
  ↓ Path / Observation / NotCovered
假设管理器 Hypothesis Manager
  ↓ CandidateHypothesis
实验规划器 Experiment Planner
  ↓ ExperimentSpec
复现构建器 Reproducer Builder
  ↓ Build Observation / Artifacts
实验执行器 Experiment Runner
  ↓ Attempt / Runtime Observation
证据判定器 Evidence Policy
  ↓ PolicyEvaluation
Core
  ↓ DecisionIssued
```

每个 Worker 只能读取完成职责所需的最小上下文：

| Worker | 可读取 | 可输出 | 不可做 |
|---|---|---|---|
| Evidence Assembler | ToolRun、Observation | Evidence Projection | 根因判断、构建复现器 |
| Static Analyzer | 源码工件、相关 Observation | 事件图、路径、覆盖边界 | 判定复现成功 |
| Hypothesis Manager | 路径、现场证据摘要 | CandidateHypothesis、排序理由 | 直接下结论 |
| Experiment Planner | 已准入假设、路径、环境能力 | ExperimentSpec | 重新解释全部原始日志 |
| Reproducer Builder | 已批准 ExperimentSpec | 构建工件、Build Observation | 修改假设或目标路径 |
| Experiment Runner | 已批准实验 | Attempt、原始运行观测 | 解释根因 |
| Evidence Policy | oracle、路径、运行观测 | PolicyEvaluation | 写报告、改变状态 |

“找可能原因”与“构造复现器”必须分离。复现构建器仅接收：

```text
hypothesis_id
target_path_id
目标事件序列
前置条件与禁止副作用
目标内核/架构/环境能力
oracle 与 causal marker
```

它不应接收完整 vmcore、所有工具专家原始文本或无关候选路径。

实验失败首先转成结构化 Attempt 结果；状态机再决定将问题送往路径扩展、实验重规划、环境准备或案件阻断，禁止把整段失败日志无差别回灌给“万能专家”。

## 14. 经验复用层：现场特征驱动的受控 RAG

构造复现器和设计测试步骤确实高度依赖人类经验。V2 不应假设 LLM 能从零推导可靠复现方法；应将经过验证的案例、实验策略和环境约束沉淀为**复现知识库**，通过 RAG 作为 Experiment Planner 的受控输入。

RAG 的权限边界与 LLM 相同：它提供候选知识，不提供当前 Case 的事实，更不能直接写入 Hypothesis、Experiment 或 Decision。

```text
历史已验证 Case / 人工 Runbook / 环境能力目录
  → 版本化复现知识库
  → 根据当前现场特征检索
  → CandidateReproductionStrategy
  → 插件与 Core 准入
  → ExperimentSpec
```

### 14.1 复现知识条目的最小结构

知识库不能只保存自然语言总结。每条可复用经验必须携带可判定的前置条件和反例：

```text
ReproductionKnowledge
  knowledge_id / version / provenance
  problem_family: uaf | deadlock | race | ...
  applicability: 子系统、对象模型、入口、架构、内核配置、硬件条件
  preconditions: 必须满足的环境与现场特征
  strategy: 触发方式、并发模型、注入方式、观测方式
  oracle: 成功、相似信号、未覆盖和失败的区分标准
  known_non_applicability: 明确不适用的条件
  artifact_refs: 已验证源码、脚本、日志、历史 Case
  validation_scope: 曾在哪些 commit/config/环境中验证
  limitations
```

例如，“KASAN UAF 的模块复现器”不是通用答案；它必须声明是否需要模块、是否需要特定 ioctl、是否依赖 KASAN、是否适用于目标架构，以及何时应改用用户态 workload 或 trace-only 验证。

### 14.2 现场特征投影：让 AI 知道从哪些维度提取

工具专家和插件先从当前 Case 的 Artifact/Observation 中生成确定性的 `InvestigationBrief`。它不是结论，只是有证据引用的结构化现场投影；LLM/RAG 只能消费该投影，而不是任意拼接的原始上下文。

```text
InvestigationBrief
  issue classification: UAF / refcount / deadlock / race / unknown
  failure signal: KASAN / lockdep / hung task / timeout / ...
  subsystem and source locations
  entry points and external trigger surface: ioctl / syscall / fs / driver / workqueue
  object/resource identity and lifecycle events
  static path facts and not_covered edges
  concurrency model: process / IRQ / RCU / workqueue / multi-CPU
  dynamic context: task, stack, timing, allocation/free context
  architecture, kernel commit, config, sanitizer, hardware constraints
  available experiment capabilities: QEMU, rootfs, module build, tracing, device access
  missing prerequisites and explicit unknowns
  evidence_refs for every populated field
```

不同插件声明自己的必需和可选维度。UAF 重点是对象生命周期、get/put/free/access、分配释放栈、回调与并发模型；死锁重点是锁对象、持锁顺序、等待图、任务状态和 lockdep 证据。未被 Observation 支持的字段必须是 `unknown`，不能由 RAG 或 LLM 填充。

### 14.3 分阶段检索，而非一次性“找相似案例”

RAG 应按调查阶段检索不同知识：

| 阶段 | 检索目标 | 输出 |
|---|---|---|
| 现场分诊 | 相似问题族、必要工件、缺失条件 | CandidateQuery / 采集建议 |
| 静态分析 | 常见资源/锁模型、已知关键函数族 | 路径扩展候选 |
| 实验规划 | 已验证触发策略、环境与 oracle | CandidateReproductionStrategy |
| 实验失败 | 常见失败分类与下一步观测手段 | CandidateExperiment adjustment |
| 修复验证 | 历史修复模式与回归测试策略 | CandidateValidationPlan |

检索结果应记录为可审计事件：查询使用的 `InvestigationBrief` 版本、召回的 knowledge ID/version、过滤原因和最终采纳/拒绝原因。它是“关于历史知识的 Observation”，不是“关于当前问题的 Fact”。

### 14.4 知识到实验的准入链

```text
RAG 命中知识条目
  → CandidateReproductionStrategy
  → 检查当前现场特征与 applicability/preconditions
  → Plugin Planner 生成 CandidateExperiment
  → Core 检查环境能力、工件、权限和资源预算
  → ExperimentAdmitted 或 blocked/not_applicable
```

知识条目不适用、前置条件不足或与现场证据冲突时，必须产生 `not_applicable` 或 `blocked` 记录，而不是让 LLM 强行套用模板。

### 14.5 人类经验的持续沉淀

当人工修复或实验成功后，经验不应只留在报告里。应经过审阅后沉淀为新的版本化知识条目，并保留：

```text
原始 Case/Revision 引用
成功与失败 Attempt
适用范围
不适用范围
所需环境
oracle
人工审阅者
```

这样，系统的经验增长路径是：人类经验 → 已验证 Case → 受审阅知识条目 → 受约束 RAG 候选 → 新实验，而不是“把历史报告直接喂给 LLM”。

## 15. LLM 的 V2 权限

LLM 是权限最小的外部提案器：

```text
输入：受控 Evidence Projection
输出：CandidateHypothesis / CandidateQuery / CandidateExperiment / OptionalNarrative
```

约束：

```text
CandidateHypothesis 必须引用已有 Observation；
CandidateExperiment 必须通过插件和 Core 的 precondition 校验；
OptionalNarrative 不进入机器结论；
LLM 不可访问事件写入、Decision、密钥、任意 shell 或未授权路径。
```

系统演进的正方向是：经验证的 LLM 启发式逐渐沉淀为 Parser、Validator、Policy 或 Golden Case，LLM 对核心决策的影响持续收缩。

## 16. 输出、审计与可观测性

输出分三级：

```text
1. Evidence Ledger：完整、不可变、可复放
2. Case Summary：Core 确定性投影，是唯一权威结论
3. Human Narrative：模板/LLM 解释，不得影响前两层
```

`case_summary.json` 至少包含：

```text
case_revision
输入与 Artifact 指纹
plugin/policy/tool 版本
hypothesis 与 Decision 状态
experiment 与 attempts
evidence refs
not_covered / blocked 条件
replay instructions
```

指标和 trace 是账本的确定性投影，而不是旁路统计：

```text
各插件的 hypothesis 命中率
policy 的 verified/refuted/blocked 分布
工具成功率、超时率和错误码
状态转换耗时
Attempt 覆盖率
高频 not_covered 边界
规则升级导致的结论变化率
```

## 17. 全局错误处理与熔断策略

在高度依赖外部工具（QEMU、LLM、源码解析器）的系统中，错误处理必须极其冷酷且精确，以贯彻 No Fallback 纪律。

### 17.1 错误分类学 (Error Taxonomy)

Worker 抛出的错误必须是带有明确分类的语义化异常，禁止抛出原生黑盒异常：

1. **基础设施故障 (Infra Failures)**：环境波动导致（如网络超时、宿主机端口冲突、LLM 熔断）。
   - **策略**：在 Attempt 级别有限退避重试。重试超限后，将状态挂起为 `PAUSED`（而非 BLOCKED），等待基建恢复，不污染业务指标。
2. **语义永久故障 (Semantic Failures)**：输入错误或硬性缺失导致（如路径不存在、语法错误、QEMU 参数致 Panic）。
   - **策略**：立即阻断 (Fail Fast & Block)，绝对禁止重试。

### 17.2 状态机拦截与流转

- 遇到语义永久故障时，状态机**立即停止**当前分支的推进，记录 `ToolRunFailed(retryable=False)`。
- 触发状态降级：当前 `Experiment` 或 `Hypothesis` 立即流转为 `BLOCKED` 或 `INCONCLUSIVE`，并附带精确的错误码（如 `ERR_QEMU_BOOT_PANIC`）。
- 若工具正常退出但“什么也没找到”（如静态分析返回空路径），这不是 Error，而是合法的 `Observation`。插件 Policy 需据此返回 `INCONCLUSIVE` 或 `NOT_COVERED`。

### 17.3 熔断与预算机制 (Circuit Breaking & Budgeting)

- **依赖级熔断**：若某外部依赖（如 LLM API）连续多次超时，触发全局熔断。后续请求直接判定为 `PAUSED(reason="CIRCUIT_OPEN")`，避免终结本可继续的调查。
- **预算级熔断**：每个 Case 设定全局计算预算（如最多启动 10 次 QEMU）。预算耗尽时，强制流转到 `PAUSED(reason="BUDGET_EXHAUSTED")`，防止死循环消耗资源。

## 18. 分阶段落地

### V2-A：核心事件语义与双写平滑迁移

为避免大爆炸重构，采用“双写与校验”策略：
1. **旁路双写**：现有 P2 流程继续作为主流程运行，同时新增旁路脚本，将 input、semcode、QEMU 输出实时转换为 Core Event Envelope 并写入 `events.jsonl`。
2. **投影校验**：编写校验器，对比“旧流程生成的 summary”与“从事件账本重建的 summary”。
3. **正式切换**：当历史 Case 的核心决策等价性（Decision Equivalence）达到 100% 一致后（忽略自然语言解释和时间戳差异），切断旧流程，由事件账本正式接管真相来源。

验收：双写期间不影响现有业务，切换后可从事件账本完美重建核心决策。

### V2-B：UAF 插件判定收紧

- 将 UAF contract 适配为插件 payload；
- 将现有验证逻辑提炼为纯 PolicyEvaluation；
- Core 检验证据引用和版本后写入 Decision。

验收：任何插件都不能绕过 Core 直接写“已复现”。

### V2-C：Revision 与 Attempt

- 工件、工具或 policy 变化创建新 Revision；
- 每次 QEMU 执行产生独立 Attempt；
- 支持尝试级因果判定和失败分类。

验收：规则升级不改写历史；多次竞态执行互不覆盖。

### V2-D：插件契约和黄金数据集

- Plugin Manifest、schema、policy、projector 的契约测试；
- UAF Golden Cases；
- 历史 Revision replay regression。

验收：插件升级必须解释结论变化并通过回归。

### V2-E：死锁插件验证通用性

- 使用 Lock/Unlock/Wait/Wakeup 作为 deadlock payload；
- 重用 Case、Hypothesis、Experiment、Attempt、Decision；
- 不修改 Core 状态语义。

验收：证明核心不是 UAF 特化。

### V2-F：受控复现知识库

- 建立由历史已验证 Case 和人工 Runbook 派生的 ReproductionKnowledge；
- 为 UAF 定义 InvestigationBrief 维度、适用性规则和阶段化检索；
- 将 RAG 输出限制为 CandidateReproductionStrategy；
- 记录知识命中、过滤、采纳和拒绝的事件链。

验收：RAG 能帮助选择复现策略，但在前置条件不满足时稳定输出 `not_applicable/blocked`，不产生伪复现结论。

## 19. V2 边界

V2 不做：

```text
全量知识图谱
任意问题的自动根因证明
多 Agent 自由协商
LLM 直接写事实或状态
图数据库优先设计
为自动成功引入 fallback
```

V2 的终点不是更聪明的 Agent，而是一个即使 LLM 服务不可用，仍能保存证据、执行已批准实验、明确输出阻断原因，并复放历史结论的调查系统。
