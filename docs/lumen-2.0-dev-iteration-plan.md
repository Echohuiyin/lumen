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
| 2 | O-005 | Producer Frontier 与 Root Cause Gate | 待实现 | `producer_frontier`、逐项 gate、`diagnosis_status`、源码/log evidence；缺 producer 不得 `supported`，不改变独立 QEMU `test_passed`；回归门 R1 |
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

**当前验收**：83 个 P0/P1 专项测试（含 3 skipped）和静态检查通过；smack 的真实 R0 正在运行，若 Codex 在有界时间内不产出 contract，记录为 Kernel Expert timeout，不进入 Test Expert/QEMU，不伪造复现。

## 回归记录格式

每个大迭代在 benchmark archive 对应目录新增 `e2e-regression/<iteration>/summary.json`，至少包含：代码 commit、输入 hash、expected kernel commit、case、attempt、QEMU 状态、guest compile 状态、`test_passed`、`call_chain_consistent`、串口路径、contract 路径、blocked/failure code、相对基线差异。第一轮不复现时第二轮仍必须执行；两轮都未复现只报告未复现，不伪造成功。
