# LawBuddy 借鉴 Hermes Agent 的优化升级改造方案

| 项目 | 内容 |
| --- | --- |
| 文档编号 | LB-HERMES-2026-01 |
| 文档状态 | 待评审；评审通过后按里程碑实施 |
| 创建日期 | 2026-11-17 |
| LawBuddy 基线 | 当前工作区；数据库 V50；Agent Runtime V1/V2 |
| Hermes 对照基线 | `NousResearch/hermes-agent` 提交 `6ce01ea637348b5ed9d3892e5d4f74863fa2c908` |
| 方案定位 | 对 LB-HARNESS-2026-01 的后续扩展，形成 Agent Runtime V3，不重复建设 V50 工作账本 |
| 数据库原则 | 只追加 V51+ 迁移，禁止修改 V1–V50 已发布迁移 |
| 评测范围 | 确定性单元/安全评测与 WorkflowArena LAW 任务；不扩展为其他领域评测 |

---

## 1. 执行摘要

本方案将 Hermes Agent 中值得借鉴的能力迁移为 LawBuddy 自身模块，目标不是引入另一套 Agent，而是在现有法律工作流和 V2 Harness 上补齐六类平台能力：

1. **更可靠的执行**：工具调用先持久化再执行，崩溃后可恢复、可对账、不会盲目重复副作用。
2. **更稳健的模型运行时**：统一 Provider 适配、错误分类、流式看门狗和受限回退。
3. **更强的长会话能力**：引证安全压缩、历史会话搜索、分支、彻底删除和产物快照。
4. **更可扩展的工具系统**：统一工具注册、中文 Tool Search、并行子代理和受限批处理。
5. **可控的持续学习**：记忆和技能只自动生成提案，经来源校验、安全扫描和人工批准后生效。
6. **更完整的工程闭环**：结构化观测、插件/MCP 隔离、故障注入、供应链和发布门禁。

核心路线：

> 保留 LawBuddy 的共享 contracts、Electron/Core 边界、Claim Ledger、案件隔离、原件只读、人工核实和密钥体系；将 Hermes 作为运行时机制参考，不作为产品底座替换。

### 1.1 不变量

以下要求优先级高于所有功能迁移：

- 事实、时间、人物、金额、权利义务、风险判断和建议必须能回溯到案件原文。
- 无可靠来源的内容必须进入 `needs_human`，不得因摘要、记忆或子代理转述而升格为已支持事实。
- 原件只读；子代理、插件和技能不得绕过文档管线、引用校验或工作副本边界。
- 所有新增 RPC 必须同步修改 Core、contracts、Electron 主进程、preload、UI 和评测。
- API Key 只能经统一加密配置源读取；禁止回退到 `.env`、明文文件、前端状态或命令行参数。
- 密钥环不可用时继续 fail-closed；不得复制 Hermes 的 keychain 到本地文件降级语义。
- 自动学习只能生成提案，不能直接修改已启用的长期记忆、技能或系统指令。
- 所有主子 Agent 共享轮次、模型调用、工具调用、Token 和墙钟硬预算。
- 默认不新增外部遥测、默认外发、云端执行或第三方进程内代码。
- 运行期不得读取 WorkflowArena `reference/`、真值、评分器内部答案或隐藏路径。

### 1.2 非目标

本方案不做以下事项：

- 不用 Hermes 替换 LawBuddy Agent Loop，不恢复固定六节点阅卷流程。
- 不迁移 Telegram、Discord、Slack、WhatsApp、Signal、Email 等消息平台。
- 不迁移 Modal、Daytona、Vercel Sandbox、SSH 集群等云终端后端。
- 不接入 Nous Portal、Honcho 云服务或 Hermes shared metrics 上传机制。
- 不引入默认无限轮次、任意 Python 工具脚本、第三方代码 Core 内执行。
- 不复制 Hermes 的明文 SessionDB、Provider `.env` 或 Vault 文件回退设计。
- 不进行 UI 整体改版，不改变文书模式通用左侧栏和现有主要交互入口。
- 不迁移训练导向的轨迹生成和模型训练压缩器，只复用其成本分析和压缩思想。

---

## 2. 现有基础与差距

### 2.1 可直接复用的 LawBuddy 基础

1. [autonomous.py](../services/core/lawbuddy/agent/autonomous.py) 已有自主工具循环、工具裁剪、上下文压缩、子代理、完成守门和逐轮检查点。
2. V50 已有工作单元、答案格、交付物和运行时状态，见 [A-03 契约与迁移设计](agent-harness-a03-schema-runtime.md)。
3. V1/V2 运行时钉住、预算恢复、检索范围和幂等交付已有确定性验收，见 [A-04 验收矩阵](agent-harness-a04-acceptance-matrix.md)。
4. [citations.py](../services/core/lawbuddy/citations.py) 与 [memory.py](../services/core/lawbuddy/memory.py) 已实现原文引用、记忆来源校验、版本失效和人工核实。
5. [sandbox.py](../services/core/lawbuddy/sandbox.py)、[policy.py](../services/core/lawbuddy/policy.py) 和 [keyring.py](../services/core/lawbuddy/keyring.py) 已实现工作空间边界、审批、参数哈希和 Windows DPAPI fail-closed。
6. [search.py](../services/core/lawbuddy/search.py) 已有 FTS5 trigram、BM25、LIKE 回退和强制案件过滤，可复用于工具及会话检索。
7. [A-04 故障注入](agent-harness-a04-acceptance-matrix.md) 已覆盖断连、预算恢复、子代理重复提交和交付中断，新测试应扩展现有矩阵而非另起体系。

### 2.2 需要补齐的差距

| 差距 | 当前风险 | 对应改造 |
| --- | --- | --- |
| 工具副作用与逐轮检查点之间存在崩溃窗口 | 恢复后可能重复写入，或无法判断副作用是否真实发生 | WP01 工具 Journal 与幂等提交 |
| Provider 配置、错误和流式恢复分散 | 新服务商适配成本高，异常分类和看门狗语义不一致 | WP02 ProviderProfile 与统一错误模型 |
| 技术事件、审计和性能指标边界不统一 | 难以重建父子调用链，且容易误把内容写入日志 | WP03 结构化观测 |
| 压缩以确定性摘录为主，缺少归档版本与并发写保护 | 长会话可导航性有限，压缩与恢复并发时可能互相覆盖 | WP04 引证安全压缩 |
| 会话只提供列表和最近历史 | 缺少中文全文搜索、分支、完整删除和产物快照 | WP05 会话生命周期 |
| 工具主要按模式和关键词静态裁剪 | 新工具会增加 Schema 开销，关键词规则持续膨胀 | WP06 Registry 与 Tool Search |
| 子代理以检索和工作单元为主 | 缺少统一的副作用并行规划、预算预留和通用批处理 | WP07 编排与受限批处理 |
| 记忆和技能主要由用户或当前任务直接修改 | 缺少可审查、可回滚的持续学习通道 | WP08/WP09 学习提案与技能版本 |
| MCP/扩展需要更强进程边界 | 第三方崩溃、恶意代码或凭据泄露可能影响 Core | WP10 插件宿主与统一审批 |
| 故障、供应链和发布数据需要统一归档 | 升级效果难以持续比较，依赖与发布风险不易追踪 | WP11 评测与供应链 |

---

## 3. Hermes 能力采用清单

| Hermes 能力 | 决策 | LawBuddy 适配方式 | 优先级 |
| --- | --- | --- | --- |
| 工具调用先落库、后执行 | 直接采用思想 | Tool Journal；持久化失败禁止执行 | P0 |
| Turn lease、watermark、CAS | 适配采用 | 租约所有者、fencing token、压缩和恢复写保护 | P0 |
| ProviderProfile | 适配采用 | 类型化内部 Profile，不开放第三方进程内 Provider | P0 |
| 错误分类、流式工具参数累加、看门狗 | 采用并整合现有实现 | 保留现有重试，统一错误码和首帧/帧间/总时长治理 | P0 |
| 确定性裁剪后 LLM 摘要 | 受限采用 | 默认确定性；LLM 摘要遵守外发策略，摘要不成为事实来源 | P0 |
| 日志脱敏和诊断包 | 适配采用 | 内容无关 Span、严格脱敏、默认仅本地 | P0 |
| FTS5 会话搜索和原文锚点 | 采用 | 中文 trigram/BM25/LIKE，返回真实消息而非只返回摘要 | P1 |
| 会话分支和谱系删除 | 采用并收紧 | 不复制授权和副作用；删除同时清理索引、快照和前端状态 | P1 |
| Tool Search 渐进披露 | 采用 | 中文工具检索；策略过滤先于相关性排序 | P1 |
| 只读/路径不重叠工具并行 | 采用 | Registry 副作用元数据驱动并行，写入走暂存和原子提交 | P1 |
| 子代理独立上下文和并行工作流 | 采用并收紧 | 默认只读、深度 1、共享硬预算、结果重新验引 | P1 |
| 脚本通过 RPC 串联工具 | 改造采用 | 受限 Tool Batch DSL，不开放默认任意 Python | P1 |
| 工作区检查点 | 适配采用 | 加密产物快照和回滚，不快照或覆盖原件 | P1 |
| 后台记忆/技能复审 | 改造采用 | 只产生带来源提案，人工批准、版本 diff 和回滚 | P1 |
| 技能隔离落盘、扫描、哈希 | 采用并收紧 | Markdown 技能暂存区；危险项禁止强装；许可证审查 | P1 |
| MCP trust gate 和插件宿主 | 采用并收紧 | 每服务独立子进程、最小能力、默认禁网、统一审批 | P2 |
| Docker 等执行后端 | 接口预留 | 本地后端保持默认；Docker 仅作为不可信扩展的可选隔离 | P2 |
| OTLP 和共享指标 | 仅保留本地接口 | 默认关闭外部导出；禁止案件内容和密钥进入指标 | P2 |
| Cron 自动任务 | 受限采用 | 本地引用重验、失效检查和提醒；外发及写操作仍需审批 | P3 |
| Provider 凭据池与模型回退 | 受限采用 | 仅用户显式配置的同用途路由；禁止跨服务商错误复用 Key | P2 |
| 多消息平台 Gateway | 不迁移 | 保持 Electron/Core 单一产品边界 | — |
| 云终端和 Serverless 后端 | 不迁移 | 与本地优先目标不一致 | — |
| `.env` 和明文密钥回退 | 不迁移 | 继续统一密钥环和 fail-closed | — |
| 明文消息存储 | 不迁移 | 新内容继续位于加密数据库/加密 blob | — |
| 默认无限迭代 | 不迁移 | 保留并统一共享硬预算 | — |
| 自动启用自修改技能 | 不迁移 | 只允许提案和人审 | — |
| 通用训练轨迹平台 | 不迁移 | 评测仍聚焦 WorkflowArena LAW | — |

---

## 4. 目标架构

```text
React Renderer
  └─ 共享 contracts / Electron IPC / preload
      └─ Python Core service.py
          ├─ 领域守门：policy / citations / Claim Ledger / case scope
          ├─ Agent Runtime V3
          │   ├─ Turn Controller + Tool Journal + Lease/Fencing
          │   ├─ ProviderProfile + Error Classifier + Stream Watchdog
          │   ├─ Tool Registry + Chinese Tool Search + Tool Batch
          │   ├─ Scheduler + Subagent + Shared Budget Reservation
          │   ├─ Conversation + Context Compaction + Artifact Snapshot
          │   └─ Learning Proposal + Skill Versions + Plugin Host
          ├─ 现有工作账本 / 阅读账本 / 证据账本 / 交付控制器
          └─ 加密 SQLite / 加密 blob / 系统密钥环
```

### 4.1 运行时兼容策略

- 新增 `runtimeVersion=v3`，只用于新任务。
- V1/V2 任务继续按原运行时和原检查点恢复，禁止升级途中改变其语义。
- `agent.runtimeVersion` 在 V3 完整验收前继续默认 `v2`；验收后才允许切换默认值。
- V50 的 `agent_runtime_state.runtime_version` CHECK 仅允许 `v1/v2/legacy`，V51 必须通过事务内表重建加入 `v3`，同时保留旧行和旧迁移文本。
- 同步更新 `work_ledger.RUNTIME_VERSIONS`、Runner 恢复白名单、共享 contracts 和评测选项。

### 4.2 统一执行路径

所有工具最终必须经过：

```text
工具发现/参数校验
  → 模式、案件、能力和审批过滤
  → Tool Journal staged 持久化
  → 获取/确认租约
  → 执行工具
  → 结果加密落库
  → succeeded / failed / unknown 对账
  → 发布 UI 事件
```

Tool Search、子代理、MCP、插件和 Tool Batch 不得建立旁路。

---

## 5. 详细改造工作包

## WP01：工具 Journal、租约与恢复幂等（P0）

### 目标

消除“工具已产生副作用，但检查点尚未保存”的不确定窗口，使任何崩溃都能解释为已提交、可重试或必须人工核实的状态。

### 改造点

- 新增 `agent/tool_journal.py`：负责工具意图、执行状态、结果引用、租约和对账。
- 新增 `agent/tool_runtime.py`：统一自主 Loop、固定 Graph、Sandbox、MCP 和子代理执行入口。
- 新增 `agent/tool_registry.py`：登记 Schema、中文别名、读写属性、副作用、幂等性、审批和并发元数据，供 WP06 复用。
- 改造 [autonomous.py](../services/core/lawbuddy/agent/autonomous.py)、`loop.py`、`graph.py`、`worker.py` 和 [sandbox.py](../services/core/lawbuddy/sandbox.py)。
- 文件写入、交付物写入、合同结果和记忆写入采用暂存后原子提交；正式交付仍只能由交付控制器写入。

### 状态机

```text
staged → executing → succeeded
                   → failed_retryable
                   → failed_final
                   → unknown
                   → cancelled
```

- `staged` 落库失败：不得执行工具。
- `executing` 后崩溃：幂等只读工具可按策略重试；非幂等工具进入 `unknown`。
- `unknown`：禁止盲目重放，提供“查看目标状态、标记已发生、重新执行、忽略”人工处置入口。
- 使用 `owner_id + fencing_token` 阻止过期 Worker 在恢复后提交旧结果。
- 结果和参数可含敏感内容，只能存于加密数据库或加密 blob；事件仅携带 ID、状态、计数和错误码。

### 验收

- 在执行前、副作用后未提交、结果提交后三个崩溃点强杀 Core。
- 重复恢复不得产生重复文件、记忆、合同动作或产物。
- 数据库写入失败时副作用次数为 0。
- 过期 Worker 不能覆盖新 Worker 结果。
- 非幂等 `unknown` 自动重放次数为 0，且 UI 可见人工处置入口。
- V1/V2 旧任务恢复路径不变，V3 新任务全部经过 Journal。

## WP02：ProviderProfile、错误分类与流式看门狗（P0）

### 目标

在不改变现有模型设置和密钥体系的前提下，统一服务商差异、故障处理和流式工具调用可靠性。

### 改造点

- 新增 `agent/provider_profile.py`，将 [model_registry.py](../services/core/lawbuddy/agent/model_registry.py) 中字典配置逐步收敛为类型化 Profile。
- Profile 包含端点、认证方式、API 模式、模型目录、视觉/流式能力、请求/响应 hooks 和错误分类 hooks。
- 改造 [model_gateway.py](../services/core/lawbuddy/agent/model_gateway.py)，保留已有退避重试、推理档位适配、断连续写和工具参数恢复。
- 新增 `agent/error_classifier.py` 与 `agent/stream_watchdog.py`。

### 统一错误类别

`auth、billing、permission、rate_limit、context_overflow、invalid_request、format、timeout、disconnect、stale_stream、provider_unavailable、persistence、cancelled`。

处理要求：

- 认证/权限失败不盲目重试，不回显 Key。
- 上下文溢出优先触发安全压缩，不得丢弃引证和未提交工作状态。
- 首 Token、帧间静默和请求总生命周期分别监控；取消可穿透等待和网络读取。
- 不完整工具参数不得执行；整轮重试必须遵守 Journal 和预算。
- 模型回退仅限用户显式配置的路由，禁止跨服务商错误复用 Key。
- Key 更新后新任务使用新值；进行中的请求按安全边界结束，旧值不得写回。
- 所有窗口读取同一设备级配置版本和 `updatedAt`，只返回 `configured` 等非敏感元数据。

### 验收

- 覆盖 400/401/402/403/413/429/5xx、首帧前断连、正文断连、工具参数断连和流式停滞。
- 错误分类、重试次数、回退原因可通过非敏感事件追踪。
- 新 Provider 只需实现 Profile 和 conformance 测试，不修改主 Loop。
- 密钥环不可用时仍 fail-closed，日志、事件和错误正文无密钥。

## WP03：结构化可观测性与本地诊断（P0）

### 目标

能够重建一次任务、模型调用、工具执行和父子 Agent 的完整技术链路，同时不把案件内容变成日志或指标。

### 改造点

- 新增 `agent/observability.py`，改造 `tasks.py`、`audit.py`、`notify.py` 和 `coreProcess.ts`。
- 统一 `trace_id、span_id、parent_span_id、task_id、conversation_id、call_id、subagent_id、runtime_version`。
- Span 只记录耗时、状态、错误码、重试、Token、预算和恢复结果；不记录查询词、文件路径、工具参数、引文和错误响应正文。
- 业务审计与技术 Span 分离：审计事件不得因队列满而丢失；非关键性能队列必须有容量、丢弃计数和告警。
- 本地诊断包默认只包含版本、配置元数据、迁移版本、Span 和脱敏错误码，不包含案件原文、密钥和完整消息。
- 外部 OTLP 默认关闭；后续如开放，只能由用户显式配置目的地并经过字段白名单。

### 验收

- 可由 trace 重建主 Agent、子 Agent、模型和工具的父子关系。
- 扫描日志、事件、审计 metadata 和诊断包，敏感原文与密钥命中为 0。
- 关闭所有外部遥测不影响任务、恢复和审计。
- 性能队列发生丢弃时有明确计数，不得静默表现为“无事件”。

## WP04：引证安全的上下文压缩与归档（P0）

### 目标

吸收 Hermes“先确定性裁剪、再结构化摘要、失败可回退”的压缩管线，同时保证法律事实仍由原文和账本支撑。

### 改造点

- 新增 `agent/context_compaction.py`，替代 `_compact_messages` 的继续膨胀。
- 第一阶段：将超大旧工具结果裁剪为加密内容引用，保留工具配对、调用 ID、文档版本和结果哈希。
- 第二阶段：按 Token 保护 system、最近轮次、未提交单元格、待核实项、当前任务状态和证据索引。
- 第三阶段：默认生成确定性导航摘要；LLM 结构化摘要默认关闭，启用时必须遵守现有模型外发和脱敏策略。
- 压缩前完整 transcript 加密归档；压缩写入使用 `watermark + CAS + lease`，过期压缩任务不能覆盖新对话。
- 摘要只保存事实的 `claim_id/source_ref`，不能将摘要文本本身作为 `supported` 依据。
- 文档版本变化、OCR 低置信度或引用失配时继续使用 `stale/needs_human`。

### 验收

- 多轮强制压缩后无孤立 tool 消息、无丢失的未提交工作状态。
- 所有 Claim 仍能经 `verify_quote` 回到原文。
- 压缩失败返回未压缩 canonical 消息，不产生“越压越大”或半写入状态。
- 过期压缩 Worker 无法覆盖新会话。
- 摘要中的事实不会因再次压缩而升级为已支持事实。

## WP05：历史会话搜索、分支、彻底删除与产物快照（P1）

### 目标

将现有会话归档升级为可检索、可分支、可恢复且可彻底删除的会话系统。

### 改造点

- 改造 [conversation.py](../services/core/lawbuddy/conversation.py)，新增 `conversation_search.py`。
- 会话搜索使用中文 trigram/BM25、短查询 LIKE 回退和案件强制过滤，返回真实消息、消息 ID、会话 ID 和定位锚点。
- 搜索默认不调用 LLM 总结；如提供摘要，必须同时提供真实消息入口。
- 分支只复制锚点前的会话消息和必要引证，不复制任务、审批、授权、预算、Journal 或副作用。
- 分支产生新的 `conversation_id`；历史引用再次使用时重新验证文档版本。
- 产物快照只覆盖工作副本和交付物，使用加密 blob 和内容哈希；不得覆盖原件。
- 删除前取消活动任务和后台订阅，清理会话、消息、FTS、检查点、待答问题、预览注册和 Renderer 持久化快照。
- 删除后不得通过新会话、缓存或 FTS 自动重建；案件原件和不含内容的最小审计墓碑按现有审计规则保留。

### UI

在现有会话侧栏增加可见的“搜索、分支、删除”入口；删除确认说明删除范围，不提供伪软删除。

### 验收

- 中文查询命中正确会话且不跨案，点击结果能定位真实消息。
- 分支后原会话不变，授权和工具副作用不被复制。
- 删除后数据库、FTS、前端快照和后台订阅均无会话残留。
- 产物回滚只影响工作副本，原件哈希不变。

## WP06：统一 Tool Registry 与中文 Tool Search（P1）

### 目标

降低工具 Schema 对上下文的固定占用，并用元数据替代不断扩张的模式和关键词特判。

### Registry 元数据

每个工具至少声明：

- 名称、版本、共享 Schema、中文描述和别名。
- 适用模式、任务阶段和交付类型。
- `read_only、effect_kind、idempotent、concurrency_scope`。
- 案件范围、数据敏感级别、审批能力和来源要求。
- 是否 eager、是否可由子代理使用、是否允许批处理。

### 检索与执行

- 高频读页、引证、人工提问和交付状态工具保持 eager。
- MCP、插件和低频工具通过 `tool_search/tool_describe/tool_call` 渐进披露。
- 检索采用精确名称加权、中文单字/bigram/trigram、BM25 和法律同义词。
- 模式、案件、权限和数据范围过滤必须先于相关性排序，模型不能搜索到无权调用的工具。
- 桥接调用仍进入 WP01 的 Tool Runtime，不得绕过参数校验、审批、Journal 和预算。
- 未命中时返回可用类别和人工可理解的降级建议，不得虚构工具。

### 验收

- “读第几页、生成图表、查历史、安装连接器”等中文表达能命中目标工具。
- 相对 M0 冻结基线，Schema Token 占用可测量且下降；法律任务完成质量不得回退。
- 无权工具即使名称精确命中也不能执行。
- 模式未登记时使用保守白名单，不再默认全量下发所有高风险工具。

## WP07：子代理并行、共享预算与受限 Tool Batch（P1）

### 目标

吸收 Hermes 并行和编排优势，同时避免并发写入、预算逃逸和子代理结论污染正式交付。

### 改造点

- 改造 `scheduler.py`、`worker.py`、`budget.py` 和 `concurrency.py`。
- 只读工具可并行；写入工具仅在路径和工作单元不重叠时进入独立暂存区。
- 正式交付物、合同工作副本和案件记忆仍由主任务在验引后提交。
- 主子 Agent 使用数据库原子“预算预留—结算—释放”，统一轮次、模型、工具、Token 和墙钟。
- 默认子代理只读、深度 1；取消和超时传播到全部子级。
- 子代理结果先 staging，主 Agent 采用前重新校验来源和版本。
- 新增受限 Tool Batch DSL，支持声明式并行只读调用、结果聚合和错误收集；不默认开放任意 Python 或 Shell。
- 产物版本使用加密快照，支持恢复和 diff，不复制授权或密钥。

### 验收

- 并发压测不超共享预算、无计数丢失，取消后无孤儿子任务。
- 单分支失败不拖垮其他只读工作，失败原因可见。
- 子代理不能写正式交付物、越案读取或直接提升 Claim 状态。
- Tool Batch 中任一调用仍分别经过策略、审批和 Journal。
- 重复提交不产生重复行、文件或产物版本。

## WP08：记忆 Provider 与受控持续学习（P1）

### 目标

获得跨会话学习和用户偏好积累能力，同时保持“没有来源不进案件记忆”。

### 改造点

- 新增 `agent/learning.py` 和本地 `MemoryProvider` 接口。
- 在任务完成、用户明确更正或材料版本变化后触发后台复审，不机械照搬固定轮数触发。
- 后台复审使用隔离会话，不写主会话，不直接改变已启用记忆。
- 输出统一进入 `learning_proposals`，包含类型、baseHash、diff、来源、理由、影响范围和扫描结果。
- 案件事实提案必须附文档、版本、页码和原文短引，并重新执行引用校验。
- 用户画像只从语言、格式、模型和权限偏好形成，禁止携带案件事实。
- 支持批准、拒绝、编辑后批准、版本回滚；baseHash 变化后必须重新审查。
- 用户主动记忆仍沿用现有 [memory.py](../services/core/lawbuddy/memory.py) 的来源验证和案件隔离。
- 不接入依赖云服务的用户建模；外部 MemoryProvider 仅在未来通过本地优先和隐私评审后接入。

### 验收

- 未批准提案不能进入 Prompt、召回或正式记忆。
- 无来源案件事实提案被存储层拒绝。
- 文档换版后相关提案和记忆进入 `stale`，不能继续作为有效事实。
- 并发修改导致 baseHash 冲突时不能覆盖新值。
- 拒绝和回滚后无残留索引或快照复活。

## WP09：技能暂存、版本、评审与安全 Curator（P1）

### 目标

将 Hermes 技能自我改进转化为可审计、可回滚的 LawBuddy 技能生命周期。

### 改造点

- 改造 [skills.py](../services/core/lawbuddy/skills.py)，所有安装和自动修改先进入隔离暂存区。
- 保持技能为 Markdown/frontmatter 提示词包；脚本和可执行扩展不得通过技能通道安装。
- 暂存阶段执行结构校验、大小限制、提示注入、凭据、危险指令、HTML/XML、许可证和敏感内容扫描。
- 危险项不得通过“强制安装”绕过；外部页面只保存 HTTPS 快照、来源和内容哈希。
- 已启用技能不可被后台任务直接覆盖；批准后创建新 `skill_version`，支持 diff、回滚和删除。
- Curator 的确定性过期、去重和归档可自动执行；语义合并和启用变更必须人审。
- 技能相关评测只使用确定性 fixture 和 WorkflowArena LAW，不引入其他领域评测。

### 验收

- 未审批版本不能被注入 Prompt。
- 同名不同内容不能静默覆盖，baseHash 冲突必须重审。
- 注入、凭据和危险内容样本被阻断，且日志不保存敏感正文。
- 回滚后只恢复指定提示词版本，不影响案件、记忆和工具授权。

## WP10：MCP/插件隔离、统一审批与执行后端（P2）

### 目标

在需要扩展生态时，避免第三方代码直接破坏 Core、案件数据和凭据边界。

### 改造点

- 新增 `agent/mcp_runtime.py` 和 `agent/plugin_host.py`。
- 每个服务使用独立子进程和受限 RPC，禁止在 Core 进程导入第三方插件代码。
- Manifest 声明工具、读写副作用、网络、文件范围、所需凭据、许可证和哈希；安装前进行许可证与安全审查。
- 默认禁用、默认禁网、默认无案件读取范围；用户按服务器、工具和用途授权。
- 所有副作用统一进入 `policy.py` 和 Tool Journal；审批绑定服务器、工具、参数哈希、案件、数据摘要和会话范围。
- 子进程环境默认清空敏感变量；密钥只在真实外部请求的最短生命周期内通过受控通道提供，不写命令行、环境快照或日志。
- 插件崩溃、超时和退出不得拖死 Core；卸载时清理进程、索引、授权和缓存。
- 执行后端只抽象 `local` 与后续可选 Docker；本地仍是默认，暂不实现云终端。
- Docker 若引入，必须评估网络、挂载、capability、PID、临时文件和凭据代理，不得因“容器内”自动跳过审批。

### 验收

- 恶意或崩溃插件不能拖死 Core或读取未授权案件。
- 修改审批参数后授权立即失效。
- 未批准网络或写副作用次数为 0。
- 子进程环境、命令行、错误和日志中无密钥。
- 插件卸载后无工具残留、订阅残留或自动重启。

## WP11：评测、故障注入、供应链与可选本地自动化（P2/P3）

### 评测与故障注入

- 新增 `services/core/evals/m6_hermes_upgrade.py`，并扩展现有 `m3_harness.py`、`m3_security.py` 和 `evals/wfa_law/harness.py`。
- 新增故障：Journal 写失败、结果提交前强杀、过期 Worker、压缩 CAS 冲突、流式停滞、Provider 回退、会话删除竞态、提案 baseHash 冲突、插件崩溃和 MCP 超时。
- 所有新能力先以 shadow/隔离工作空间运行，再进入 V3 默认路径。
- WFA 对比沿用同一模型、同一输入快照和同一评分器，报告中位数、最差值、方差、Token、耗时、重复读取、恢复和交付状态。
- 报告面向普通用户解释“是否更可靠、是否更省、是否出现回退”，不只输出技术日志。

### 供应链与发布

- Python/npm 依赖锁定版本和哈希；新增依赖必须说明许可、离线能力、维护状态和数据行为。
- 生成 SBOM、第三方 NOTICE 和安装包校验信息；直接复制 Hermes 代码时保留 MIT 版权和许可声明。
- 密钥扫描覆盖源码、构建目录、JS/Python bundle、source map、安装包、构建日志和诊断包，命中即阻断。
- 依赖漏洞扫描和许可证扫描应成为发布门禁；不能只报告而继续发布高风险项。
- 不引入 Hermes 全量依赖，只按实际模块选择薄实现或许可兼容依赖。

### 可选本地自动化（P3）

仅在前述能力稳定后开放：

- 定时引用重验、文档版本失效检查、待核实事项提醒和本地导出提醒。
- 每个任务绑定案件、运行时、预算和审批上下文。
- 无人值守时禁止新增外发和高风险写入；需要审批的任务进入等待而不是绕过。

---

## 6. 数据库与共享协议设计

### 6.1 迁移规划

| 迁移 | 计划内容 | 说明 |
| --- | --- | --- |
| V51 | `tool_call_journal`、`tool_result_blobs`、`task_leases`、`runtime_spans`、`task_budget_reservations`、`subagent_runs` | 工具可靠性、预算和观测底座 |
| V51 同事务 | 重建 `agent_runtime_state`，允许 `v3` | 保留旧行；不得修改 V50 SQL 文本 |
| V52 | `conversation_fts`、`conversation_branches`、`context_archives`、`artifact_snapshots` | 会话、压缩归档和产物版本 |
| V53 | `learning_proposals`、`skill_versions`、`plugin_installations`、`plugin_runtime_state` | 学习和扩展默认均为未启用/待审批 |
| V54（可选） | `local_jobs` | 仅本地自动化，默认无任务 |

实际拆分时只递增版本号，不覆盖已发布迁移。所有新表必须有删除级联、索引和敏感字段责任说明。

### 6.2 关键字段约束

- `tool_call_journal`：`task_id + call_id` 唯一；另含 `args_hash、effect、idempotency_key、status、attempt、owner_id、fencing_token、result_ref、error_code`。
- `runtime_spans`：禁止查询词、路径、原文、引文、工具参数和错误正文。
- `learning_proposals`：必须包含 `base_hash、proposal_diff、status、source_ref、scan_result`；案件事实缺来源时数据库拒绝。
- `conversation_fts`：必须带 `case_id`，所有查询在 SQL 层强制过滤。
- `skill_versions`：启用版本唯一；未经批准的版本不能标记 active。
- 任何新表都不得保存明文 API Key、KEK、DEK 或完整密钥环内容。

### 6.3 共享 contracts 新类型

计划在 [packages/contracts/src/index.ts](../packages/contracts/src/index.ts) 增加：

- `ToolDescriptor、ToolCallState、ToolJournalEntry、ToolBatchRequest/Result`
- `ClassifiedModelError、ProviderHealth、StreamWatchdogEvent`
- `RuntimeSpan、RuntimeTraceSummary`
- `ConversationSearchHit、ConversationBranch、ArtifactSnapshot`
- `SubagentRun、SharedBudgetSnapshot`
- `LearningProposal、SkillVersion、SkillScanResult`
- `PluginCapability、PluginInstallState、LocalJob`

计划新增或扩展 RPC：

- `conversation.search / branch / delete / snapshots / rollback`
- `agent.toolCall.get / resolveUnknown / trace`
- `agent.toolSearch / toolDescribe / toolBatch`
- `model.health / routes / conformance`
- `learning.list / approve / reject / rollback`
- `skills.versions / approve / reject / rollback`
- `plugins.install / enable / disable / uninstall / status`
- `localJobs.*`（可选）

每个 RPC 必须同步修改 [service.py](../services/core/lawbuddy/service.py)、Electron 主进程、preload、共享 contracts、Renderer 和评测；不得只改一端，也不得回传密钥或敏感原文。

---

## 7. UI 与交互改造

保持现有布局和设计语言，只增加用户可操作、可理解的入口：

1. **恢复中心**：显示失败、`unknown`、待继续任务；提供查看状态、继续、标记已发生和取消。
2. **会话管理**：搜索、分支、彻底删除、产物版本和回滚。
3. **模型健康**：显示 Provider、 configured 状态、最近错误类别和回退原因，不显示密钥。
4. **学习提案**：显示 diff、来源、影响范围和扫描结果，支持批准、拒绝和回滚。
5. **子代理进度**：展示工作单元、状态和预算，不暴露无意义的内部工具查找过程。
6. **扩展审批**：明确服务器、工具、目标、数据范围和风险，不用笼统“允许全部”。
7. **诊断导出**：默认本地、明确包含内容、敏感字段不可勾选导出。

所有 AI 分析结果继续保留“关键事实请以原文为准”；`needs_human/conflict/stale` 必须同时以文字和图标表达，并提供原文定位入口。

---

## 8. 实施里程碑与依赖

| 里程碑 | 内容 | 依赖 | 出口条件 |
| --- | --- | --- | --- |
| M0 基线与备份 | 冻结 V50/V2 基线、WFA 结果、故障矩阵；加密备份和隔离恢复演练 | 无 | 基线可复现，恢复成功，所有新指标有测试编号 |
| M1 执行可靠性 | V51、Tool Registry/Journal/Runtime、租约、结构化 Span | M0 | 三类崩溃点通过；无重复副作用；V1/V2 恢复不变 |
| M2 模型与上下文 | ProviderProfile、错误分类、看门狗、引证安全压缩 | M1 | Provider conformance 和压缩/失配测试通过 |
| M3 会话与工具发现 | V52、会话搜索/分支/删除、中文 Tool Search、产物快照 | M1/M2 | 无跨案、无删除复活、桥接无审批旁路 |
| M4 编排 | 共享预算预留、子代理并行、Tool Batch | M1/M3 | 并发预算正确，取消完整，正式交付仅主控制器提交 |
| M5 受控学习 | V53、记忆提案、技能版本和 Curator | M2/M3 | 未审批不可启用，来源/冲突/回滚测试通过 |
| M6 扩展隔离 | 插件宿主、MCP 统一审批、可选 Docker 接口 | M1/M3/M5 | 恶意/崩溃扩展隔离测试通过，默认禁用 |
| M7 发布验收 | 全量 WFA、故障注入、SBOM、密钥与安装包扫描 | M1–M6 | 确定性门禁全通过，法律指标无回退，发布证据完整 |
| M8 可选自动化 | 本地重验和提醒 | M7 | 无人值守任务无外发和越权副作用 |

评测与实现同步推进，不能到 M7 才补测试。V3 只有在 M7 通过后才能成为新任务默认运行时。

### 实施进度（2026-11-17）

| 里程碑 | 状态 | 落地位置 | 验证 |
| --- | --- | --- | --- |
| M0 | 已完成 | 基线冻结于现有 V50 迁移与全部评测 | 迁移/契约/安全/循环基线等 20 个评测通过 |
| M1 | 已完成 | V51 迁移；`agent/tool_registry.py`、`tool_journal.py`、`tool_runtime.py`、`observability.py` | HUP-T-001~004 + registry-coverage / span-redaction / journal-idempotent |
| M2 | 已完成 | `agent/provider_profile.py`、`error_classifier.py`、`stream_watchdog.py`、`context_compaction.py` | HUP-T-005~007 |
| M3 | 已完成 | V52 迁移；`conversation_search.py`；`tool_search`/`tool_describe`/`tool_call` | HUP-T-008~011 |
| M4 | 已完成 | `agent/budget.py` 预留；`agent/tool_batch.py`、`agent/subagent_runs.py` | HUP-T-012~014 |
| M5 | 已完成 | V53 + `PROFILE_V2` 迁移；`agent/learning.py`、`skill_lifecycle.py` | HUP-T-015~017 |
| M6 | 已完成 | `agent/exec_backend.py`、`mcp_runtime.py`、`plugin_host.py`（默认禁用 + V54 持久化安装记录） | HUP-T-018 |
| M7 | 基本完成 | 离线部分：HUP-T-020（fast 档回归）、HUP-T-021（NOTICE/许可/源码密钥扫描/SBOM/构建产物扫描）；打包验收已实跑：`core:freeze` 重冻结（含全部 V3 模块）+ `core:smoke`（编码/RPC/OCR 冒烟）+ NSIS 安装包与签名 + win-unpacked 文本扫描（146 文件 0 命中）+ setup.exe 二进制扫描（sk-/PEM/JWT 全 0）+ 无 source map + 安装包内冻结 Core 实测 `automation.status`/`plugin.status`/`agent.recovery_center`/`diagnostics.snapshot`（latestMigration=54）全部可用；真实模型全量 WFA 已实跑（Ollama Cloud，v2 运行时，16 份协议 / 288 格 / 17 聚合题）：第一轮 composite **84.42**、branch **0.9826**、merge **0.7059**、merge_partial **0.8005**、consistency **0.9412**，2283s；第二轮 composite **11.98**、branch **0.2396**、merge **0.0**，1655s | 离线与打包门禁通过；**HUP-T-020 真实模型部分未达标**。第一轮唯一差异为聚合题 M11 返回 `{}`（真值 `A13`），merge 0.7059 低于 M0 基线 0.7647、`full_success` 变 False；第二轮为模型规划路径变异：全程未调用 `table_aggregate`（第一轮 14 次），只 `deliverable_patch` 一次写入 72 行后改向 `work/*.csv` 反复整表覆盖写（11 次），最终 `rowsFilled=72/288`、`answers.json` 全空，`turn_complete.reason=empty_answer`。两轮均以 `COMPLETED_WITH_WARNINGS` 结束且守门 `ok=false`。本轮据此修好两处确定性缺陷：① 新增聚合一致性收尾门禁（`_aggregate_consistency_gaps`，只复算模型自声明规格，不读评分器真值，不一致时先给修复指令再拦截）；② 修复 `WORK_UNITS_PENDING` 读错字段（`cellsPending` 在 `delivery.progress` 下，原代码读顶层导致 233 个未终态答案格未触发任何告警）。第二轮崩塌与上述门禁无关：该轮从未调用 `table_aggregate`，新门禁对其为空操作 |
| M8 | 已完成 | `local_automation.py`（只读巡检：引用重验统计、失效检查、待核实提醒）；`automation.status` / `automation.run` RPC；恢复中心「本地巡检」页签；开关 `agent.localAutomation` 默认关闭 | M8 gate（无人值守无外发与越权副作用：默认关闭拒绝执行、零状态变更、审计只记计数） |

新增门禁统一在 `services/core/evals/m6_hermes_upgrade.py`，共 28 个 gate：
方案矩阵 HUP-T-001~022 全覆盖（其中 T-014 拆为「Tool Batch 故障」正式用例与
「只读并发约束」补充门禁两条），另加 3 条基础设施门禁
（registry-coverage、span-redaction、journal-idempotent）、1 条 M8 门禁
（local-automation）和 1 条聚合一致性门禁（aggregate-gate：未提交/空对象判为不一致、
正确值判为一致、数值等价不误判、列表忽略顺序）。

V3 仍为**显式设置**（`agent.runtimeVersion = v3`），默认保持 v2，与本文 M7 约定一致。
新增能力默认关闭：`agent.toolSearch`（渐进披露）、`agent.learningProposals`（学习提案）、
`agent.plugins`（插件总闸）、`agent.mcpEnabled`（MCP 子系统）、`agent.execBackend`（仅 local 可用）、
`agent.localAutomation`（M8 本地巡检，关闭即拒绝执行且不留后台任务）；
`agent.contextCompressionV2` 默认开启，关闭时回退旧压缩但仍保留归档与 CAS 写保护。

### UI 入口（§7）

七个入口均已接入渲染层，且各自读取 Core 的真实数据（无 mock）：

| 入口 | 组件 | 数据来源 |
| --- | --- | --- |
| 恢复中心 | `RecoveryDialog.tsx` | `agent.recovery_center` / `agent.tool_journal` / `agent.subagent_runs` / `agent.resolve_tool_call` / `agent.resume` |
| 子代理进度 | `RecoveryDialog.tsx`（页签） | `agent.subagent_runs`（`subagent_runs` 表，任务结束时自动收敛未终态记录） |
| 本地巡检（M8） | `RecoveryDialog.tsx`（页签） | `automation.status` / `automation.run`（只读统计；开关 `agent.localAutomation` 在模型健康面板可切换） |
| 会话管理 | `ConversationDialog.tsx` | `conversation.search/list/branch/delete/snapshots/rollback/stats` |
| 模型健康 | `ModelHealthDialog.tsx` | `model.health` / `model.conformance` / `settings.*`（回滚开关） |
| 学习提案 | `LearningDialog.tsx` | `learning.proposals/approve/reject/rollback`、`skill.staging/approve_staging/versions/diff` |
| 扩展审批 | `PluginDialog.tsx` | `plugin.status/list/review/install/enable/disable/uninstall` |
| 诊断导出 | `ModelHealthDialog.tsx` | `diagnostics.snapshot` / `diagnostics.export`（Main 负责落盘，Core 只产出技术字段） |

回滚开关在「模型健康 → 新能力开关」中可逐项开关，关闭只影响后续行为、不删除已有数据。

---

## 9. 验收测试矩阵

| 编号 | 场景 | 必须通过的断言 |
| --- | --- | --- |
| HUP-T-001 | staged 落库失败 | 工具副作用为 0，任务错误可解释 |
| HUP-T-002 | 副作用后、结果提交前强杀 | 非幂等调用进入 `unknown`，恢复不重复执行 |
| HUP-T-003 | 过期 Worker 提交 | fencing token 拒绝旧结果，不覆盖新状态 |
| HUP-T-004 | V1/V2 旧任务恢复 | 运行时不变、预算连续、旧检查点可读 |
| HUP-T-005 | Provider 错误矩阵 | 分类和重试正确，认证不盲重试，Key 不混用 |
| HUP-T-006 | 流式停滞和参数断连 | 可取消、可恢复，不完整调用不执行 |
| HUP-T-007 | 多轮压缩 | 工具配对完整，Claim 可验引，旧压缩不能覆盖新会话 |
| HUP-T-008 | 中文会话搜索 | 命中真实消息、定位正确、跨案结果为 0 |
| HUP-T-009 | 会话分支 | 不复制授权、预算和副作用，原会话不变 |
| HUP-T-010 | 会话彻底删除 | DB、FTS、前端和订阅无残留，新会话不自动重建 |
| HUP-T-011 | Tool Search 越权尝试 | 搜索和执行均受策略限制，不能绕过审批 |
| HUP-T-012 | 主子代理并发预算 | 不超预算、无计数丢失、取消无孤儿任务 |
| HUP-T-013 | 子代理交付 | 只能暂存；验引后由主控制器唯一提交 |
| HUP-T-014 | Tool Batch 故障 | 单项失败可见，所有调用独立记账和审批 |
| HUP-T-015 | 未审批学习提案 | 不进入 Prompt、记忆、技能或召回 |
| HUP-T-016 | baseHash 并发冲突 | 旧提案不能覆盖新值，必须重审 |
| HUP-T-017 | 恶意技能和插件 | 危险项阻断，第三方崩溃不影响 Core |
| HUP-T-018 | 日志与诊断包扫描 | 密钥、案件原文、查询词和敏感参数命中为 0 |
| HUP-T-019 | 文档版本变化 | 旧引用、记忆、提案和缓存进入 `stale/needs_human` |
| HUP-T-020 | WFA LAW 全量回归 | 结构交付完整，引用和法律指标不差于 M0 冻结基线 |
| HUP-T-021 | 安装包与依赖 | 无明文密钥，SBOM/NOTICE 完整，依赖可复现 |
| HUP-T-022 | 外部遥测关闭 | 不影响任务、恢复、审计和用户可见进度 |

量化性能目标不预先编造。M0 冻结基线后，分别记录 Schema Token、每提交格 Token、重复读取率、恢复成功率和交付完整性；质量门禁优先于成本优化。

---

## 10. 回滚与兼容方案

### 10.1 独立开关

- `agent.runtimeVersion`：验收前默认 `v2`，V3 先用于隔离任务。
- `agent.toolSearch`：关闭后回到 Registry 生成的保守白名单，不回到全量高风险工具。
- `agent.contextCompressionV2`：关闭后使用现有确定性压缩，但保留归档和 CAS 修复。
- `agent.learningProposals`：关闭后禁止后台提案，不影响用户主动记忆和技能管理。
- `agent.plugins`：默认关闭，关闭时停止子进程并禁用工具，不删除审计证据。

### 10.2 回退顺序

出现法律质量或交付回退时依次停用：

```text
插件/MCP → 自动学习 → 并行子代理/Tool Batch → Tool Search → 新压缩
```

Tool Journal、持久化失败禁止执行、密钥和错误治理属于安全修复，不通过回退恢复到不安全行为。

### 10.3 数据回退

- 升级前创建加密备份并执行隔离恢复。
- 新迁移只增不改；降级程序不得删除新表或旧版本数据。
- V3 任务只能由支持 V3 的版本恢复，旧版本应明确提示“不支持该运行时”，不得误按 V1/V2 打开。
- `unknown` Journal 永久保留到处置完成，不因关闭功能自动重放或清除。

---

## 11. 主要风险与控制

| 风险 | 影响 | 控制 |
| --- | --- | --- |
| 将“持久化”误解为绝对 exactly-once | 外部系统已执行但本地未知时仍可能重复 | 明确采用 Journal + 幂等 + unknown 人审，不作绝对保证 |
| LLM 摘要丢失法律事实 | 结论无法回溯或旧事实被误用 | 摘要只作导航，Claim/SourceRef 和原文重验优先 |
| Tool Search 变成权限旁路 | 模型发现并执行敏感工具 | 策略过滤先于检索，所有桥接调用统一 Tool Runtime |
| 子代理扩大预算和写入面 | 成本失控、交付冲突、跨案读取 | 共享原子预算、默认只读、暂存和主控制器提交 |
| 自我改进污染长期指令 | 错误经验持续放大 | 提案、人审、baseHash、版本 diff、回滚和 WFA 回归 |
| 插件引入供应链和凭据风险 | Core 被控制、案件或密钥泄露 | 默认禁用、独立子进程、最小能力、哈希/许可证/安全审查 |
| 观测数据反向泄露案件内容 | 日志和诊断包成为新泄露面 | 字段白名单、内容无关 Span、严格扫描、默认不外发 |
| V3 迁移破坏旧任务 | 旧检查点和预算不可恢复 | 运行时钉住、V51 表重建事务、旧版本兼容测试 |
| 过度平台化 | 复杂度增加但法律质量无提升 | 按里程碑独立验收，不迁移消息平台、云终端和全量依赖 |

---

## 12. 完成定义（Definition of Done）

每个工作包只有同时满足以下条件才算完成：

- 实现、共享 Schema、RPC 五端和 UI 已同步，无平行临时类型。
- 数据库迁移只追加，旧数据、旧任务和旧运行时恢复验证通过。
- 原文引用、案件隔离、原件只读、人工核实和密钥 fail-closed 未弱化。
- 新增故障注入、单测和确定性评测全部通过，并记录失败项而非掩盖。
- `npm run typecheck`、相关构建、Python 相关评测和秘密扫描通过。
- WFA LAW 对比报告包含同模型同输入的质量、成本、恢复和交付结果。
- 日志、事件、审计 metadata、诊断包和安装包无敏感原文及明文密钥。
- 新能力可独立关闭和回滚，关闭后不会留下后台任务、订阅或自动重建状态。
- UI 提供真实可用的进度、审批、恢复和原文定位入口，不增加无交互能力的装饰面板。
- 文档、第三方 NOTICE、许可证和发布证据齐全。

---

## 13. 参考源码与既有方案

### LawBuddy

- [Agent Harness 优化升级改造方案](agent-harness-upgrade-plan.md)
- [A-03 契约与迁移设计](agent-harness-a03-schema-runtime.md)
- [A-04 验收矩阵与故障注入](agent-harness-a04-acceptance-matrix.md)
- [WorkflowArena LAW 技术优化方案](wfa-law-optimization-plan.md)
- [自主 Agent Loop](../services/core/lawbuddy/agent/autonomous.py)
- [模型网关](../services/core/lawbuddy/agent/model_gateway.py)
- [任务事件与检查点](../services/core/lawbuddy/agent/tasks.py)
- [会话持久化](../services/core/lawbuddy/conversation.py)
- [引用校验](../services/core/lawbuddy/citations.py)
- [记忆管理](../services/core/lawbuddy/memory.py)
- [技能管理](../services/core/lawbuddy/skills.py)
- [工作空间与 Shell 边界](../services/core/lawbuddy/sandbox.py)
- [密钥环](../services/core/lawbuddy/keyring.py)

### Hermes Agent（固定提交）

- [AIAgent 与运行时参数](https://github.com/NousResearch/hermes-agent/blob/6ce01ea637348b5ed9d3892e5d4f74863fa2c908/run_agent.py#L215-L289)
- [工具副作用前持久化](https://github.com/NousResearch/hermes-agent/blob/6ce01ea637348b5ed9d3892e5d4f74863fa2c908/agent/turn_tool_round.py#L49-L171)
- [ProviderProfile](https://github.com/NousResearch/hermes-agent/blob/6ce01ea637348b5ed9d3892e5d4f74863fa2c908/providers/base.py#L141-L205)
- [上下文压缩](https://github.com/NousResearch/hermes-agent/blob/6ce01ea637348b5ed9d3892e5d4f74863fa2c908/agent/context_compressor.py#L5245-L5360)
- [Tool Search](https://github.com/NousResearch/hermes-agent/blob/6ce01ea637348b5ed9d3892e5d4f74863fa2c908/tools/tool_search.py#L347-L382)
- [后台学习复审](https://github.com/NousResearch/hermes-agent/blob/6ce01ea637348b5ed9d3892e5d4f74863fa2c908/agent/background_review.py#L1192-L1232)
- [Provider 密钥引用实现](https://github.com/NousResearch/hermes-agent/blob/6ce01ea637348b5ed9d3892e5d4f74863fa2c908/hermes_cli/config_secret_refs.py#L224-L249)
- [Vault 文件回退实现](https://github.com/NousResearch/hermes-agent/blob/6ce01ea637348b5ed9d3892e5d4f74863fa2c908/agent/vault_store.py#L314-L338)
- [SessionDB 消息结构](https://github.com/NousResearch/hermes-agent/blob/6ce01ea637348b5ed9d3892e5d4f74863fa2c908/hermes_state_common.py#L539-L570)
- [MIT License](https://github.com/NousResearch/hermes-agent/blob/6ce01ea637348b5ed9d3892e5d4f74863fa2c908/LICENSE)
