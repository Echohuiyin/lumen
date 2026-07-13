# 重构记录

## 目标

围绕内核维护工作流做一次收敛式重构，重点解决三类问题：

1. 每个专家的职责边界不清
2. 工具调用能力与提示词声明不一致
3. 专家之间的输入输出链路不稳定，依赖 LLM 的地方过多

## 本次重构计划

### 1. 梳理专家职责

- 明确 `validator`、`pm`、`tool_expert`、`kernel_expert`、`test_expert` 的职责范围
- 识别职责重叠、职责缺失、输出格式不统一的问题
- 让每个专家的输出可被下游稳定消费

### 2. 校验工具能力

- 检查每个专家是否声明了真实可用的工具能力
- 修正“提示词声明了工具，但运行时没有对应能力”的问题
- 对关键动作尽量改为工具化、脚本化，减少 LLM 直接做决定

### 3. 规范输入输出

- 统一专家输入字段，明确哪些信息是必须项，哪些是可选项
- 统一专家输出结构，保证上游输出能直接喂给下游
- 对关键产物引入结构化校验，避免靠自然语言猜测

### 4. 修正维护配置与文档

- 修正 `README.md` 中对 `config.json` 的说明
- 明确 `config.json` 的作用与主配置入口
- 修正 Crash / Lock prompt 描述与真实执行能力不一致的问题
- 修正 Kernel Expert 的虚假 bash 工具声明，补齐真实需要的工具能力

### 5. 强化 Kernel / Test 协作链路

- 让 Kernel Expert 的输出更适合 Test Expert 消费
- 让 Test Expert 能直接基于 Kernel Expert 的结构化输出做验证
- 补足复现、验证、归档之间的证据传递

### 6. 处理异常与容错

- 对配置缺失、路径错误、工具不可用等情况做显式失败
- 尽量在脚本层做确定性检查，而不是交给 LLM 兜底
- 让失败原因可定位、可回放、可修复

## 已完成

- 梳理了专家职责与调用链路
- 修正了部分 prompt / 配置 / 文档不一致问题
- 补齐了 Kernel Expert 的结构化校验逻辑
- 为 kernel contract 增加了更严格的工件校验与测试覆盖
- 已提交相关修改

## 已完成时间

- 2026-06-21 10:03:15 +08:00

## 未完成

- 继续逐个核对所有专家的真实工具能力与 prompt 声明是否完全一致
- 继续检查专家输入输出字段是否还有隐式依赖
- 继续检查 Kernel Expert 与 Test Expert 之间的契约是否还存在格式漂移
- 继续把能脚本化的动作下沉到脚本或规则层
- 继续评估 MCP 调用与直接调用 `crash_session` 的取舍，保留更稳的实现路径

## 备注

- 本文件记录的是当前阶段的重构计划快照，不代表后续不会调整
- 若后续新增修复，应在同一文件中追加时间戳和状态

## 后续开发任务

## 开发策略修订：保守推进与容错优先

本轮优化不能假设一次重构即可把所有专家链路稳定下来。当前系统仍包含 LLM 输出、外部工具、QEMU、vmcore、内核源码、busybox/initramfs 等多个不稳定因素，因此后续开发采用“先观测、再校验、最后强约束”的顺序。

### 基本原则

1. 不直接大改主流程
   - 先增加旁路检查、静态校验和结构化输出，不立即改变核心路由行为
   - 任何新 contract 校验先以 warning/degraded 方式运行，确认稳定后再升级为 blocked

2. 不把 LLM 输出当作可信事实源
   - LLM 可以做解释、摘要、策略建议
   - 路径、工具能力、kernel 类型、QEMU 参数、复现器产物必须由脚本或工具确认

3. 不追求“全自动成功”
   - 目标是失败可诊断、可回放、可继续推进
   - QEMU 不可用、kernel headers 缺失、vmcore 缺失、架构不匹配都应该是明确状态，不应该被包装成分析成功

4. 每一步必须能单独回滚
   - 能力清单、prompt 修正、contract 校验、工具 adapter、工作流阻断逻辑分开提交
   - 任一阶段发现误伤，可以只回退对应层，不影响已有主流程

5. 优先保证证据链完整
   - 每个工具动作至少记录输入、输出摘要、错误、产物路径
   - 知识库归档不得把未验证案例标记为已验证

### 分阶段落地方式

Phase 1：观测与静态一致性

- 建立专家能力清单
- 校验 prompt、配置、代码工具绑定是否一致
- 只检查事实一致性，不改变主流程行为
- 风险低，收益是能提前发现虚假工具声明和配置漂移

Phase 2：软 contract 校验

- 给专家输出增加结构化校验函数
- 初期只记录 warning/degraded，不强制阻断
- 收集现有测试和真实样例中的误报情况
- 风险中等，主要风险是旧输出格式不稳定导致误报

Phase 3：关键路径强约束

- 只对高风险动作强制 blocked：
  - ELF `vmlinux` 被当作 QEMU boot kernel
  - `test_script_path` 缺失
  - `target_arch` 不支持
  - crash 输入文件不存在
- 低风险字段继续保持 degraded，不影响归档
- 风险可控，收益是避免错误工具执行和误导性结果

Phase 4：工具 adapter 与降级策略

- crash、QEMU、build 等工具统一 adapter 输出 schema
- adapter 不追求隐藏所有差异，只保证调用方看到稳定字段
- 每个 adapter 必须定义 unavailable、timeout、bad_input、tool_error 四类失败

Phase 5：端到端回归

- 只覆盖最关键的成功/失败路径
- 不把真实 QEMU、真实 vmcore 作为普通单元测试的硬依赖
- 重型测试放到手动或环境具备时运行

### 风险与缓解

| 风险 | 影响 | 缓解方式 |
|------|------|----------|
| prompt 静态扫描误判 | 阻止合理文档描述 | 先使用少量明确禁止词，只拦截已确认的虚假工具声明 |
| contract 过早强制 | 旧样例无法跑通 | 分阶段从 warning 到 blocked，不一次性切换 |
| QEMU 环境不可用 | 测试专家大量失败 | 返回 `SKIPPED_QEMU_MISSING`，不伪造验证失败或成功 |
| crash session 不稳定 | Crash/Lock 分析中断 | session 复用、错误归一化、必要时降级为文本分析 |
| 复现器编译依赖宿主 headers | Kernel Expert 被环境阻塞 | 明确 blocked reason，保留已生成源码和编译日志 |
| 工具 adapter 抽象过度 | 增加复杂度但收益低 | 先统一输出 schema，不急于统一所有实现细节 |

### 效果判断标准

不是以“自动复现成功率立刻提升”为主要验收标准，而是以以下指标判断优化是否有效：

- 虚假工具声明能被静态检查发现
- 关键路径错误能在执行前被阻断
- blocked/degraded 的原因能直接定位到字段、路径或工具
- Test Expert 能完整复用 Kernel Expert 的 `kernel_contract`
- 知识库归档能区分已验证、未验证、环境跳过、工具失败

### 当前阶段边界

当前只推进 Phase 1：

- 增加专家能力清单
- 增加静态一致性检查脚本
- 修正已确认的 prompt 与运行时能力不一致
- 不改变主工作流路由
- 不新增强制 blocked 行为

### P0：建立专家能力清单与静态校验

目标：

- 让每个专家的职责、输入、输出、工具能力有一份可机器校验的清单
- 避免 prompt 声明能力和运行时真实能力不一致

开发任务：

1. 新增专家能力清单文件，例如 `agent_capabilities.json`
2. 为每个专家记录以下字段：
   - `role`
   - `required_inputs`
   - `optional_inputs`
   - `outputs`
   - `tools`
   - `downstream_consumers`
3. 新增静态检查脚本，例如 `dev/scripts/check_agent_contracts.py`
4. 检查 prompt 中声明的工具、配置中启用的工具、代码中绑定的工具是否一致
5. 将检查纳入现有测试或 smoke test 流程

验收标准：

- 运行检查脚本能列出所有专家的职责和工具能力
- prompt 声明不存在无法调用的工具
- 代码中新增或移除工具时，检查脚本能发现清单未同步

### P0：统一专家输入输出 Contract

目标：

- 明确专家之间传递的数据结构
- 降低自然语言输出导致的格式漂移

开发任务：

1. 梳理 `graph/rn_state.py` 中当前状态字段
2. 为核心专家输出定义稳定 contract：
   - `validator_contract`
   - `pm_contract`
   - `tool_analysis_contract`
   - `kernel_contract`
   - `test_contract`
   - `knowledge_contract`
3. 为每个 contract 增加校验函数
4. 在每个节点输出后立即校验，不合格则进入显式 blocked 状态
5. 为 contract 增加单元测试

验收标准：

- 每个专家输出都有结构化 contract
- 下游专家只依赖 contract 字段，不依赖自由文本解析
- 缺字段、字段类型错误、路径不存在时能返回明确 blocked reason

### P0：强化 Kernel Expert 到 Test Expert 的契约

目标：

- 保证 Kernel Expert 产物可以直接被 Test Expert 执行
- 避免 Test Expert 重新解释自然语言测试方案

开发任务：

1. 扩展 `kernel_contract` 字段：
   - `target_arch`
   - `kernel_source_path`
   - `boot_kernel_path`
   - `test_script_path`
   - `reproducer_dir`
   - `reproducer_module_path`
   - `expected_failure_signal`
   - `qemu_command_hint`
   - `evidence`
2. 明确 `boot_kernel_path` 与 `vmlinux_path` 的不同用途
3. Test Expert 只接受已通过校验的 `kernel_contract`
4. Test Expert 执行前做二次路径和类型校验
5. 增加缺失脚本、错误 kernel 类型、架构不匹配等负向测试

验收标准：

- `vmlinux` 不会被误当作 QEMU boot kernel
- `test_script_path` 缺失时不会进入 QEMU 执行
- Test Expert 的输入可以从 `kernel_contract` 完整重放

### P1：工具调用路径工具化和脚本化

目标：

- 把确定性动作从 LLM 决策中移出
- 每个关键动作都有可重复执行的脚本入口

开发任务：

1. 梳理当前依赖 LLM 判断的动作：
   - crash 命令选择
   - lock 分析命令选择
   - kernel artifact 检查
   - QEMU 启动参数拼装
   - 复现器编译检查
2. 对确定性动作增加脚本或工具函数
3. LLM 只负责选择策略和解释结果，不直接拼接关键命令
4. 所有工具调用返回统一 evidence 结构
5. 对工具失败增加重试、跳过或 blocked 策略

验收标准：

- crash / lock / qemu / build 的关键动作都有确定性入口
- 工具输出包含命令、退出码、stdout/stderr 摘要、产物路径
- LLM 输出缺失时，系统仍能给出可诊断失败信息

### P1：评估 MCP 与直接 `crash_session` 调用的边界

目标：

- 明确哪些场景使用 MCP，哪些场景直接调用本地 `crash_session`
- 在效果、性能和容错之间选择稳定路径

开发任务：

1. 列出 MCP 调用链路和直接 `crash_session` 调用链路
2. 对比以下指标：
   - 初始化成本
   - 单次命令延迟
   - session 复用能力
   - 错误可观测性
   - 日志完整性
   - 部署依赖
3. 增加一个统一 crash adapter
4. adapter 根据配置选择 MCP 或直接调用
5. 为两种路径使用同一组输出 schema

验收标准：

- 调用方不直接依赖 MCP 或 `crash_session` 的实现细节
- 两种路径输出字段一致
- 任一路径不可用时能给出明确降级或 blocked 信息

### P1：Prompt 与代码能力一致性治理

目标：

- prompt 不再声明不存在的工具
- prompt 只描述职责和输入输出，不承担运行时能力注册

开发任务：

1. 检查 `prompts/maintenance/*.md`
2. 删除虚假工具声明
3. 将工具能力引用改为来自能力清单
4. 保留 prompt 中必要的输出格式要求
5. 增加 prompt 静态扫描测试

验收标准：

- prompt 中出现的工具名都能在能力清单或代码绑定中找到
- Kernel Expert 的 bash 能力不再只停留在提示词声明
- Crash / Lock prompt 与真实 crash 工具链描述一致

### P2：知识库归档结构化

目标：

- 让知识库归档消费各专家 contract，而不是人工总结式文本

开发任务：

1. 定义 `knowledge_contract`
2. 从 `validator_contract`、`pm_contract`、`tool_analysis_contract`、`kernel_contract`、`test_contract` 汇总归档字段
3. 增加归档前校验
4. 缺失关键证据时标记归档质量等级
5. 为知识库生成增加最小测试

验收标准：

- 知识库文档能追溯每条关键结论的来源 expert
- 归档中包含复现器、验证结果、工具日志路径
- 未完成验证的案例不会被标记为已验证

### P2：端到端回归用例

目标：

- 用最少但覆盖关键链路的样例防止重构回退

开发任务：

1. 增加一个无 vmcore 的输入校验失败用例
2. 增加一个 crash 分析 blocked 用例
3. 增加一个 kernel contract 成功但 test contract 失败用例
4. 增加一个完整 smoke workflow 用例
5. 将所有回归用例纳入本地验证命令

验收标准：

- 本地一条命令能跑完核心回归
- 每个 blocked 分支都有明确原因
- 输出 contract 可以作为失败现场保存

## 推荐执行顺序

1. P0：建立专家能力清单与静态校验
2. P0：统一专家输入输出 Contract
3. P0：强化 Kernel Expert 到 Test Expert 的契约
4. P1：工具调用路径工具化和脚本化
5. P1：评估 MCP 与直接 `crash_session` 调用的边界
6. P1：Prompt 与代码能力一致性治理
7. P2：知识库归档结构化
8. P2：端到端回归用例

## 下一步建议

优先开发 P0 的能力清单和 contract 校验。原因是后续所有工具化、prompt 修正、MCP/crash_session 适配都依赖这份事实来源；如果先继续改 prompt 或工具实现，容易再次出现声明和运行时能力不一致。

## 2026-06-21 后续审计记录

### 审计目标

本轮继续围绕主维护工作流做事实核对，不直接修改主流程行为，重点记录以下问题：

1. 每个专家是否具备 prompt 和能力清单中声明的工具能力
2. 每个专家的输入输出是否已经结构化、可校验、可被下游稳定消费
3. 当前工具调用链路是否仍存在由 LLM 临时拼接命令、临时选择工具、输出不可复现的问题
4. 后续优化是否可以继续把确定性动作下沉为脚本、工具函数或 adapter，只在需要归纳、解释、策略选择时调用 LLM

### 当前仓库状态观察

- 主工作流已经具备 `validator -> pm -> tool_expert -> kernel_expert -> test_expert -> knowledge_base` 的 LangGraph 状态机结构。
- `ValidationResultContract`、`ToolExpertOutput`、`KernelExpertOutput`、`TestPlan`、`TestResultContract` 已存在，但还不是所有节点都有同等强度的结构化 contract。
- `Test Expert` 的 QEMU 验证已经基本转为 `run_qemu_test_plan()` 确定性执行路径，LLM 只保留在失败后的建议生成环节。
- `Crash/Lock` 专家已经有确定性 baseline 采集，但额外 crash 命令选择和最终解释仍依赖 LLM。
- `Kernel Expert` 已有文件写入、编译、搜索、受控 bash 工具，但复现器内容设计、路径选择、contract 填写仍主要依赖 LLM。
- `Knowledge Base` 归档仍主要由 LLM 汇总文本生成，尚未定义独立 `knowledge_contract`。
- 当前 `.venv` 可运行项目脚本，但没有安装 `pytest`，因此完整测试套件无法直接用 `.venv/bin/python -m pytest` 执行。

### 静态能力检查结果

执行命令：

```bash
.venv/bin/python dev/scripts/check_agent_contracts.py
```

当前检查失败，说明能力清单、prompt 和运行时工具绑定仍存在不一致。已确认问题如下：

- `lock_analysis` prompt 仍包含过期工具声明：
  - `/kernel-build`
  - `/qemu-test`
- `kernel_log_analysis` prompt 仍包含不准确的工具声明：
  - `已绑定 crash 工具`
  - `run_crash_command:`
- `kernel_expert` prompt 仍包含过期 skill 声明：
  - `/kernel-testcase-generator`
  - `kernel-testcase-generator skill`
- `kernel_expert` prompt 缺少当前真实运行时工具术语：
  - `write_file`
  - `compile_module`
  - `search_files`
- `test_expert` prompt 仍包含过期工具声明：
  - `Bash 工具`
  - `Read 工具`
  - `Write 工具`
  - `/kernel-build`
  - `/qemu-test`
  - `实际调用 Bash 工具`
- `test_expert` prompt 缺少当前确定性 runner 术语：
  - `run_qemu_test_plan`

这些问题会导致模型误以为自己可以调用不存在的 skill 或通用工具，也会让静态能力清单无法作为可信事实源。

### 专家逐项核对

#### Validator

当前职责：

- 判断用户输入是否包含足够的内核维护上下文。
- 优先用规则识别 `vmcore`、`vmlinux`、`boot_kernel`、panic、oops、deadlock、hung task、log、reproducer 等信号。
- 规则无法判断时才回退 LLM。

真实工具能力：

- 无外部工具。
- 运行时依赖规则函数 `_validate_input_by_rules()` 和可选 LLM。

输入输出状态：

- 输入：`user_input`、`config_path`。
- 输出：`validation_passed`、`validation_feedback`、`validation_contract`、`config`。
- 已有 `ValidationResultContract`。

问题：

- 规则校验覆盖常见内核信号，但缺少对关键路径存在性、路径类型、架构字段的早期结构化提取。
- `validation_contract` 还没有承载标准化后的 `vmcore_path`、`vmlinux_path`、`boot_kernel_path`、`target_arch`。

优化方向：

- 增加确定性输入解析器，输出 `input_artifacts_contract`，把路径、架构、日志片段、复现器信息提前结构化。
- Validator 不判断根因，只做字段提取、完整性校验和明显错误阻断。

#### PM

当前职责：

- 根据输入选择工具专家。
- 创建 issue 目前是 stub。
- 优先规则分流，规则无法覆盖时回退 LLM。

真实工具能力：

- 无真实外部工具。
- `_create_issue_stub()` 只是占位，不应被视为真实 issue 工具。

输入输出状态：

- 输入：`user_input`、`config`。
- 输出：`required_experts`、`pm_routing_reason`、`issue_id`、`issue_url`。
- 尚未定义 `pm_contract`。

问题：

- `issue_id`/`issue_url` 是打桩数据，知识库归档和最终响应可能把它误当真实系统产物。
- PM 输出缺少结构化路由证据，例如命中的规则、跳过的专家、配置中不可用的专家。

优化方向：

- 定义 `PMRoutingContract`，包含 `selected_experts`、`routing_rules_hit`、`skipped_experts`、`issue_status`。
- 将 issue 创建抽象为独立工具；未配置真实 issue 后端时输出 `issue_status=stubbed`，避免误导。

#### Knowledge Search Expert

当前职责：

- 调用 RAG 检索历史案例，并由 LLM 总结相似经验。

真实工具能力：

- `get_rag_context_for_query()`。
- 能力清单中声明为 `rag_case_retrieval`。

输入输出状态：

- 输入：`user_input`、`expert_type`、`config`。
- 输出：`expert_results`，内部有 `ToolExpertOutput`。

问题：

- RAG 检索返回内容被拼入 LLM prompt，检索命中本身的结构化元数据不足。
- 缺少检索工具的统一 step 记录，例如 query、top_k、命中文档 id、score、错误。

优化方向：

- 将 RAG 检索封装为 `ToolStepResult` 或 `RetrievalResultContract`。
- LLM 只负责总结已检索出的案例，不负责声称是否命中。

#### Crash Analysis Expert

当前职责：

- 在存在 `vmcore` 和 `vmlinux` 时创建 crash session。
- 确定性采集 `sys`、`ps`、`bt -a`、`log | tail -n 200`。
- 由 LLM 基于证据生成 crash 分析报告。

真实工具能力：

- `collect_baseline`
- `run_crash_command`
- `run_crash_commands`
- `get_command_history`
- 额外有代码内置的 deterministic evidence collector。

输入输出状态：

- 输入：`user_input`、`expert_type`、`config`，路径从文本中正则提取。
- 输出：`expert_results`，包含 `ToolExpertOutput` 的 `evidence`、`artifacts`、`errors`。

问题：

- 路径提取仍在 `tool_expert` 内部局部实现，未复用 Validator 的结构化输入。
- baseline 采集已确定性执行，但后续额外命令仍由 LLM 决定，存在重复、无效 crash 命令和不稳定输出风险。
- crash 命令输出是文本，部分 evidence 解析规则有限。
- crash session 直接调用与 MCP 调用边界还没有统一 adapter。

优化方向：

- 增加 `CrashAnalysisPlan`，用脚本根据故障类型生成固定命令集合。
- 先执行固定命令集合，再允许 LLM 提出补充命令，但补充命令必须经过 allowlist 和去重。
- 定义 `CrashToolAdapter`，输出统一的 `ToolStepResult` 列表，屏蔽直接 `crash_session` 和 MCP 差异。
- 对 crash 命令做结构化解析优先，LLM 只解释已解析证据。

#### Lock Analysis Expert

当前职责：

- 复用 crash session 分析 hung task、deadlock、mutex/rwsem 等锁问题。
- baseline 命令比 crash 多 `waitq`、`foreach bt`。

真实工具能力：

- 与 Crash Analysis 相同：
  - `collect_baseline`
  - `run_crash_command`
  - `run_crash_commands`
  - `get_command_history`

输入输出状态：

- 输入输出同 `Crash Analysis Expert`。

问题：

- prompt 仍声明 `/kernel-build`、`/qemu-test`，与运行时能力不一致。
- 锁链路推导中，mutex owner decode、waiter 列表、D-state task 关联仍大量依赖 LLM 解释。
- 缺少针对 mutex/rwsem/spinlock 的确定性解析脚本。

优化方向：

- 增加 `lock_analyzer.py` adapter，在 crash 输出中结构化提取 task、backtrace、lock address、owner、waiter。
- LLM 不直接推导锁链，只对 adapter 给出的锁链候选做解释和风险判断。
- lock prompt 删除所有 build/qemu 描述，只保留 crash-session 证据解释职责。

#### Kernel Log Analysis Expert

当前职责：

- 有 vmcore 时用 crash session 执行 `log` 提取内核日志。
- 无 vmcore 时基于用户输入文本做日志分析。

真实工具能力：

- 代码实际能力更接近 `extract_crash_log`。
- 不是通用 crash command 工具专家。

输入输出状态：

- 输入：`user_input`、可选 `vmcore/vmlinux`。
- 输出：`expert_results`。

问题：

- prompt 仍提到已绑定 crash 工具和 `run_crash_command`，与代码路径不一致。
- 日志分析结果主要由 LLM 生成，确定性日志事件抽取只作为辅助 evidence。
- 日志来源、时间线、关键事件类型还没有形成独立 contract。

优化方向：

- 定义 `KernelLogContract`，包含 `source`、`events`、`timeline`、`error_patterns`、`raw_log_artifact`。
- 将 `_parse_log_evidence()` 升级为独立工具函数或脚本，并作为日志专家的主要输出。
- LLM 只负责把结构化事件串成分析结论。

#### Kernel Expert

当前职责：

- 综合工具专家结果，生成复现器源码、Makefile、测试脚本、构建结果和 `kernel_contract`。
- 已绑定文件操作、搜索、编译和受控 bash 工具。

真实工具能力：

- `create_directory`
- `write_file`
- `read_file`
- `compile_module`
- `check_file_exists`
- `list_directory`
- `search_files`
- `bash`

输入输出状态：

- 输入：`user_input`、`expert_results`、可选 `test_result`。
- 输出：`kernel_analysis`、`reproduce_case`、`kernel_diagnosis`、`kernel_ready_for_test`、`kernel_contract`、若干扁平字段。
- 已有 `KernelExpertOutput` 和 artifact 校验。

问题：

- prompt 仍包含 `/kernel-testcase-generator` 等过期 skill 声明。
- prompt 未明确列出真实绑定的 `write_file`、`compile_module`、`search_files` 等工具。
- 如果宿主缺少 kernel headers，当前直接 blocked，可能阻断“仅生成源码但跳过编译”的场景。
- 复现器生成仍高度依赖 LLM，缺少按故障类型组织的模板化生成器。
- `kernel_contract` 字段与 `spec.md` 中早期规划字段略有漂移，例如 `expected_failure_signal` 和代码中的 `expected_signal`。

优化方向：

- 修正 prompt，使其只引用真实工具。
- 引入 `ReproducerBuilder` 脚本层：
  - 根据 fault type 选择模板
  - 写入源码、Makefile、test.sh
  - 调用 `compile_module`
  - 生成 `KernelExpertOutput`
- LLM 只负责选择复现策略和填充故障相关参数，不直接自由组织所有文件。
- 对 headers 缺失改为 `build_status=skipped`，保留源码产物；只有缺少交接必需字段时才 `blocked`。

#### Test Expert

当前职责：

- 根据 Kernel Expert 的交接字段执行 QEMU 验证。
- 主要使用 `run_qemu_test_plan()` 确定性 runner。
- 失败达到最大次数后，必要时用 LLM 生成改进建议。

真实工具能力：

- 通过 `qemu_tools.py` 提供：
  - `check_qemu_available`
  - `create_initramfs`
  - `boot_kernel`
  - `analyze_boot_log`
- 通过 `test_runner.py` 提供确定性 orchestration。

输入输出状态：

- 输入：`target_arch`、`boot_kernel_path`、`reproducer_dir`、`reproducer_module_path`、`test_script_path`、`expected_signal`。
- 输出：`test_result`、`test_passed`、`test_attempts`、`test_contract`。
- 已有 `TestPlan` 和 `TestResultContract`。

问题：

- prompt 仍声明 Bash/Read/Write 和 `/kernel-build`、`/qemu-test`，与当前确定性 runner 不一致。
- `test_expert_node()` 从 state 的扁平字段构造 `TestPlan`，没有直接从 `kernel_contract` parse/validate。
- `expected_signal` 使用简单字符串匹配，后续需要支持正则、多个信号、负向信号和超时型故障。

优化方向：

- prompt 明确 Test Expert 不负责构造命令，只解释 `run_qemu_test_plan()` 结果。
- Test Expert 入口只接受已验证的 `KernelExpertOutput`，从 contract 构造 `TestPlan`。
- 扩展 `expected_signal` 为 `expected_signals` 列表，支持 `type=contains|regex|absence|timeout`。

#### Knowledge Base

当前职责：

- 汇总各专家文本和结构化 contract，由 LLM 生成知识库 Markdown。
- 调用 `rag-case-retrieval` skill 的 `import_cases.py` 导入 Chroma。

真实工具能力：

- `rag_case_import`，具体是本地脚本调用。

输入输出状态：

- 输入：`user_input`、`expert_results`、`kernel_contract`、`test_contract`、若干文本分析。
- 输出：`knowledge_file`、`final_response`。
- 尚未定义 `knowledge_contract`。

问题：

- 归档内容由 LLM 生成，未强制区分 `verified`、`not_reproduced`、`blocked`、`skipped`。
- Chroma 导入失败不会影响主流程状态，只在 final response 中显示。
- 归档 JSON 临时文件和导入状态未进入结构化 contract。

优化方向：

- 定义 `KnowledgeContract`，归档前由脚本汇总事实字段。
- Markdown 生成可以由模板完成，LLM 只负责生成摘要段落。
- Chroma 导入结果作为 `ToolStepResult` 写入归档状态。

### 当前工具调用问题清单

1. prompt 与真实工具能力仍不一致，静态检查当前失败。
2. `config.json` 中仍有 legacy `agents.tool_expert` 结构，虽然加载器会 normalize，但配置文件本身不直观。
3. `pyproject.toml` 项目名和描述仍是旧项目元数据，不利于后续维护和部署识别。
4. `pytest` 未安装在当前 `.venv`，本地回归命令不可用。
5. Crash/Lock 的额外命令选择仍由 LLM 决定，缺少固定 plan 和命令 allowlist/denylist 分层。
6. Kernel Expert 的复现器生成仍由 LLM 主导，缺少按故障类型的模板和脚本化 builder。
7. Knowledge Base 仍以 LLM 总结为主，缺少归档质量状态和事实字段模板。
8. 部分工具函数返回文本，再由 wrapper 反向解析文本中的路径或状态，应该逐步改为先返回结构化对象，再格式化为文本。
9. 工具输出的 `stdout/stderr/returncode/duration/artifacts` 字段尚未在所有工具中统一。
10. 当前 contract 和 state 同时保留结构化字段与扁平字段，存在字段漂移风险。

### 后续优化总原则

1. 能由规则判断的，不交给 LLM。
2. 能由脚本执行的，不让 LLM 拼命令。
3. 能由 contract 表达的，不让下游解析自然语言。
4. LLM 只保留三类职责：
   - 多证据归纳和解释
   - 复现策略选择
   - 面向人的总结和建议
5. 所有外部动作必须经过工具 adapter，统一输出：
   - `name`
   - `status`
   - `code`
   - `inputs`
   - `stdout`
   - `stderr`
   - `returncode`
   - `duration_ms`
   - `artifacts`
   - `error`

### 下一轮推荐实施计划

#### P0：修复能力一致性检查

任务：

1. 修正 `prompts/maintenance/*.md` 中的过期工具声明。
2. 让 prompt 中的工具名和 `agent_capabilities.json`、运行时 `create_*_tools()` 完全一致。
3. 更新 `config.json`，优先使用顶层 `tool_experts` 结构，减少 legacy normalize 依赖。
4. 修正 `pyproject.toml` 的项目名和描述。
5. 安装或声明测试依赖，使 `.venv/bin/python -m pytest` 可用。

验收：

- `.venv/bin/python dev/scripts/check_agent_contracts.py` 通过。
- 本地测试命令可执行。

#### P0：统一输入 artifact 解析

任务：

1. 新增确定性输入解析模块，例如 `agents/input_artifacts.py`。
2. 从 `user_input` 解析：
   - `vmcore_path`
   - `vmlinux_path`
   - `boot_kernel_path`
   - `target_arch`
   - `kernel_source_path`
   - `log_excerpt`
   - `reproducer_path`
3. Validator 输出 `input_artifacts_contract`。
4. PM、Tool Expert、Kernel Expert、Test Expert 不再各自正则提取路径。

验收：

- 同一输入在所有专家中看到同一份 artifact contract。
- 路径不存在、ELF vmlinux 误作 boot kernel、架构缺失能早期定位。

#### P1：Crash/Lock 确定性分析 plan

任务：

1. 定义 `CrashAnalysisPlan` 和 `LockAnalysisPlan`。
2. 根据故障类型生成固定 crash 命令集合。
3. 固定 plan 先执行，LLM 只能请求补充命令。
4. 补充命令必须通过 allowlist、去重和超时限制。
5. 将 `session.run_command()` 结果统一包装成 `ToolStepResult`。

验收：

- 没有 LLM 时仍可得到 baseline crash/lock 报告。
- LLM 额外命令不会破坏 session 或执行明显无效 shell 语法。

#### P1：Kernel Expert 复现器脚本化

任务：

1. 新增 `ReproducerBuilder`。
2. 按故障类型维护最小模板：
   - nullptr
   - deadlock
   - softlockup
   - panic
   - stack_overflow
3. builder 负责写文件、生成 Makefile、生成 test.sh、编译、产出 `KernelExpertOutput`。
4. LLM 只选择模板和关键参数，不直接决定所有文件路径和构建步骤。

验收：

- 缺少 LLM 或 LLM 输出不完整时，仍能给出 blocked/degraded 的 builder 结果。
- `kernel_contract` 始终来自 builder 校验结果，而不是纯文本解析。

#### P1：Test Expert 只消费 contract

任务：

1. `test_expert_node()` 从 `kernel_contract` 构造 `TestPlan`，不再依赖 state 扁平字段。
2. 扁平字段只作为兼容输出，不能作为事实源。
3. 扩展 expected signal schema。
4. QEMU runner 输出完整 step metadata。

验收：

- `TestPlan` 可以从 `kernel_contract` 完整重放。
- 所有 test blocked/skipped/failed 都有稳定 `code`。

#### P2：知识库模板化归档

任务：

1. 定义 `KnowledgeContract`。
2. 使用模板生成 Markdown 主体，LLM 只生成摘要和经验教训段落。
3. 归档明确标记：
   - `verified`
   - `not_reproduced`
   - `blocked`
   - `skipped_environment`
   - `tool_failed`
4. Chroma 导入结果进入 contract。

验收：

- 未验证案例不会被写成已验证。
- 每条关键结论可追溯到 expert、tool step 或 artifact。

### 更新后的优先级

1. 先让静态能力检查通过，消除 prompt/工具/能力清单不一致。
2. 再统一输入 artifact contract，解决各专家重复正则解析路径的问题。
3. 然后将 Crash/Lock 的计划执行和 Kernel 复现器生成进一步脚本化。
4. 最后收敛 Knowledge Base，让归档从事实 contract 生成，而不是完全依赖 LLM 总结。

## 2026-06-21 P0 执行记录

### 已完成

1. 修复 prompt 与运行时工具能力不一致问题：
   - `kernel_expert` 改为声明真实绑定工具：`create_directory`、`write_file`、`read_file`、`compile_module`、`check_file_exists`、`list_directory`、`search_files`、`bash`
   - `test_expert` 改为当前确定性 runner 模型：`run_qemu_test_plan`、`check_qemu_available`、`create_initramfs`、`boot_kernel`、`analyze_boot_log`
   - `lock_analysis` 删除旧的 `/kernel-build`、`/qemu-test` 表述
   - `kernel_log_analysis` 改为 `extract_crash_log` 能力描述，不再声明通用 crash 命令工具
2. 将 `config.json` 从 legacy `agents.tool_expert` 结构迁移到顶层 `tool_experts`。
3. 修正 `pyproject.toml` 项目元数据：
   - `name = "lumen"`
   - 描述改为 Linux kernel maintenance analysis 工作流
4. 修正 dev 依赖声明：
   - `requirements-dev.txt` 加入 `pytest>=8.0.0`
   - `pyproject.toml` 的 dev extra 加入 `pytest>=8.0.0`
5. 修正 `dev/tests/test_pm_rules.py` 中依赖全局 `config.json` 形状的旧夹具，让 only-crash 路由测试自包含。

### 验证结果

已通过：

```bash
.venv/bin/python dev/scripts/check_agent_contracts.py
.venv/bin/python dev/tests/test_pm_rules.py
.venv/bin/python dev/tests/test_kernel_contract.py
.venv/bin/python dev/tests/test_test_runner_contract.py
.venv/bin/python dev/tests/test_validator_rules.py
.venv/bin/python dev/tests/test_tool_evidence.py
.venv/bin/python dev/tests/test_qemu_tools.py
```

直接运行 `dev/tests/test_expert_io_format.py` 时，依赖真实 LLM 的专家测试被当前无效 API key 阻断，错误为 OpenAI-compatible backend 返回 401。该失败不是本轮 prompt/contract 静态一致性修复引入的功能回归，但说明后续测试需要区分离线 contract 测试和在线 LLM 集成测试。

### 新增后续任务

1. 给 LLM 依赖测试增加 mock backend 或跳过条件，避免本地无 API key 时把集成测试误判为代码回归。
2. 继续推进 `input_artifacts_contract`，统一 `vmcore`、`vmlinux`、`boot_kernel`、`target_arch` 的解析来源。
3. 将 QEMU、crash、kernel build 工具逐步改成先产出结构化对象，再格式化为人类可读文本，减少从文本反向解析状态。

## 2026-06-21 P0 输入 Artifact Contract 执行记录

### 已完成

1. 新增 `InputArtifactsContract`，用于承载用户输入中确定性解析出的：
   - `vmcore_path`
   - `vmlinux_path`
   - `boot_kernel_path`
   - `target_arch`
   - `kernel_source_path`
   - `reproducer_path`
   - `log_excerpt`
   - `evidence`
   - `warnings`
2. 新增 `agents/input_artifacts.py`，作为统一解析入口。
3. `validator_node` 现在在所有返回路径中输出 `input_artifacts_contract`。
4. `MaintenanceWorkflowState` 增加 `input_artifacts_contract` 字段。
5. PM fan-out 会把 `input_artifacts_contract` 传给工具专家。
6. `tool_expert` 中 crash/log 路径提取优先使用 `input_artifacts_contract`，旧 `_extract_vmcore_paths()` 保留为 fallback。
7. `test_expert` 优先从 `kernel_contract` 构造测试计划，其次使用 `input_artifacts_contract`，最后才回退旧文本解析函数。
8. `agent_capabilities.json` 已同步新增输入/输出字段。
9. 增加 `parse_input_artifacts` 规则测试。

### 验证结果

已通过：

```bash
.venv/bin/python dev/scripts/check_agent_contracts.py
.venv/bin/python dev/tests/test_validator_rules.py
.venv/bin/python dev/tests/test_pm_rules.py
.venv/bin/python dev/tests/test_kernel_contract.py
.venv/bin/python dev/tests/test_test_runner_contract.py
.venv/bin/python dev/tests/test_tool_evidence.py
.venv/bin/python dev/tests/test_qemu_tools.py
```

### 剩余问题

1. `tool_expert` 和 `test_expert` 中旧解析函数仍为兼容 fallback，后续可以在更多调用方迁移完成后逐步降级为测试辅助或删除。
2. `input_artifacts_contract` 当前只做文本解析，不做文件存在性和文件类型校验；后续应加入 artifact validator。
3. `boot_kernel_path` 和 `vmlinux_path` 的类型校验仍主要在 Kernel/Test contract 阶段完成，后续可前移到输入解析后的软校验。

## 2026-06-21 输入 Artifact 校验执行记录

### 已完成

1. `InputArtifactsContract` 增加 `errors` 字段。
2. `parse_input_artifacts()` 增加软校验能力：
   - 检查已解析路径是否存在
   - 检查文件/目录类型是否符合字段语义
   - 对 `vmlinux_path` 和 `boot_kernel_path` 进行 kernel 类型识别
   - 当 `boot_kernel_path` 指向 ELF `vmlinux` 时降级并记录错误
   - 当路径不存在时记录 warning 和 artifact check evidence
3. `parse_input_artifacts(validate_paths=False)` 可用于只测试解析行为或保留兼容场景。
4. 增加输入 artifact 校验测试：
   - 路径和架构解析
   - vmlinux ELF 类型识别
   - boot kernel bzImage 类型识别
   - boot kernel 误传 ELF 时降级

### 验证结果

```bash
.venv/bin/python dev/tests/test_validator_rules.py
.venv/bin/python -m pytest dev/tests/test_validator_rules.py -q
.venv/bin/python -m pytest -q
.venv/bin/python dev/scripts/check_agent_contracts.py
.venv/bin/python -m pytest -q --run-online
```

结果：

```text
validator_rules OK
8 passed
46 passed, 18 skipped, 1 warning
agent contract check passed
64 passed, 1 warning
```

### 剩余问题

1. 目前输入 artifact 校验是软校验，只记录 `warnings/errors` 并把状态降为 `degraded`，不会直接阻断工作流。
2. `tool_expert` 和 `test_expert` 中旧解析 fallback 仍存在，后续可以在更多调用方迁移完成后逐步删除。
3. `kernel_source_path` 只校验目录存在性，尚未识别是否为实际 Linux source tree。

## 2026-06-21 Kernel Source Artifact 校验执行记录

### 已完成

1. `kernel_source_path` 不再只检查目录存在性。
2. 输入 artifact 校验会检查典型 Linux source tree 标志：
   - `Makefile`
   - `Kconfig`
   - `include/linux/kernel.h`
   - `init/main.c`
3. 如果目录存在但不像 Linux source tree，记录 warning 并将 `input_artifacts_contract.status` 降级为 `degraded`。
4. artifact evidence 中会记录：
   - `linux_source_markers`
   - `missing_linux_source_markers`
   - `is_linux_source_tree`
5. 为 DeepSeek Anthropic-compatible backend 将临时网络错误重试次数从 3 次提高到 5 次，降低在线测试受 TLS/transport 瞬时错误影响的概率。

### 验证结果

```bash
.venv/bin/python dev/tests/test_validator_rules.py
.venv/bin/python -m pytest dev/tests/test_validator_rules.py -q
.venv/bin/python -m pytest -q
.venv/bin/python dev/scripts/check_agent_contracts.py
.venv/bin/python -m pytest -q --run-online
```

结果：

```text
validator_rules OK
10 passed
48 passed, 18 skipped, 1 warning
agent contract check passed
66 passed, 1 warning
```

### 剩余问题

1. Linux source tree 识别目前是启发式软校验，不能替代后续 build adapter 的确定性构建检查。
2. 在线测试仍依赖 DeepSeek endpoint 稳定性；当前已通过重试降低瞬时连接错误影响。

## 2026-06-21 在线 LLM 与完整测试执行记录

### 已完成

1. 使用 `ensurepip` 为当前 `.venv` 补齐 `pip`。
2. 通过 `requirements-dev.txt` 安装 `pytest`。
3. 检查 `~/.claude/settings.json_ds`，确认其提供 DeepSeek 的 Anthropic-compatible endpoint：
   - base URL: `https://api.deepseek.com/anthropic`
   - model: `deepseek-v4-flash`
4. 新增 `AnthropicBackend`，直接调用 Anthropic-compatible Messages API。
5. `config.py` 支持：
   - `backend = "anthropic"`
   - `default.settings_file`
   - 从 `ANTHROPIC_DEFAULT_OPUS_MODEL` / `SONNET` / `HAIKU` 回填模型名
6. `config.json` 默认改为使用 `settings_file = "~/.claude/settings.json_ds"`，URL 和 key 从该文件回填。
7. 为 `AnthropicBackend` 增加临时网络错误重试，覆盖 TLS/transport error、timeout、429/5xx/529 等场景。

### 验证结果

LLM smoke test 已通过：

```bash
.venv/bin/python - <<'PY'
from langchain_core.messages import HumanMessage, SystemMessage
from config import load_config, get_llm_with_config
cfg=load_config('config.json')
llm=get_llm_with_config({}, default_config=cfg['default'], agent_name='validator')
resp=llm.invoke([SystemMessage(content='Reply with exactly: ok'), HumanMessage(content='test')])
print(resp.content.strip()[:200])
PY
```

完整测试已通过：

```bash
.venv/bin/python -m pytest -q
```

结果：

```text
62 passed, 1 warning in 132.44s
```

唯一 warning 是 pytest 尝试收集 Pydantic 模型 `TestPlan`，不影响测试结果。

### 后续建议

1. 可以保留当前 Anthropic-compatible backend；如果后续希望减少自定义 backend，也可以把 DeepSeek URL 切换到 OpenAI-compatible `/v1` 并继续使用 `ChatOpenAI`。
2. 把在线 LLM 测试和离线 contract 测试分组，避免网络波动影响本地快速回归。
3. 对在线 LLM 测试设置更小的样本集或 marker，例如 `pytest -m online`。

## 2026-06-21 测试分组执行记录

### 已完成

1. 增加 pytest `online` marker。
2. 增加 `--run-online` 选项，默认跳过在线 LLM / 外部 crash session 测试。
3. 将以下测试归为在线测试：
   - `dev/tests/test_expert_io_format.py`
   - `dev/tests/test_kernel_expert.py::test_kernel_expert_tool_calling`
   - `dev/tests/test_test_expert.py::test_qemu_tool_calling`
   - `dev/tests/test_tool_expert_mcp.py::test_tool_calling_loop`
   - `dev/tests/test_tool_experts.py::test_expert_direct`
   - `dev/tests/test_tool_experts.py::test_all_experts`
4. 将测试生成目录加入 `.gitignore`：
   - `knowledge_base/`
   - `self_test_reports/`

### 验证结果

默认离线测试：

```bash
.venv/bin/python -m pytest -q
```

结果：

```text
44 passed, 18 skipped, 1 warning in 1.81s
```

完整在线测试：

```bash
.venv/bin/python -m pytest -q --run-online
```

结果：

```text
62 passed, 1 warning in 111.86s
```

### 影响

本地快速回归不再依赖在线 LLM 服务或外部 crash session。需要验证完整在线链路时显式加 `--run-online`。

---

# UAF / 引用计数形式化分析与复现设计规范

## 1. 背景与目标

对于 UAF、`kref`、`refcount` 和引用计数增减不平衡问题，用户需要的不只是一个复现器，而是一条可以持续排查的完整证据链：

1. 在明确边界内列出所有可能的引用计数路径。
2. 从候选路径中找出证据支持度最高的路径。
3. 基于该路径构造复现器并执行验证。
4. 无论复现成功、失败、环境阻塞或结论不确定，都保留全部路径的排查结果。

本规范同时解决工作流中以下共性问题：

- LLM 自由文本无法保证路径完整性。
- Kernel Expert 重试可能覆盖前一轮证据。
- 复现结果与根因分析耦合，复现失败时容易丢失分析价值。
- QEMU 仅靠日志子串判断成功，可能产生假阳性。
- 专家状态、资源生命周期和会话数据缺少可验证的不变量。

## 2. 范围与非目标

### 2.1 范围

- Linux 内核 UAF、引用计数泄漏、重复释放、漏 `put`、错误引用转移。
- 同步路径、错误回滚路径、异步 workqueue/RCU/timer 路径及有限并发交错。
- Kernel Expert、Test Expert、Knowledge Base 之间的数据契约和证据传递。
- Crash、源码、日志、QEMU 结果的统一证据表示。

### 2.2 非目标

- 不承诺在无边界条件下枚举内核中的数学意义“全部执行路径”。
- 不使用复现成功替代根因证明。
- 不把 LLM 判断作为可验证事实的唯一来源。
- 第一阶段不引入完整定理证明器或全内核模型检查。

“所有可能路径”定义为：在指定内核版本、Kconfig、分析入口集合、对象类型、调用深度、间接调用解析结果和并发模型下，所有已发现的可达引用计数路径。

## 3. 形式化模型

### 3.1 对象状态

```text
ObjectState = (lifecycle, refcount, owners, aliases)

lifecycle ∈ {allocated, alive, releasing, freed}
refcount ∈ Z
owners    = 当前持有引用的主体集合
aliases   = 仍可能访问对象的指针或容器集合
```

### 3.2 状态迁移事件

| 事件 | 状态变化 | 典型 API |
|------|----------|----------|
| `GET` | `refcount += 1` | `kref_get`、`refcount_inc`、`get_device` |
| `PUT` | `refcount -= 1` | `kref_put`、`refcount_dec_and_test`、`put_device` |
| `TRANSFER` | owner A → owner B，计数通常不变 | 保存到容器、交给 worker、回调接管 |
| `FREE` | `lifecycle = freed` | release callback、`kfree`、slab free |
| `ACCESS` | 读取或修改对象 | 解引用、回调、异步任务 |
| `CANCEL` | 取消异步访问能力 | cancel work、del timer、RCU grace period |

### 3.3 必须保持的不变量

```text
I1: refcount >= 0
I2: ACCESS => lifecycle != freed
I3: FREE => refcount == 0
I4: GET 最终必须对应 PUT 或显式 TRANSFER
I5: max_likely_path_id ∈ all_possible_path_ids
I6: Paths(n+1) ⊇ Paths(n)
I7: test_passed => reproducer_started
                  ∧ target_signal_after_start
                  ∧ target_context_matched
I8: 每次 crash session acquire 恰好对应一次 release
```

其中 `I6` 保证 Kernel Expert 重试只能补充路径，不能删除已经确认或已经排除的路径。

## 4. 数据契约设计

### 4.1 问题分类

新增确定性字段：

```json
{
  "issue_type": "uaf",
  "reference_model": "kref",
  "target_object": "struct foo",
  "classification_evidence": []
}
```

`issue_type` 至少支持：

- `uaf`
- `refcount_leak`
- `refcount_underflow`
- `double_put`
- `missing_put`
- `ownership_transfer_error`
- `unknown_memory_lifetime`

### 4.2 单条引用路径

目标 contract：

```json
{
  "id": "P1",
  "entrypoint": "foo_ioctl",
  "description": "ioctl 获取引用后交给 worker，close 提前释放",
  "events": [
    {
      "sequence": 1,
      "function": "foo_ioctl",
      "source": "drivers/foo/foo.c:120",
      "operation": "get",
      "delta": 1,
      "owner": "userspace fd",
      "guard": "cmd == FOO_START",
      "thread": "caller"
    },
    {
      "sequence": 2,
      "function": "foo_release",
      "source": "drivers/foo/foo.c:240",
      "operation": "put",
      "delta": -1,
      "owner": "userspace fd",
      "thread": "close"
    },
    {
      "sequence": 3,
      "function": "foo_worker",
      "source": "drivers/foo/foo.c:310",
      "operation": "access",
      "delta": 0,
      "owner": "workqueue",
      "thread": "kworker"
    }
  ],
  "net_delta": 0,
  "terminal_state": "uaf",
  "reachability": "reachable",
  "evidence_level": "observed",
  "evidence": [],
  "unknowns": [],
  "eliminated_by": []
}
```

关键字段要求：

- `id`：稳定路径 ID，跨重试保持不变。
- `events`：按发生顺序保存 get/put/transfer/free/access。
- `net_delta`：由程序根据事件计算，不能只信任 LLM 输出。
- `terminal_state`：`balanced`、`leak`、`underflow`、`uaf`、`inconclusive`。
- `reachability`：`reachable`、`unreachable`、`unknown`。
- `evidence_level`：`observed`、`inferred`、`hypothetical`。
- `eliminated_by`：被排除路径仍须保存排除依据。

### 4.3 路径覆盖范围

```json
{
  "kernel_commit": "<commit>",
  "kernel_config": "<config artifact>",
  "target_object": "struct foo",
  "entrypoints": ["open", "ioctl", "release", "worker callback"],
  "terminal_functions": ["foo_release", "kfree"],
  "max_call_depth": 12,
  "concurrency_model": "two threads plus one workqueue",
  "resolved_indirect_calls": 8,
  "unresolved_indirect_calls": 2,
  "status": "incomplete",
  "limitations": []
}
```

`status` 支持：

- `complete_within_scope`
- `incomplete`
- `blocked`

只有提供了上述覆盖边界，最终文本才可以使用“全部路径”表述；否则必须写“当前已发现路径”。

### 4.4 UAF 分析总契约

```json
{
  "issue_type": "uaf",
  "coverage": {},
  "all_possible_paths": [],
  "max_likely_path_id": "P1",
  "ranking_reason": [],
  "reproduction_target_path_id": "P1",
  "analysis_status": "complete_within_scope"
}
```

校验规则：

1. UAF/refcount 问题的 `all_possible_paths` 不得为空。
2. `max_likely_path_id` 必须属于候选路径集合。
3. 每条路径必须包含入口、事件、终态、证据等级和未知项。
4. `net_delta` 必须由确定性校验器重新计算。
5. `reachable` 路径不能缺少 guard 和至少一项证据。
6. `analysis_status=incomplete` 时允许继续复现，但最终报告必须显示限制。

## 5. 最大可能路径选择

在没有真实概率模型时，不使用未经依据的百分比，而选择“证据支持度最高路径”。采用确定性优先级：

1. vmcore 中直接观察到目标对象、引用值和调用栈。
2. free/access 栈与源码路径一致。
3. 路径 guard 与现场变量一致。
4. 引用计数变化能够解释现场计数和对象终态。
5. 并发关系可由锁、workqueue、RCU、timer 或任务状态解释。
6. 历史案例和静态代码模式仅作为辅助证据。

排序结果必须输出：

- `max_likely_path_id`
- 选择依据
- 反证和不确定项
- 其他路径没有被选择的原因

复现器只针对最大可能路径构造，但不得删除其他路径。

## 6. 工作流设计

### 6.1 状态流转

```text
Validator
  → PM/Issue Classifier
  → Tool Experts
  → Reference Path Analyzer
  → Kernel Expert
  → Contract Validator
  → Test Expert
  → Knowledge Base
  → Final Response
```

### 6.2 Validator

- 从 `input.txt` 确定 kernel source、vmcore、vmlinux、boot kernel 和架构。
- 检测 UAF/refcount 关键字和 KASAN 证据。
- 只做输入与问题类型分类，不生成候选路径。

### 6.3 Tool Experts

- Crash Expert：提供真实对象地址、引用计数、allocation/free/access 栈。
- Kernel Log Expert：提取 KASAN 报告和时间关系，不假定所有问题都是 hung task。
- Knowledge Search：只提供历史模式，不可把相似案例当作当前事实。
- 每条结论必须绑定 evidence，工具错误时状态为 `degraded` 或 `failed`，不能仍标为 `ok`。

### 6.4 Reference Path Analyzer / Kernel Expert

执行顺序：

1. 确定目标对象、引用 API、release callback 和访问点。
2. 使用 semcode 建立入口到 get/put/free/access 的调用图。
3. 解析正常路径、错误回滚路径、引用转移路径和异步路径。
4. 对有限并发场景生成必要的事件交错。
5. 计算每条路径的 `net_delta` 和 terminal state。
6. 保存全部路径，包括排除路径和未知路径。
7. 选择最大可能路径。
8. 针对该路径生成 reproducer、Makefile 和 test.sh。

### 6.5 Contract Validator

UAF/refcount 问题进入 Test Expert 前执行：

```text
all_possible_paths 非空
max_likely_path_id 属于 all_possible_paths
coverage 已声明
reproduction_target_path_id == max_likely_path_id
测试所需 kernel/artifact 字段有效
```

路径覆盖不完整不阻止复现，但必须进入 `degraded/incomplete` 状态；结构错误或最大路径不存在时阻止测试。

### 6.6 Test Expert

测试成功条件从“日志包含子串”升级为：

```text
guest_booted
∧ REPRO_START 已出现
∧ reproducer 实际执行
∧ REPRO_START 之后出现目标故障信号
∧ KASAN/free/access 栈与目标模块或函数匹配
```

guest 测试脚本必须输出唯一标记：

```text
LUMEN_REPRO_START:<case-id>:<path-id>
LUMEN_REPRO_END:<case-id>:<path-id>:<status>
```

TestResult 增加：

- `target_path_id`
- `reproducer_started`
- `signal_after_start`
- `target_context_matched`
- `matched_stack_frames`
- `false_positive_checks`

### 6.7 Knowledge Base 与最终输出

最终输出必须由两部分组成：

1. LLM 生成的问题摘要和建议。
2. 程序确定性追加的路径分析附录和原始结构化 JSON。

固定结构：

```markdown
## 路径覆盖范围
## 所有已发现路径
## 已排除路径及依据
## 最大可能路径
## 复现目标与构造方式
## 复现结果
## 未覆盖范围和下一步排查建议
```

复现失败、QEMU 缺失、内核无法启动或测试达到上限时，以上路径章节仍必须完整输出。

## 7. 状态与资源一致性改造

### 7.1 单一事实源

避免顶层 state、`kernel_contract` 和 `test_contract.plan` 保存可独立修改的重复字段。

目标数据流：

```text
KernelExpertOutput → TestPlan → TestResult
```

顶层兼容字段只能由 contract 派生，不能作为独立输入源。

### 7.2 状态单调性

- `all_possible_paths` 使用路径 ID 去重并累积。
- 已排除路径不能删除，只能增加新的排除依据。
- 每次 Kernel Expert 重试保存独立 attempt 记录。
- `kernel_analysis` 和 `test_result` 不覆盖历史，改为 append-only attempts。

### 7.3 Crash session 生命周期

引入上下文管理器或 lease token：

```python
with crash_session(vmcore, vmlinux) as session:
    ...
```

要求所有成功、异常、超时和提前返回路径都释放引用，并增加 acquire/release 平衡测试。

### 7.4 会话隔离

- 不使用进程级 `_session_dir` 保存单次工作流状态。
- 不使用进程级 `KERNEL_SOURCE_DIR` 作为下游配置来源。
- session 路径、kernel source 和 semcode db 通过 state/contract 显式传递。
- 并发运行不同 input.txt 时不得互相覆盖输出目录或源码路径。

## 8. 实施计划

### Phase 1：信息保持与兼容字段

状态：部分完成。

- 增加 `all_possible_paths` 和 `max_likely_path`。
- Kernel Expert 重试合并历史路径。
- Knowledge Base 和最终响应显示路径分析。
- 兼容旧的文本 marker 和旧 contract。

验收：复现失败或重试后，已发现路径仍出现在最终响应。

### Phase 2：结构化路径契约

- 新增 `ReferenceEvent`、`RefcountPath`、`PathCoverage`、`UafAnalysisContract`。
- 把 `list[str]` 迁移为结构化路径；旧字段保留一个兼容周期。
- 实现 `net_delta`、path ID、最大路径成员关系校验。
- 为 marker 输出保留只读 fallback，不再作为主要事实源。

验收：构造错误 contract 时能够确定性拒绝，并指出具体路径和字段。

### Phase 3：路径分析器与覆盖声明

- 基于 semcode 获取函数、调用者、被调用者、类型和调用链。
- 建立 get/put/transfer/free/access 事件图。
- 支持正常、错误回滚、异步和有限并发路径。
- 输出 unresolved indirect calls 和覆盖限制。

验收：对测试 UAF 模块能枚举预置的全部路径，识别漏 put、重复 put 和 free 后访问。

### Phase 4：复现因果验证

- test.sh 输出 `LUMEN_REPRO_START/END`。
- 串口日志按时间窗口截取复现器执行后的内容。
- KASAN 报告匹配目标模块、函数或对象上下文。
- 防止 expected signal 被 echo 或无关启动故障触发。

验收：伪造 signal、启动期 KASAN 和无关 WARNING 均不能判定复现成功。

### Phase 5：资源与会话一致性

- Crash session 改为上下文管理器。
- 修复 kernel log 分支异常路径未释放的问题。
- 移除 per-session 全局变量和环境变量依赖。
- 增加两个 workflow 并发运行的隔离测试。

验收：所有异常注入点 acquire/release 平衡，不同 session 的文件和 kernel source 不串扰。

### Phase 6：路由强约束与清理

- UAF/refcount 路由消费结构化分析 contract。
- 删除无 contract 时默认进入 Test Expert 的 legacy fallthrough。
- 将 `kernel_ready_for_test` 默认值改为 false，并由有效 contract 推导。
- 移除过渡 marker 和重复顶层字段。

验收：所有状态转移满足路由不变量，非法状态不能进入 QEMU 测试。

## 9. 测试方案

### 9.1 单元测试

- `GET/PUT/TRANSFER/FREE/ACCESS` delta 计算。
- `refcount < 0`、漏 put、重复 put、free 后 access 检测。
- `max_likely_path_id` 成员关系。
- 路径跨重试单调合并。
- coverage incomplete 的最终文本展示。
- Crash session 异常路径释放。

### 9.2 状态机性质测试

使用 property-based test 或等价状态遍历验证：

```text
test_passed => test_contract.code == PASSED_REPRODUCED
PASSED_REPRODUCED => reproducer_started && target_context_matched
uaf_issue => all_possible_paths 非空或 analysis_status == blocked
max_likely_path_id ∈ path_ids
attempts 单调增加且最终终止
```

### 9.3 集成测试

至少覆盖：

1. 漏 `put` 导致引用泄漏。
2. 重复 `put` 导致提前释放。
3. workqueue 持有裸指针，close 后异步访问导致 UAF。
4. 错误回滚路径漏释放。
5. 复现失败但路径分析完整保留。
6. QEMU 不可用但最终输出仍包含全部路径。
7. 第一次分析发现 P1/P2，重试发现 P3，最终包含 P1/P2/P3。
8. 日志中 echo expected signal，不得误判成功。

### 9.4 门禁

```bash
venv/bin/python -m compileall -q agents graph dev/scripts dev/tests
venv/bin/python dev/scripts/check_agent_contracts.py
venv/bin/python dev/scripts/run_static_checks.py
venv/bin/pytest -q dev/tests/
```

重型 UAF QEMU E2E 放入显式 E2E 门禁，不作为普通离线单元测试的硬依赖。

## 10. 验收标准

方案完成需要同时满足：

1. 每个 UAF/refcount 案例都有范围明确的路径覆盖声明。
2. 所有候选路径以结构化 contract 保存，并可追溯到源码或现场证据。
3. 最大可能路径属于候选集合，选择依据可解释。
4. 复现器明确绑定一个 path ID。
5. 只有满足因果条件的目标故障才能判为复现成功。
6. 复现失败、环境跳过或工具故障不删除路径分析。
7. Knowledge Base 和 CLI 最终输出都包含确定性路径附录。
8. Kernel Expert 重试满足路径集合单调性。
9. Crash session 引用获取和释放在所有控制流上平衡。
10. 并发 workflow 之间不存在 session、输出目录或 kernel source 串扰。

## 11. 兼容与迁移

- 当前 `all_possible_paths: list[str]` 作为 Phase 1 兼容字段。
- 新结构上线后，读取旧结果时转换为 `RefcountPath` 的 degraded 形式，并标记 `legacy_unstructured`。
- 一个兼容周期内同时输出旧 marker 和新 JSON contract。
- 下游完成迁移后，marker 仅用于人工阅读，不再参与路由和成功判定。
- 所有强制门禁先在测试资产上验证，再从 warning/degraded 升级为 blocked。

## 12. 当前结论

现阶段已完成“路径信息保持”的基础改造，但尚未完成严格的路径完整性证明、最大路径一致性校验和复现因果验证。后续应优先推进 Phase 2 和 Phase 4：先让路径可机器验证，再消除 QEMU 复现假阳性；随后处理资源生命周期和并发会话隔离问题。

## 13. 长链路 Agent 可靠性设计

### 13.1 可靠性目标

长链路的整体成功率近似为各节点成功率的乘积。若 10 个串行节点单次成功率均为 95%，一次执行成功率约为：

```text
0.95 ^ 10 ≈ 59.9%
```

工程目标不是假设每个 Agent 永不犯错，而是让每个节点满足：

```text
失败可检测
∧ 状态可持久化
∧ 输入可重放
∧ 失败类型可分类
∧ 恢复行为确定
```

若一个可重试节点最多执行 3 次，且失败近似独立，则该节点有效成功率为：

```text
1 - (1 - 0.95) ^ 3 = 99.9875%
```

但真实系统中的限流、配置错误、脏缓存和错误 prompt 往往是相关失败，因此不能只依赖重试；必须同时具备校验、错误分类和确定性降级。

### 13.2 当前能力与缺口

| 能力 | 当前实现 | 主要缺口 |
|------|----------|----------|
| 输出持久化 | Agent 输出和部分工具结果写入磁盘 | 输出文件不能直接驱动节点级恢复；写失败部分被静默忽略 |
| 工作流 checkpoint | LangGraph `MemorySaver` | 仅进程内有效，进程退出后不能续跑 |
| 确定性缓存 | ikconfig、crash command、initramfs | 部分 key 未包含代码/contract/tool 版本；部分写入非原子 |
| 输出校验 | Pydantic contract、artifact 检查 | 各节点 precondition/postcondition/failure behavior 未统一声明 |
| 超时 | LLM、CLI、QEMU、脚本多数已有 timeout | 缺少全链路 deadline、统一 timeout 分类和取消传播 |
| 重试 | Test Expert 和 E2E 有有限重试 | E2E 仍可能整链重跑；瞬时/永久/合法空未统一分类 |
| 语义错误 | 部分 blocked reason 和状态码 | 大量路径仍使用字符串或吞异常，缺少统一 ErrorEnvelope |
| 可重放 | session 保存部分输入输出 | 缺少输入指纹、代码版本、分支选择和依赖版本 manifest |
| 熔断/预检 | QEMU、文件、部分工具有 preflight | 外部 LLM、RAG、MCP 缺少统一健康检查和熔断状态 |

### 13.3 节点执行契约

每个节点必须声明统一的 `NodeExecutionContract`：

```json
{
  "node": "kernel_expert",
  "version": "<code hash or contract version>",
  "input_fingerprint": "sha256:...",
  "preconditions": [],
  "status": "ok",
  "branch": "normal",
  "attempt": 1,
  "started_at": "...",
  "finished_at": "...",
  "duration_ms": 1234,
  "output_artifacts": {},
  "postconditions": [],
  "error": null,
  "retry": {
    "retryable": false,
    "max_attempts": 1,
    "next_delay_ms": 0
  }
}
```

节点契约包含三类规则：

1. `preconditions`：输入不满足时 fail-fast，错误归因给正确的上游。
2. `failure behavior`：明确停止、重试、降级、合法跳过或复用缓存，禁止隐式行为。
3. `postconditions`：输出只有通过校验后才能交给下游。

建议节点行为：

| 节点 | 前置条件 | 后置条件 | 失败行为 |
|------|----------|----------|----------|
| Validator | input 可解析 | artifact contract 完整 | `blocked`，请求补充输入 |
| PM | validation passed | 专家集合属于能力清单 | 规则降级；不能产生未知专家 |
| Tool Expert | 所需工具和 artifact 可用 | evidence 带来源和状态 | 瞬时错误节点重试；工具缺失 `blocked`；无数据 `valid_empty` |
| Kernel Expert | 源码和专家证据可访问 | kernel/UAF contract 校验通过 | 保留已有路径；输出 `incomplete` 或 `blocked` |
| Test Expert | boot artifact 和 test plan 有效 | 因果复现 contract 完整 | 仅重试可恢复步骤；环境问题 `skipped/blocked` |
| Knowledge Base | 至少有原始状态快照 | 文件原子落盘且确定性附录存在 | LLM 失败时保存原始结构化内容 |

### 13.4 统一错误分类

把“取不到”“没结果”“结果不完整”和“永久错误”分开：

```text
TRANSIENT       超时、连接中断、5xx、临时限流；允许有限重试
VALID_EMPTY     查询成功但业务上没有数据；不重试，显式继续或跳过
PARTIAL         返回部分内容；必须通过完整性校验决定降级或失败
INVALID_INPUT   输入路径、字段、架构或 contract 不合法；不重试
UNAVAILABLE     工具、QEMU、MCP 或模型未安装；预检后快速阻塞
PERMANENT       权限、认证、模型不支持、确定性编译错误；不自动重试
INTERNAL_BUG    断言、解析器异常、非法状态迁移；保存快照并停止
```

统一错误对象：

```json
{
  "category": "TRANSIENT",
  "code": "LLM_READ_TIMEOUT",
  "message": "Kernel Expert 请求在 120 秒内未返回",
  "cause": "upstream read timeout",
  "node": "kernel_expert",
  "attempt": 2,
  "retryable": true,
  "next_action": "10 秒后仅重试 kernel_expert",
  "upstream_field": "",
  "artifacts": {"input_snapshot": "..."}
}
```

错误信息必须同时回答：哪里失败、为什么失败、是否可重试、下一步执行什么、从哪个节点恢复。

### 13.5 持久化检查点与恢复

#### 检查点粒度

每个节点只有在 postcondition 通过后才提交检查点：

```text
RUNNING → VALIDATING → COMMITTED
                  ↘ FAILED
```

`COMMITTED` 是下游可以消费的唯一状态。磁盘上存在输出文件不等于节点完成。

#### 检查点内容

- 节点输入和输出 contract。
- 输入、输出和代码指纹。
- Agent/prompt/skill/工具版本。
- 执行分支、attempt、耗时和错误对象。
- 原始工具证据及其 artifact 路径。
- 下一个允许执行的节点。

#### 恢复规则

```text
checkpoint 存在
∧ status == COMMITTED
∧ input_fingerprint 相同
∧ node_version 相同
∧ artifacts 校验通过
=> 跳过节点并读取结果
```

否则缓存失效并重新执行该节点。进程级 `MemorySaver` 应替换为持久化 checkpointer；仅保存文本文件不能视为可恢复 checkpoint。

### 13.6 内容寻址缓存

缓存 key 必须包含所有影响输出的因素：

```text
cache_key = hash(
    normalized_input
    + upstream_contract_hash
    + node_code_hash
    + prompt_hash
    + skill_hash
    + tool_version
    + relevant_config
)
```

规则：

1. 不缓存 `TRANSIENT`、`PARTIAL` 和未通过 postcondition 的结果。
2. `VALID_EMPTY` 可以缓存，但必须带有效期和业务上下文。
3. 缓存文件先写同目录临时文件、`fsync`，再原子 `rename`。
4. 读取缓存时重新校验 schema、checksum 和 artifact 完整性。
5. 并发写相同 key 时使用文件锁或 write-once 内容寻址对象。
6. 失败 cache 与成功 cache 分开，避免一次网络失败长期污染后续运行。

现有缓存的改造重点：

- ikconfig cache：加入解析器版本，改为原子 JSON 写入。
- crash command cache：避免持久缓存瞬时失败；并发 append 增加锁或改为每 key 单文件。
- initramfs/rootfs cache：加入构建脚本内容、busybox/toolchain 版本和 rootfs schema；复制完成后再原子发布。
- Agent 输出：从“按时间戳保存文本”升级为“contract + manifest + raw output”的 committed artifact。

### 13.7 超时、deadline 与取消

每个外部 I/O 必须有 timeout，但还需全链路 deadline：

```text
workflow_deadline
  ├── node_deadline
  │     ├── connect_timeout
  │     ├── read_timeout
  │     └── subprocess_timeout
  └── remaining_budget
```

要求：

- 下游 timeout 不能超过 workflow 剩余时间。
- 父节点取消后终止子进程、QEMU 和 CLI，避免孤儿进程。
- timeout 转换成结构化 `TRANSIENT` 或 `PERMANENT` 错误，禁止无限等待。
- hint 等人工等待计入独立 human-in-the-loop budget，不占用工具执行 timeout。

### 13.8 节点级重试、退避和熔断

只对幂等且 `retryable=true` 的节点操作重试：

```text
delay = min(base * 2^attempt + jitter, max_delay)
```

禁止：

- 对 `INVALID_INPUT`、确定性编译失败、contract 校验失败做盲目重试。
- 在整条 workflow 外层无条件重跑。
- 重试时清空已经提交的上游 checkpoint。

熔断器按依赖维度维护：

```text
CLOSED → 连续失败达到阈值 → OPEN
OPEN   → 冷却后允许一次探测 → HALF_OPEN
HALF_OPEN → 成功 CLOSED；失败 OPEN
```

适用依赖：LLM endpoint、RAG/Chroma、semcode MCP、crash backend 和 QEMU capability。开工前执行轻量 preflight，依赖明确不可用时快速失败，不让每个节点重复撞同一个故障。

### 13.9 可重放与可观测性

每个 session 生成 `run_manifest.json`：

```json
{
  "session_id": "...",
  "git_commit": "...",
  "dirty_worktree": false,
  "input_file_hash": "...",
  "config_hash": "...",
  "kernel_source_commit": "...",
  "nodes": [],
  "external_dependencies": {},
  "final_state": "..."
}
```

每个节点边界记录：

- 输入和输出指纹。
- contract 摘要和 artifact checksum。
- 正常、缓存命中、重试、降级或阻塞分支。
- 工具调用次数、耗时和 timeout。
- 用于复现失败的最小输入快照。

提供重放入口：

```bash
python main.py --resume-session <session-id>
python main.py --replay-node <session-id> <node-name>
```

`--resume-session` 从最后一个合法 committed checkpoint 继续；`--replay-node` 使用保存的输入快照单独重放节点，不调用无关上游。

### 13.10 UAF 流程中的恢复边界

UAF 分析按以下可恢复阶段提交：

```text
输入校验
→ vmcore/log 证据采集
→ 调用图和引用事件图
→ 全路径 contract
→ 最大可能路径选择
→ reproducer 构建
→ rootfs 构建
→ QEMU 验证
→ 归档
```

恢复要求：

- QEMU 失败只重跑 QEMU 或复现器调整，不重新执行已提交的 vmcore 分析。
- reproducer 修改只失效 reproducer、rootfs 和 QEMU 下游缓存。
- kernel source commit 变化时，调用图、路径 contract 和全部下游自动失效。
- 最大路径排序策略变化时，只失效排序及复现下游，不重新采集原始 Crash 证据。
- Knowledge Base 失败只重跑归档，不能重新触发分析和 QEMU。

### 13.11 长链路实施计划

#### Reliability Phase R1：节点契约和错误分类

- 定义 `NodeExecutionContract`、`ErrorEnvelope` 和错误分类枚举。
- 为 Validator、Kernel Expert、Test Expert、Knowledge Base 增加 pre/postcondition。
- 清理“异常后仍 status=ok”和吞异常无记录的问题。

验收：相同故障始终产生相同 error code、状态和 next action。

#### Reliability Phase R2：持久化 checkpoint

- 引入磁盘或数据库 checkpointer。
- 节点 postcondition 通过后原子提交 contract 和 manifest。
- 支持 `--resume-session`。

验收：在任意节点强制终止进程后，重新启动只重跑未提交节点。

#### Reliability Phase R3：缓存正确性

- 统一内容寻址 key 和版本字段。
- 所有 cache/artifact 使用原子写。
- 禁止缓存瞬时失败和 partial output。
- 增加缓存损坏、版本变化、并发写测试。

验收：上游、代码或 prompt 改变时下游自动失效；半写文件永远不被当作命中。

#### Reliability Phase R4：局部重试和熔断

- 为 LLM/MCP/RAG/crash 定义可重试错误集合。
- 指数退避、jitter、最大尝试次数和依赖级熔断。
- 删除无条件整链重试。

验收：注入一次瞬时失败只重跑对应节点；永久错误不会重复消耗时间。

#### Reliability Phase R5：重放与故障注入

- 完成 `run_manifest.json`、`--replay-node` 和依赖调用记录。
- 对 timeout、5xx、合法空、partial、缓存损坏、进程中断做故障注入。
- 统计首次成功率、恢复后成功率、平均恢复节点数和错误定位时间。

验收：偶发失败可以用保存的输入快照稳定重放，且不依赖整链重跑。

### 13.12 可靠性验收指标

除端到端成功率外，持续统计：

- `first_pass_success_rate`：一次执行成功率。
- `recovered_success_rate`：有限局部恢复后的成功率。
- `resume_reuse_ratio`：恢复时复用 committed 节点的比例。
- `false_success_rate`：错误结果被标记成功的比例，目标为 0。
- `undetected_partial_rate`：partial output 未被 postcondition 拦截的比例，目标为 0。
- `mean_recovery_nodes`：一次失败平均需要重跑的节点数，目标接近 1。
- `mean_time_to_diagnose`：从失败到明确 error code 和 next action 的时间。
- `cache_invalid_hit_rate`：脏缓存/版本漂移导致的错误命中率，目标为 0。
- `hung_invocation_count`：无 deadline 的挂起调用数量，目标为 0。

最终验收场景必须包括：

1. Kernel Expert 调用中途进程退出，恢复后从该节点继续。
2. LLM 发生一次 timeout，只局部重试并保留上游分析。
3. semcode 返回合法空、部分结果和网络失败时分别进入不同状态。
4. cache 文件写到一半进程退出，下次运行视为 cache miss。
5. kernel source、prompt 或代码变化后相关 checkpoint 正确失效。
6. QEMU 连续失败触发熔断，但所有 UAF 路径仍进入最终输出。
7. Knowledge Base 调用失败时，确定性报告和原始 contract 仍成功落盘。
