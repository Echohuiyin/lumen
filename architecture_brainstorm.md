# Lumen 通用静态/动态问题 Agent：架构头脑风暴

> 本文是架构讨论稿，不是当前实现规格，也不授权直接扩展功能。目标是为后续演进建立稳定的判断框架。

## 1. 重新定义目标

Lumen 不应仅被理解为“多 Agent 工作流”，而应演进为一个以证据为中心、持续将静态可能性与动态现实收敛的**问题调查系统**。

Agent 是推理与决策单元，不是系统事实的载体。系统的核心应是同一案件的可追溯证据账本：各专家向其中追加事实、假设、实验和结果；下游按需读取，而不是依赖上游自由文本转述。

```text
工具执行层 ────────────────┐
semcode / crash / QEMU      │
trace / eBPF / 编译 / fuzz  │
                            ▼
                    证据图 / 案件账本
                    Artifact / Event / Path
                    Hypothesis / Experiment / Result
                            ▲
                            │
静态与动态分析层 ──────────┤
                            ▼
                     LLM / 人类决策层
```

这样，动态验证失败会成为可定位的反证或缺口，而不是笼统地回到 Kernel Expert “再想一遍”。

## 2. 金字塔架构

```text
L5  交付层：报告、知识库、修复建议、复现结论、人机审阅
L4  决策与推理层：LLM 专家、规则、假设排序、实验选择、冲突消解
L3  问题分析层：静态分析、动态分析、静动关联、差分与根因定位
L2  工具执行层：semcode、crash、QEMU、ftrace、eBPF、fuzz、bisect
L1  证据与模型层：Artifact、Observation、Event、Path、Experiment、Result、Claim
L0  治理与可靠性层：输入、版本、权限、超时、checkpoint、审计、可复放
```

L0 是地基，L1 是承重结构。模型、专家和工具都可以迭代，但不得破坏证据、版本和执行可复放性。

## 3. 当前 P2 在架构中的位置

当前 UAF/refcount P2 已形成第一个纵向切片：

```text
input.txt 的 kernel_source
  → semcode Observation
  → get / put / transfer / free / access Event
  → RefcountPath + net_delta
  → UafAnalysisContract
  → QEMU Experiment
  → TestResultContract
```

它应被视作通用“资源与状态事件模型”的第一个实例，而不是永久独立的 UAF 专用体系。

## 4. 静态与动态的统一语言：事件图

通用系统不应只理解引用计数。可逐步收敛到以下核心事件：

```text
Acquire / Release / Transfer
Read / Write / Access
Allocate / Free
Lock / Unlock
Spawn / Join
Schedule / Callback
Check / Assert / ErrorReturn
Crash / Timeout / Hang
```

路径不是字符串，也不只是顺序数组；它是一个带关系的事件子图：

```text
Event A --control--> Event B
Event A --ownership--> Event C
Event A --happens-before--> Event D
Event A --conflicts-with--> Event E
Event A --evidenced-by--> Observation X
```

`net_delta` 保留为资源账本的一种度量。长期可以抽象为：

```text
Resource Ledger
  resource: struct foo / file / page / lock / request
  owner: task / fd / workqueue / RCU callback
  state: acquired / transferred / released / unknown
  delta: +1 / -1 / ownership transfer
  condition: normal / error / async / concurrent
  evidence: source edge / vmcore / trace / experiment log
```

这会将 UAF、资源泄漏、fd 泄漏、锁未释放和 I/O request 泄漏放入同一建模框架。

## 5. 静态、动态与关联器的职责

### 静态分析

回答“可能性空间”：对象如何获得、转移、释放和访问；哪些边已被源码证明；哪些受宏、间接调用、回调或配置影响而未覆盖。

### 动态分析

回答“现实样本”：在确定的环境、配置、输入和调度下执行了什么；是否发生了目标信号；观察结果有哪些边界。

### 静动关联器

负责因果判定，禁止以“出现同类 KASAN”替代“验证目标路径”。最小关联结果应包含：

```text
静态目标路径：p-17
动态实验：exp-09
触发器已开始：是/否
信号发生于开始后：是/否
目标上下文匹配：是/否/部分
静态事件序列被动态支持：n/m
结论：causally_supported / partially_supported / similar_only / refuted / blocked
```

## 6. LLM 的权限边界

LLM 可以：

- 提取与归纳人类输入；
- 提出可证伪假设；
- 对已有候选路径解释风险排序；
- 生成实验草案与下一步建议；
- 将结构化证据翻译为维护者可读结论。

LLM 不可以：

- 伪造源码边、源码位置、配置、栈帧或动态结果；
- 用猜测填补 `not covered`；
- 删除或覆盖既有确定性证据；
- 把“最大可能路径”表述成“已证明根因”；
- 把实验失败静默改写为无结论文本。

建议将 LLM 输出收敛为可审查的假设对象：

```text
Hypothesis
  id
  statement
  supports[]
  contradicts[]
  assumptions[]
  confidence
  falsification_plan[]
  status: pending | supported | refuted | blocked
```

## 7. 专家按认知职责拆分

长期不应只按工具划分专家，而应按认知职责拆分：

| 职责 | 主要输出 |
|---|---|
| 证据采集 | 标准化 Observation，不下根因结论 |
| 静态模型 | 调用、资源、状态、锁、数据流图 |
| 现场取证 | vmcore、日志、trace、寄存器与栈事实 |
| 假设管理 | 候选根因、支持证据与反证关系 |
| 实验设计 | 可执行 ExperimentSpec |
| 实验执行 | QEMU、fuzz、fault injection、trace 结果 |
| 因果验证 | 静态目标与动态观测的关联结论 |
| 修复评审 | patch 是否关闭目标路径、是否引入新路径 |
| 归档 | 原始证据、结论等级与限制条件 |

原则是：采集与推理分离；推理与执行分离；执行与判定分离。

## 8. 动态执行应成为实验平台

动态能力不应只等价于“跑一次 QEMU”。建议长期使用声明式实验模型：

```text
ExperimentSpec
  environment: kernel / config / arch / rootfs / tool versions
  setup: module / workload / fault injection
  target: hypothesis / static path / expected event
  observation: tracepoints / eBPF / KASAN / crash / counters
  oracle: pass / fail / inconclusive 的精确定义
  budget: timeout / retries / concurrency / seed space
  cleanup: 必须执行
```

动态失败必须分类。例如竞态用例至少区分：

```text
未触发 workload
触发 workload 但未覆盖目标交错
覆盖目标交错但未观察到异常
观察到异常但上下文不匹配
观察到目标异常且静动关联成立
```

## 9. 可靠性应内建于底座

- 每次工具调用产生不可变 `ToolRun`：输入指纹、版本、超时、退出码、stdout/stderr 与工件。
- 节点仅在 postcondition 通过后提交 checkpoint。
- checkpoint 使用输入、上游 evidence、代码、prompt、工具版本和配置的内容寻址。
- 重试仅作用于明确可重试的 `ToolRun`，不重跑整个案件。
- 外部依赖统一具有 deadline、取消、熔断和并发预算。
- LLM 输出必须通过 schema 校验；不合格即显式错误，不编造补全。
- 证据追加单调：新分析只能补充、反驳或标记失效，不能静默删除历史。

## 10. 结论等级与未覆盖边界

每条结论都应有明确认识论状态：

```text
proved       工具或实验直接证明
supported    多条独立证据支持，但未完全证明
plausible    合理候选，仍有未验证假设
refuted      被源码、现场或实验反驳
blocked      缺少关键输入、工具或环境
```

未覆盖边界本身是一等对象：

```text
NotCovered
  edge: callback -> release
  reason: indirect function pointer
  impact: may contain an unmatched put
  next_best_action: resolve callback with trace or type analysis
```

## 11. 应避免的陷阱

1. 不要过早建设无所不包的图数据库或知识图谱；先让最小对象和追加规则可靠落地。
2. 通用不等于所有问题走同样长的流程；简单静态问题应允许提前结束。
3. 不要把多 Agent 数量视作能力；确定性分析器加谨慎决策通常优于多轮文本转述。
4. 最大可能路径不是唯一真相；候选、排除路径和反证必须保留。
5. 修复也应进入证据图：它关闭哪些边、动态实验是否不再触发、是否引入新状态转换。

## 12. 建议的下一步

在新增更多专家或工具前，先设计最小“案件账本”规格：

```text
Artifact / Observation / Hypothesis / Experiment / Result / Claim
```

需要明确：稳定 ID、版本/来源、追加与失效规则、关联关系、结论状态机和归档格式。随后以适配器方式将既有 `UafAnalysisContract`、crash 证据和 QEMU `TestPlan` 映射进去，再逐步扩展至死锁、竞态、性能、文件系统、驱动和网络问题。
