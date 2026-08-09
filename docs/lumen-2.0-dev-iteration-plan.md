# Lumen v2.0-dev P0/P1 迭代计划

本计划以远端 `spec.md` 为事实源，只覆盖 P0、P1；不引入新的 Agent、服务进程、MCP/REST 层或自由 shell。Lumen 的定位始终是 Linux 内核维护问题的证据分析、诊断性用户态 C 复现和修复验证，不是漏洞挖掘。

## 约束与验收总门槛

- `cases/*/input.txt` 是唯一执行输入，已经脱敏，不包含 `reproducer:`、`syz_repro:`、`repro.c` 或 `repro.syz` 路径。历史 reproducer 仅存在于审计归档，不得传给 Lumen。
- Kernel Expert 只能基于原始报告、源码和工具证据生成用户态 C contract；Test Expert 才能在真实 QEMU guest 内编译、执行和判定调用链。
- 每次改动先记录引入原因、上下游影响、正负影响和回滚边界；保持最小修改。
- 每个“大迭代”完成后做回归门：已知可复现的 3 个案例各最多 2 次执行（第一次不复现时必须执行第二次）；随后其余 5 个案例各最多 2 次。只记录真实 QEMU/串口结果，不能把 blocked 当作失败复现或成功。
- 当前已知可复现基线：`044fdf24e96093584232`、`0a89a7b56db04c21a656`、`56edda805363e0a093b8`。基线证据来自 archive 中 `independent-test-expert-qemu-rerun-*` 的 `PASSED_CALL_CHAIN_CONSISTENT` contract；其余 5 个为探索组。

## 优先级顺序

| 顺序 | 计划项 | 目标 | 现状 | 交付与验收 |
|---:|---|---|---|---|
| 1 | O-001 + O-002 | 持久化 crash 命令流水；命令准入和结果状态硬门禁 | 已实现，R0 待完成 | `crash_commands.jsonl`、原始输出及 SHA-256；稳定 `evidence_id`；危险 shell 语法不执行；全量必要命令失败为 `blocked`，部分失败为 `degraded`；专项单测通过；R0 真实回归目前被 Kernel Expert Codex 超时阻断，未计为复现结果 |
| 2 | O-005 | Producer Frontier 与 Root Cause Gate | 本轮实施 | `producer_frontier`、逐项 gate、`diagnosis_status`、源码/log evidence；缺 producer 不得 `supported`，不改变独立 QEMU `test_passed`；回归门 R1 |
| 3 | O-003 | 统一实际生效的 crash 分析策略 | 部分实现 | Codex compact maintenance prompt、禁止 broad `git show/diff`、仅 staged evidence/精确源码；部署模板导出 Codex model/reasoning/service 环境，现有配置默认 `xhigh`；仍需补齐统一策略 contract 与负向测试；回归门 R2 |
| 4 | O-004 | 确定性 crash evidence 索引 | 已实现，随 R0 验收 | 每个命令稳定 `evidence_id`，索引到原始输出文件和 SHA-256；失败/空输出可追溯；`ToolExpertOutput.evidence` 与 artifacts 同时携带索引；与 O-001 兼容；专项单测通过；回归门 R0/R3 |
| 5 | O-010 | 确定性故障证据骨架 EvidenceGraph | 待实现 | 统一事实、关系、来源、状态和 extractor 版本；运行时/源码双轨引用；缺证据不得升级 verdict；回归门 R4 |
| 6 | O-006 | ECTM 问题演化与候选机制记录 | 待实现 | `problem_evolution`、候选机制、证据、反证动作、状态；重试增量追加，不删除历史假设；回归门 R5 |
| 7 | O-007 | 条件式定向内核代码审查 | 待实现 | 仅在 producer 缺失、共享状态/引用计数/锁/R​​CU 等触发；输出 `targeted_review`，不改 reproducer/oracle；回归门 R6 |
| 8 | O-008 | try-out 指纹和重复重试门禁 | 部分已有（增量变更、setup/prefix、无进展门） | 补齐 source+plan fingerprint、重复拒绝和审计字段；不改变 10 次硬上限；回归门 R7 |
| 9 | O-011 | 已知/未知双路诊断与证据融合 | 待实现 | `known_path`/`unknown_path`/`fusion_status`；历史候选不得覆盖本地 runtime/source evidence；回归门 R8 |
| 10 | O-013 | 发现、定位、复现、机制、修复分层评测合同 | 待实现 | 独立 verifier 和分层 verdict；`test_passed` 不等于根因闭合；最终 8 案例双轮探索回归 |

P2（O-009、O-012、O-014、O-015）本轮不做，避免扩大架构和验证面。

## 每个迭代项的统一实施模板

1. **问题引入与影响分析**：指出现有行为、错误状态传播、受影响的消费者和不改变的边界。
2. **最小实现**：优先扩展现有 contract、session、Knowledge Base 和 Kernel/Test loop；不新增 Agent。
3. **验证**：新增正向、负向和状态传播测试；运行编译、静态检查、全量单测。
4. **回归门**：执行 Rn 的 3+5 案例，保存 input、源码 commit、POC contract、QEMU overlay、串口、结果 contract；报告复现/不一致/blocked 和是否劣化。
5. **提交与同步**：只提交本项及测试/文档，提交前同步 `v2.0-dev` 并 rebase；不覆盖合作者分支改动。

## O-001/O-002 本轮改动记录

**引入原因**：原 crash 工具只在文本输出中写“等待输出”，最终结果覆盖文件；危险命令仅 warning 仍可能执行；必要命令失败时工具专家仍可能返回 `status=ok`。这会破坏审计、重试去重和 No fallback 约束。

**实现边界**：新增 `agents/crash_command_audit.py`，由 `crash_tools.py` 和 `tool_expert.py` 共用；允许只读 crash 命令和有限管道过滤器；每次命令追加 JSONL、原始输出和 hash；`sys`、`bt -a`、`log | tail -n 200` 全失败为 blocked，部分失败为 degraded；不改变 Kernel Expert、Test Expert、QEMU 和调用链 oracle。

**正面影响**：命令、结果、原始输出和 hash 可重放；危险 shell 片段不进入 crash；模型总结不能覆盖底层失败。

**负面影响/风险**：过窄的 crash allowlist 可能拒绝合法只读命令；因此保留显式 allowlist 测试，新增命令必须先补 contract 和测试，禁止自动放宽。

**当前验收**：专项测试已覆盖 allowlist、危险语法拒绝、JSONL 顺序/hash；还需完成 R0 真实 QEMU 回归后才可标记完成。

## O-003/O-004 增量改动记录

**引入原因**：实际 Codex 命令曾落成默认 `high`，原因是现有 `.env` 的 model/reasoning/service 三项没有 `export`，且已生成 `config.json` 默认值仍为 `high`；同时交互模型可能把整棵内核树作为输入，扩大等待和安全误判面。Crash 输出虽已落盘，但上层证据没有稳定索引，无法跨重试引用同一命令结果。

**实现边界**：部署模板为 Codex 运行选择项写入可覆盖的 `export` 环境变量；`config.json` 的 reasoning 默认与模板统一为 `xhigh`；Codex compact prompt 禁止 broad `git show/diff`，只允许 staged evidence 和有界的精确源码读取。Crash ledger 为 `run_crash_command + command + output_sha256` 生成稳定 `evidence_id`，并在结构化工具证据和 artifacts 中保留它。

**正负影响**：正面是运行时实际模型档位可审计、提示词范围和耗时更稳定、同一输出可去重引用；负面是 `xhigh` 可能增加单次 Codex 延迟，过窄的源码读取约束可能使模型返回 `blocked`，因此保留精确 Semcode evidence 和明确 blocked 结果，不引入 fallback。

**当前验收**：83 个 P0/P1 专项测试（含 3 skipped）和静态检查通过；后续 R13/R14 双轮回归已完成，Codex 未产出 contract 的轮次均按合同/环境 blocked 归档，不伪造复现。

## 回归记录格式

每个大迭代在 benchmark archive 对应目录新增 `e2e-regression/<iteration>/summary.json`，至少包含：代码 commit、输入 hash、expected kernel commit、case、attempt、QEMU 状态、guest compile 状态、`test_passed`、`call_chain_consistent`、串口路径、contract 路径、blocked/failure code、相对基线差异。第一轮不复现时第二轮仍必须执行；两轮都未复现只报告未复现，不伪造成功。

## 当前 P0/P1 批次：contract/input boundary 与环境分类

**引入原因**：旧的 `reproducer_module_path`、legacy one-shot runner 和
Kernel Expert shell tool 仍可被旧 JSON/工具入口接触；这与 C-only、Test
Expert-only QEMU 和 no-fallback 约束冲突。与此同时，显式 `reproducer:`
路径没有在 Validator 边界硬阻断，guest ABI/QEMU 启动阻断可能被错误计入
Kernel/Test trigger loop。

**最小实现**：移除生产 contract/state/runner 的模块字段和模块 staging，
不再合成 rootfs；Kernel Expert 工具面不再暴露 shell。Validator 对显式
reproducer artifact 返回 `BLOCKED_INPUT_REPRODUCER_PRESENT`。Test Result
增加 `failure_class`、`retryable`、`next_action`，QEMU/镜像/guest ABI/合同
preflight 以 blocked 归档且不消耗 try-out；编译失败和真实触发/调用链不一致
仍按有效 try-out 反馈给 Kernel Expert。

**正负影响与边界**：正面是旧模块/脚本不会再通过兼容字段进入执行，输入和
环境问题不会浪费重试预算；负面是依赖旧 one-shot runner、显式 `.ko` 或
未声明 reproducer 的历史测试会明确阻断，必须迁移到 userspace-C contract。
不改变独立 QEMU 的 `test_passed`/调用链判定，也不删除历史归档。

## 增量合同归一化修复（`381aade` 到 `79cce6d`）

这些提交都遵循“先分析引入原因，再做最小兼容映射”的边界：模型明确给出的
字段别名才会被归一化；不会根据源码、setup 或日志推断缺失值，也不会放宽
QEMU/调用链门禁。

- `381aade`、`f872d56`、`872768c`、`f734716`、`1a87198`、`124d2f3`、
  `dee369c`、`00dedc0`：补齐版本化调用链、原始日志帧、严格顺序、schema
  标记和根因字段的显式别名。引入原因是 Codex 合同本身有完整证据，但字段
  名变体让 Validator 误报为空；影响是保留原始 RCA/调用链证据，缺证据仍
  blocked，不新增 fallback。
- `4f98e86`：接受 `incremental_setup.change_from_previous_tryout` 的显式
  增量文本/对象。引入原因是重试合同把已验证 setup 放在嵌套字段，旧解析器
  将其误判为缺少增量；影响是重试能继承 setup，仍强制校验变更声明。
- `79cce6d`：接受顶层或嵌套 `change_from_previous_tryout.incremental_change`
  的显式字符串列表。引入原因是 Smack 第 2 次重试使用列表而非单个 `change`
  字段，导致 `BLOCKED_INVALID_INCREMENTAL_CONTRACT`；影响是该轮真正进入
  QEMU，未改变 progress gate、10 次硬上限或调用链判定。

## R13 已知 3 案例双轮回归（`79cce6d`）

归档索引：
`${HOME}/benchmark_assets/lumen-v0.2-8case-archive-20260808/e2e-regression/P0-P1-r13-r14-final/summary.json`。

| 案例 | 第 1 轮 | 第 2 轮 | 最佳 RCA evidence score | C/真实 QEMU/一致调用链 |
|---|---|---|---:|---|
| Smack `044fdf24e96093584232` | QEMU 3 次，均 `FAILED_SIGNAL_NOT_FOUND`，第 3 次 `BLOCKED_PROGRESS_GATE` | 合同缺 `source_dir/entry_source/fault_signatures` | 75 | C=是，QEMU=是，一致=否 |
| JFS `0a89a7b56db04c21a656` | 合同缺入口/故障签名 | QEMU 第 1 次未命中，增量重试合同阻断 | 74 | C=是，QEMU=是，一致=否 |
| bcachefs `56edda805363e0a093b8` | 合同缺 `fault_signatures` | 合同缺入口/故障签名 | 47 | C=是，QEMU=否，一致=否 |

## R14 其余 5 案例双轮回归

| 案例 | 第 1 轮 | 第 2 轮 | 最佳 RCA evidence score | C/真实 QEMU/一致调用链 |
|---|---|---|---:|---|
| J1939 `07bb74aeafc88ba7d5b4` | 合同缺入口/故障签名 | QEMU 后 `BLOCKED_GUEST_RUNTIME_INCOMPATIBLE`：guest 缺 `pthread_clone` | 49 | C=是，QEMU=是，一致=否 |
| NILFS `5c04210f7c7f897c1e7f` | QEMU 第 1 次未命中，重试合同阻断 | 合同缺 `fault_signatures` | 47 | C=是，QEMU=是，一致=否 |
| SCO `b825d87fe2d043e3e652` | 合同缺入口/故障签名 | 同类合同阻断 | 0 | C=是，QEMU=否，一致=否 |
| e24 `e24baf53dc389927a7c3` | 合同缺入口/故障签名 | QEMU 第 1 次未命中，重试合同阻断 | 0 | C=是，QEMU=是，一致=否 |
| Technisat `eaaaf38a95427be88f4b` | Validator 无法在 `${HOME}/linux-next` 解析声明 commit `9a33b369…` | 同样的 commit preflight 阻断 | — | C=否（本轮未进入 Kernel Expert），QEMU=否，一致=否 |

## 当前验收结论与差距

- 8/8 均完成双轮尝试或明确环境/合同终态；5/8 案例至少进入真实 QEMU，
  7/8 生成了用户态 C 源文件，0/8 观察到与原始日志一致的完整调用链。
- RCA evidence score 的最佳值为 Smack 75、JFS 74、bcachefs 47、J1939
  49、NILFS 47；这些是证据充分度，不等同于人工确认的根因准确率。当前
  根因准确率 80% 目标尚未达成，SCO/e24 以及多次合同阻断必须补齐合同和
  源码证据后再评估，不能把模型分数当作准确率。
- POC 100% 目标也尚未达成：Technisat 因精确 commit 不可解析没有生成本轮
  C contract；其历史归档中的 reproducer 不会被传给 Lumen。复现目标 20%
  亦未达成，本批一致调用链为 0/8。
- 所有每轮 `kernel_contract.json`/`KERNEL_CONTRACT.json`、C 源码、QEMU
  overlay、串口日志和 QEMU 日志均保留在上述 `e2e-regression` 目录；基础
  rootfs 和 `${HOME}/linux-next` 未删除。只清理已确认过期的临时
  worktree，不清理当前证据。
