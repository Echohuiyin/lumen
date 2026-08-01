# Lumen 开发纲要与规格

## 1. 项目定位

Lumen 是 Linux 内核维护团队使用的问题辅助定位框架，面向内核崩溃、死锁、内存错误、性能退化和设备/子系统异常的证据分析、诊断性复现、修复验证与归档。

本项目不是漏洞挖掘、漏洞利用或攻击工具。文档、prompt、代码注释和最终报告统一使用“内核问题”“故障触发条件”“诊断性用户态 C reproducer”等维护语义，不使用武器化、攻击、提权或面向第三方系统的表述。

诊断性 reproducer 只能在用户授权的源码、制品和隔离 QEMU 环境中运行，目的仅限于：

- 验证根因分析是否与原始日志和源码证据一致；
- 确认修复前后行为变化；
- 沉淀可审计、可回归的维护案例。

目标不是保证所有问题都能自动复现，而是保证证据完整、失败可定位、复现结论可信、结果可归档。

## 2. 目标架构

```text
input.txt
  → Validator
  → PM
  → Tool Experts（crash / lock / log / knowledge）
  → Kernel Expert（证据汇总、源码分析、根因、用户态 C reproducer）
  → Test Expert（隔离镜像、QEMU、guest 内编译执行、注入、调用链判定）
       ├─ 调用链一致：Knowledge Base → Final Response
       ├─ 未一致且 try-out < 10：结构化反馈 → Kernel Expert
       └─ 第 10 次仍未一致或硬阻断：Knowledge Base → Final Response
```

Kernel Expert 与 Test Expert 组成显式 loop。两个角色通过结构化 contract 交接，禁止通过自然语言覆盖工具证据。

## 3. 角色边界

| 组件 | 核心职责 | 明确禁止 | 输出事实源 |
|---|---|---|---|
| Validator | 解析并校验输入制品、架构与内核源码 | 猜测缺失路径或架构 | `input_artifacts_contract` |
| PM | 规则化分类并选择工具专家 | 用 LLM 文本替代确定性路由 | `required_experts` |
| Tool Experts | 采集 crash、锁、日志和历史案例证据，每个结果独立落盘 | 输出最终根因、reproducer 或复现成功结论 | `expert_results` |
| Kernel Expert | 阅读原始日志、工具结果和只读源码，分析根因并生成用户态 C reproducer | 启动 QEMU、写内核模块、编造源码证据、宣称测试成功 | `kernel_contract` |
| Test Expert | 理解问题原理，准备隔离镜像，在 QEMU guest 内编译执行 reproducer，按需注入压力/故障并判定调用链一致性 | 修改根因、修改 reproducer 源码、用 LLM 覆盖确定性失败 | `test_attempt_contract` |
| Deterministic Test Runner | 执行镜像复制、QEMU、SSH/SCP、guest 编译、注入、串口采集和调用链规则匹配 | 自行推断执行步骤或最终根因 | runner step artifacts |
| Knowledge Base | 归档证据、根因、所有 try-out 和最终结论 | 删除失败轮次或把未验证结论写成已复现 | `knowledge_file`、`final_response` |

## 4. 全局开发约束

1. 每次修改前评估机制变化和上下游影响。
2. 新增功能必须同时增加单元测试、负向测试和适用的在线 LLM/QEMU 测试，并纳入门禁。
3. No fallback：缺制品、缺工具、缺配置、contract 错误或确定性校验失败时显式报错或 `blocked`，禁止猜测补全。
4. 禁止 monkey patch。
5. Bugfix 和新功能严格限制在批准范围内；无关问题只记录。
6. 一键部署脚本及其依赖检查必须同步维护。
7. 远端 `$HOME/lumen/spec.md` 是规格事实源；每次更新后同步本地副本并校验 SHA-256 一致。
8. 所有 prompt 必须明确 Linux 内核维护定位，使用诊断性语言，不得把工作描述为漏洞发现或利用。

## 4.1 Kernel Expert ????????

Kernel Expert ?????? Claude settings/model profile???????????profile ??????? name ? settings ????????? model?settings ???????????????API key?token ? provider ?????????? contract?

1. ?????? Kernel Expert ????? round-robin ?? profile???? profile ???????????????????????????
2. ??????????? quota preflight?????????????????????? skill?????????????? profile ???? cooldown???????????
3. ????????????/log ???????????????? 429??????? billing ?????????????? profile??????MCP ?????/??????????????
4. ????? GLM settings ? ?? settings ?????? settings ????????????????????????
5. ????????????????????? Kernel Expert ? Linux ?????????? C-only reproducer ????????? Test Expert ??? QEMU ???

## 5. 输入与证据

- `input.txt` 是用户输入和 `kernel_source` 的唯一配置来源；`kernel_source` 必须是绝对路径。
- `log:` 是原始日志路径的唯一来源。没有 log 时，日志专家从可读的 `vmcore`/`vmlinux` 提取完整日志并原子落盘。
- `vmcore` 和 log 至少存在一个；只有 vmcore 时必须同时提供可读取的 `vmlinux`。
- `boot_kernel_path` 必须是可启动的 `bzImage`/`Image`，不得用 ELF `vmlinux` 启动 QEMU。
- 目标架构必须明确为 `x86_64`、`arm64` 或 `arm32`，不得按宿主架构静默猜测。
- Kernel Expert 只接收原始日志路径和工具专家结果文件路径，按需读取；复制的摘要不是第一手证据。
- 每条源码 evidence 必须包含 `source_domain`、函数、文件、行号和来源。`kernel` 与 `reproducer` 源码域禁止混写。

## 6. Kernel Expert 规格

### 6.1 输入

- 用户问题和 `input_artifacts_contract`；
- 原始 log 路径；
- 各工具专家独立结果文件路径；
- 只读内核源码目录和 Semcode 能力；
- 上一轮 `test_attempt_contract`，仅在 loop 重试时提供；
- 当前 try-out 编号和最大次数 10。

### 6.2 职责

1. 汇总工具专家证据，但不得把专家推测提升为事实。
2. 直接阅读原始日志，提取故障签名和原始调用链。
3. 优先使用 Semcode 定位源码，复核关键函数、调用关系、对象状态、并发关系和触发前置条件。
4. 基于日志与源码证据形成根因分析。
5. 定义调用链 oracle，包括关键帧、顺序、故障签名和目标上下文。
6. 编写能够从用户态接口触发目标内核路径的诊断性 C reproducer。
7. 重试时读取上一轮结构化反馈，明确记录本轮相对上一轮的修改及理由。
8. 只生成分析和 reproducer contract；不得执行 QEMU 或自行宣称复现成功。

### 6.3 用户态 C-only 硬规则

Kernel Expert 只能生成用户态 C 程序：

- `language` 必须为 `c`；
- `artifact_type` 必须为 `userspace`;
- 源文件后缀只能是 `.c`/`.h`；
- 通过 syscall、ioctl、netlink、文件系统接口、设备节点或其他公开用户态 ABI 触发目标路径；
- 编译必须在 Test Expert 启动的 QEMU guest 内完成；
- 可以提供结构化编译参数和链接库声明，但不得提供自由 shell。

以下内容一律拒绝：

- `.ko`、`.mod.c`、Kbuild、`obj-m`；
- `module_init`、`module_exit`、`MODULE_LICENSE` 等内核模块入口；
- `insmod`、`rmmod`、`modprobe`、`load_module`；
- 修改或重编译目标内核；
- 执行自由形式 `test.sh`；
- 仅打印期望日志文本来伪造复现信号。

发现上述内容时，contract 后置校验必须返回 `BLOCKED_NON_USERSPACE_REPRODUCER`，不得进入 Test Expert。

### 6.4 源码与路径分析

- 源码定位必须优先调用 Semcode；Semcode 无结果或不可用时记录 `blocked`，不得用 grep 或 LLM 猜测替代。
- Kernel Expert 写 reproducer 前必须完成关键路径源码复核。
- UAF、kref、refcount 和引用泄漏问题必须输出结构化路径分析：
  - get / put / transfer / free / access 事件；
  - 所有候选路径、稳定 path ID、`net_delta` 和终态；
  - 最大可能路径和选择依据；
  - 正常、错误回滚、异步、转移、并发覆盖；
  - 未解析的间接调用和未覆盖边界。
- 重试只能增加或修正有证据支持的内容，不得删除已经归档的候选或排除路径。

### 6.5 KernelExpertContract

最小字段：

```text
status
tryout
root_cause
root_cause_evidence[]
original_call_chain
call_chain_oracle
reproducer
pressure_requirements[]
fault_injection_requirements[]
change_from_previous_tryout
warnings[]
blocked_reason
```

`reproducer` 至少包含：

```text
language = c
artifact_type = userspace
source_dir
source_files[]
entry_source
output_binary
compiler
compiler_args[]
link_libraries[]
run_args[]
runtime_timeout_sec
```

`call_chain_oracle` 至少包含：

```text
fault_signatures[]
required_frames[]
required_frame_alternatives[][]
required_frame_order[]
target_subsystems[]
target_objects[]
allowed_wrapper_frames[]
```

## 7. Test Expert 规格

### 7.1 定位

Test Expert 是理解问题原理的测试验证专家，不是单纯的 QEMU 命令包装器。它必须理解 Kernel Expert 给出的根因、目标路径和 oracle，判断测试产生的问题是否与原始日志中的问题一致。

Test Expert 可以否决疑似假阳性，但不能把确定性规则未通过的结果改成成功。

### 7.2 每轮执行流程

每个 try-out 使用独立目录：

```text
sessions/<session-id>/tryouts/tryout-01/
  image/
  source/
  build/
  logs/
  test_attempt_contract.json
```

严格流程：

1. 校验 `KernelExpertContract` 和 C-only 规则。
2. 校验 QEMU、KVM/TCG、base image、SSH key、guest 编译器和注入能力。
3. 从只读 base image 创建本轮隔离副本；策略可以是配置明确的 full copy、reflink 或 copy-on-write overlay，禁止复用上一轮已修改镜像。
4. 使用 `boot_kernel_path` 和本轮镜像启动新的 QEMU。
5. 等待 SSH 健康检查，在固定 deadline 内失败则 `blocked`。
6. 将 `.c`/`.h` 源码复制进 guest 的唯一临时目录。
7. 在 guest 内按结构化编译参数调用允许的 C 编译器，保留命令、stdout、stderr 和二进制摘要。
8. 按 contract 启用允许的压力注入和故障注入。
9. 写入 `LUMEN_REPRO_START:<case-id>:<tryout>` 串口标记。
10. 执行用户态 reproducer，采集 SSH 输出和 START 之后的新增串口日志。
11. 写入或确认 `LUMEN_REPRO_END:<case-id>:<tryout>:<status>`。
12. 执行调用链确定性比对和 Test Expert 原理复核。
13. 关闭 QEMU，保留本轮镜像、日志和 contract。

### 7.3 压力注入

只允许结构化 profile，不接受自由 shell：

- `cpu`
- `memory`
- `io`
- `scheduler`
- `filesystem`
- `network`

每个 profile 明确 `workers`、`duration_sec` 和有限的 profile 参数。是否启用必须基于根因和源码证据，并记录理由。

### 7.4 故障注入

故障注入只能使用目标内核已启用、guest 已验证存在的标准接口，例如：

- `failslab`
- `fail_page_alloc`
- `fail_futex`
- `fail_function`
- `fail_make_request`

每个注入步骤必须结构化声明：

```text
profile
probability
interval
times
space
target
duration_sec
rationale
```

Test Expert 必须先做 capability preflight。目标内核未启用相应配置或接口不存在时返回 `BLOCKED_FAULT_INJECTION_UNAVAILABLE`，不得切换成其他未声明机制。

### 7.5 调用链一致性

准确复现必须同时满足：

```text
QEMU 成功启动
AND guest 内 C reproducer 编译成功
AND REPRO_START 已出现
AND 目标故障信号出现在 START 之后
AND 原始日志要求的关键帧全部出现（候选帧组每组至少出现一个）
AND 关键帧顺序满足 required_frame_order
AND 目标子系统或对象上下文一致
AND Test Expert 原理复核未否决
```

允许出现中断、异常入口、架构包装和编译器内联造成的额外 wrapper frame，但不得缺失 oracle 声明的关键帧，也不得只匹配通用 `WARNING`、`BUG`、KASAN 或 panic 文本。

启动期异常、旧串口内容、reproducer 回显期望文本、无关 WARNING/KASAN、故障签名相同但调用链不同，都必须判定为不一致。

### 7.6 双层判定

第一层为确定性 `CallChainMatchContract`：

```text
signature_matched
start_marker_found
signal_after_start
required_frames_found[]
missing_frames[]
frame_order_matched
target_context_matched
matched_log_lines[]
false_positive_checks[]
```

第二层为 Test Expert 语义复核：

```text
principle_consistent
review_reason
contradictions[]
```

最终成功条件：

```text
deterministic_match = true
AND principle_consistent = true
```

### 7.7 TestAttemptContract

每轮必须记录：

```text
status
code
tryout
image_copy
qemu
guest_compile
pressure_steps[]
fault_injection_steps[]
execution
call_chain_match
semantic_review
call_chain_consistent
artifacts
error
kernel_feedback
```

`kernel_feedback` 只描述下一轮需要调整的触发条件、并发窗口、参数或源码假设，不得直接修改 reproducer。

## 8. Kernel Expert ↔ Test Expert loop

### 8.1 状态机

```text
Kernel Expert
  → contract 后置校验
      ├─ blocked → Knowledge Base
      └─ ready → Test Expert
                    ├─ CALL_CHAIN_CONSISTENT → Knowledge Base（成功）
                    ├─ NOT_CONSISTENT 且 tryout < 10 → Kernel Expert
                    ├─ NOT_CONSISTENT 且 tryout = 10 → Knowledge Base（失败）
                    └─ ENV/CONTRACT BLOCKED → Knowledge Base（阻断）
```

### 8.2 次数语义

- `max_tryouts` 固定为 10，配置只能收紧，不能扩大。
- try-out 从 Test Expert 接受有效 contract 并创建本轮隔离镜像时计数。
- guest 编译失败属于一次有效 try-out，因为它反映 reproducer 本身需要 Kernel Expert 修正。
- 输入、contract、base image、QEMU、SSH key 或 guest 工具链 preflight 失败属于硬阻断，不通过重复 loop 消耗次数。
- 调用链一致时立即成功退出，不执行剩余次数。
- 调用链不一致且当前次数小于 10 时，必须回到 Kernel Expert。
- 第 10 次仍不一致时，最终状态为 `FAILED_CALL_CHAIN_MISMATCH_AFTER_10_TRYOUTS`。
- 禁止无修改地重复完全相同的 reproducer 和注入计划；每轮必须提供 `change_from_previous_tryout`。

### 8.3 保留历史

所有 try-out contract、镜像身份、C 源码摘要、编译日志、执行输出和串口窗口必须追加归档。后续轮次不得覆盖或删除前轮证据。

## 9. 路由与状态

LangGraph 目标路由：

```text
START → validator → pm → tool_expert fan-out
      → kernel_expert → test_expert
      → route_after_test
          ├─ knowledge_base
          └─ kernel_expert
      → END
```

核心状态字段：

```text
kernel_contract
kernel_analysis
reproducer_source_dir
original_call_chain
call_chain_oracle
test_attempt_contract
test_attempts[]
tryout_count
max_tryouts = 10
call_chain_consistent
test_feedback
final_response
```

旧字段 `reproducer_module_path`、`load_module`、`test_script_path` 和 Kernel Expert 内嵌 runner 路径必须移除，不保留会重新启用内核模块或自由脚本的 fallback。

## 10. 错误与状态码

至少支持：

```text
BLOCKED_INVALID_INPUT
BLOCKED_INVALID_KERNEL_CONTRACT
BLOCKED_NON_USERSPACE_REPRODUCER
BLOCKED_BOOT_KERNEL_MISSING
BLOCKED_BASE_IMAGE_MISSING
BLOCKED_QEMU_UNAVAILABLE
BLOCKED_GUEST_COMPILER_UNAVAILABLE
BLOCKED_FAULT_INJECTION_UNAVAILABLE
FAILED_IMAGE_COPY
FAILED_QEMU_BOOT
FAILED_GUEST_COMPILE
FAILED_REPRODUCER_EXECUTION
FAILED_CALL_CHAIN_MISMATCH
FAILED_CALL_CHAIN_MISMATCH_AFTER_10_TRYOUTS
PASSED_CALL_CHAIN_CONSISTENT
```

每个错误必须包含类别、代码、原因、是否可重试、下一步动作和已有 artifacts。

## 11. 最终输出与归档

Knowledge Base 和 CLI 最终响应必须包含：

```text
项目定位：Linux 内核维护问题定位与诊断性复现
输入和分析范围
原始故障签名与调用链
工具专家证据
源码根因与证据位置
用户态 C reproducer 路径
每轮 try-out 的修改、注入、编译和执行结果
调用链一致性逐项检查
最终状态：一致 / 10 次后不一致 / blocked
未覆盖范围和下一步维护建议
```

任何失败都不得删除此前的源码、路径或测试证据。

## 12. 分阶段实施

### Phase 0：规格与基线冻结

1. 更新 `spec.md`、Kernel Expert prompt 和新 Test Expert prompt。
2. 记录当前离线门禁结果和已存在的无关工作树修改。
3. 为旧行为增加负向测试：内核模块、自由脚本、Kernel Expert 启动 QEMU 必须被拒绝。

验收：规格、prompt、能力声明一致；无代码行为漂移。

### Phase 1：Contracts 与路由

1. 增加用户态 reproducer、调用链 oracle、注入、test attempt contract。
2. 移除 `load_module` 和 `reproducer_module_path`。
3. 恢复 `test_expert` 节点及 `route_after_test`。
4. `max_tryouts` 固定为 10。

验收：纯路由单元测试覆盖成功、重试、第 10 次失败和硬阻断。

### Phase 2：Kernel Expert C-only

1. 从 Kernel Expert 移除 QEMU runner 调用。
2. Prompt 和后置校验只接受用户态 C 源码。
3. 从原始日志生成结构化调用链 oracle。
4. 重试时消费 Test Expert feedback，并要求记录修改差异。

验收：模块、Kbuild、自由 shell 均被阻断；有效 C contract 进入 Test Expert。

### Phase 3：Test Expert 与隔离 QEMU

1. 每轮创建隔离 image 副本。
2. 启动 QEMU、等待 SSH、上传 C 源码。
3. guest 内编译并运行，保留完整 artifacts。
4. 每轮关闭 QEMU，不复用已修改镜像。

验收：使用测试 image 完成 guest 编译/执行 E2E；上一轮文件不出现在下一轮。

### Phase 4：压力与故障注入

1. 实现结构化压力 profile。
2. 实现结构化 fault injection profile 和 capability preflight。
3. 所有注入加入超时、清理和证据记录。

验收：每个 profile 有正向、缺能力和非法参数测试。

### Phase 5：调用链判定与 10 轮 loop

1. 解析并规范化原始和测试调用链。
2. 实现关键帧、顺序、上下文和 START 后窗口比对。
3. Test Expert 增加原理一致性复核。
4. 完成最多 10 次 loop 和历史归档。

验收：一致立即成功；相同信号但不同调用链失败；第 10 次不一致终止。

### Phase 6：清理与部署

1. 删除或迁移旧 Test Expert、legacy one-shot runner 和 persistent runner 入口。
2. 更新 `agent_capabilities.json`、配置模板、README、DEPLOYMENT 和一键部署脚本。
3. 验证 x86_64/arm64 image、SSH、gcc、stress-ng 和 fault injection 依赖。

验收：静态能力校验无漂移，一键部署可准备完整测试环境。

## 13. 开发与验收门禁

每次修改遵守最小粒度原则，不为测试降低真实约束。

```bash
venv/bin/python -m compileall -q agents graph dev/scripts dev/tests
venv/bin/python dev/scripts/check_agent_contracts.py
venv/bin/python dev/scripts/run_static_checks.py
venv/bin/python -m pytest -q
```

新增专项门禁：

```text
Contract：拒绝模块、Kbuild、自由 shell 和缺失调用链 oracle
Router：成功、1～9 次重试、第 10 次失败、blocked
Runner：镜像隔离、guest 编译、压力/故障注入、超时清理
Oracle：关键帧缺失、顺序错误、启动期噪声、回显信号、无关调用链
Online LLM：Kernel Expert 输出 C-only contract；Test Expert 不覆盖确定性证据
QEMU E2E：真实 guest 内编译运行并产生一致调用链
```

离线单元测试不得依赖 API key、真实 vmcore 或 QEMU。真实 LLM/QEMU 测试作为显式环境门禁运行。
