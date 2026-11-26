# LawBuddy Harness A-03：契约与迁移设计

| 项目 | 内容 |
| --- | --- |
| 依据 | [agent-harness-upgrade-plan.md](agent-harness-upgrade-plan.md) 阶段 A-03、§4.1–§4.10 |
| 状态 | 已实现并随代码落地；本文补齐字段责任、共享协议差异、运行时兼容矩阵与事件字段清单 |
| 数据基线 | V49 → V50；V50 只新增表与索引，不修改 V1–V49 |
| 生成日期 | 2026-11-16 |

## 1. 结论

- V50 迁移已落地，四张表与五个索引与方案 §4.1 一致，见 [migrations.py](../services/core/lawbuddy/storage/migrations.py)。
- 新增字段均有唯一写入方与明确读取方；敏感内容只落在加密数据库，不进入事件 payload 与普通日志。
- 运行时版本在任务创建时钉住，恢复时不允许跨 V1/V2；旧任务按 `legacy` 处理，不回填工作单元或事实答案。
- 共享协议、Core、IPC、preload、主进程与 Renderer 的字段已同步，前端不自建平行类型。

## 2. V50 SQL 草案

以下 SQL 与 `services/core/lawbuddy/storage/migrations.py` 中 `V50__AGENT_HARNESS` 完全一致。

```sql
-- Agent Harness 优化升级（LB-HARNESS-2026-01 §4.1）：
-- 工作单元 / 答案格 / 交付物 / 运行时状态四张表，把大卷宗任务的工作全集与
-- 交付进度从模型上下文挪到数据库。只新增表与索引，不回填旧任务事实状态；
-- 旧任务（V1 运行时）不写这些表，仍按原检查点恢复。

-- 工作单元：任务契约编译出的最小可调度工作项（文档 × 问题、被告 × 字段等）。
CREATE TABLE agent_work_units (
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  work_unit_id TEXT NOT NULL,
  scope_type TEXT NOT NULL,
  scope_json TEXT NOT NULL DEFAULT '{}',
  dependencies_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','leased','extracting','staged','verified','committed','needs_human','blocked')),
  priority INTEGER NOT NULL DEFAULT 100,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  cost_json TEXT NOT NULL DEFAULT '{}',
  output_ref TEXT,
  last_error_code TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (task_id, work_unit_id)
);

CREATE INDEX idx_agent_work_units_status ON agent_work_units(task_id, status, priority);
CREATE INDEX idx_agent_work_units_priority ON agent_work_units(task_id, priority, updated_at);

-- 答案格：交付表的一格（行主键 + 列），带来源与证据状态。
CREATE TABLE agent_cell_results (
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  artifact_name TEXT NOT NULL,
  cell_key TEXT NOT NULL,
  value_json TEXT NOT NULL DEFAULT 'null',
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','committed','needs_human','blocked','stale')),
  document_id TEXT,
  version_id TEXT,
  page_no INTEGER,
  quote TEXT,
  reason_code TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (task_id, artifact_name, cell_key)
);

CREATE INDEX idx_agent_cell_results_status ON agent_cell_results(task_id, status, updated_at);
CREATE INDEX idx_agent_cell_results_version ON agent_cell_results(task_id, document_id, version_id);

-- 交付物登记：主/派生交付物的行覆盖与原子写入结果（只记哈希与路径，不存内容）。
CREATE TABLE agent_artifacts (
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','partial','complete','invalid')),
  rows_expected INTEGER NOT NULL DEFAULT 0,
  rows_committed INTEGER NOT NULL DEFAULT 0,
  content_sha256 TEXT,
  storage_path TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (task_id, name)
);

CREATE INDEX idx_agent_artifacts_status ON agent_artifacts(task_id, status, updated_at);

-- 运行时状态：运行时版本钉住与预算计数（恢复时不得跨 V1/V2）。
CREATE TABLE agent_runtime_state (
  task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
  runtime_version TEXT NOT NULL DEFAULT 'v1'
    CHECK (runtime_version IN ('v1','v2','legacy')),
  schema_version INTEGER NOT NULL DEFAULT 1,
  rounds_used INTEGER NOT NULL DEFAULT 0,
  tool_calls_used INTEGER NOT NULL DEFAULT 0,
  model_calls_used INTEGER NOT NULL DEFAULT 0,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  explore_rounds_used INTEGER NOT NULL DEFAULT 0,
  started_at TEXT,
  state_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);
```

迁移规则：

1. 单事务执行；失败整体回滚，不留下半迁移表。
2. 不修改 V1–V49，不删除旧数据。
3. 旧任务不猜测工作单元，不回填事实答案。
4. 任务删除时四张表按 `task_id` 外键级联清理。

## 3. 字段责任表

### 3.1 `agent_work_units`

| 字段 | 来源 | 写入方 | 读取方 | 含敏感内容 | 进事件 | 进日志 |
| --- | --- | --- | --- | --- | --- | --- |
| `task_id` | 任务创建 | `work_ledger.seed_units` 等 | 调度器、账本、UI | 否（ID） | 是（ID） | 是（ID） |
| `work_unit_id` | 契约编译 | `seed_units` | 调度器、子代理、交付控制器 | 否 | 是 | 是 |
| `scope_type` | 契约编译 | `seed_units` | 调度器、子代理、交付控制器 | 否 | 否 | 否 |
| `scope_json` | 契约编译（artifact / cellKey / documentId 等） | `seed_units` | 子代理、交付控制器 | 仅 ID 与格键，不含原文 | 否 | 否 |
| `dependencies_json` | 契约编译 | `seed_units` | 调度器 | 否 | 否 | 否 |
| `status` | 状态机 | `lease_unit` / `transition_unit` / `advance_unit` | 调度器、UI | 否 | 是 | 是 |
| `priority` | 契约编译 | `seed_units` | 调度器 | 否 | 否 | 否 |
| `attempts` | 租约 | `lease_unit` | 调度器、UI | 否 | 是（`work_unit_update`） | 是 |
| `max_attempts` | 契约编译 | `seed_units` | 调度器、租约 | 否 | 否 | 否 |
| `cost_json` | 运行期成本 | `transition_unit` | 调度器、评测 | 计数 | 否 | 否 |
| `output_ref` | 子代理暂存 | `transition_unit` | 交付控制器、评测 | 暂存相对路径，非原文 | 否 | 否 |
| `last_error_code` | 失败分类 | `transition_unit` / `scheduler.record_failure` | UI、调度器 | 错误码 | 是（`lastErrorCode`） | 是（错误码） |
| `created_at` / `updated_at` | 数据库时钟 | `seed_units` / 状态迁移 | 评测、UI | 否 | 否 | 否 |

### 3.2 `agent_cell_results`

| 字段 | 来源 | 写入方 | 读取方 | 含敏感内容 | 进事件 | 进日志 |
| --- | --- | --- | --- | --- | --- | --- |
| `task_id` | 任务创建 | `commit_cell` / 文件同步 | 交付控制器、UI | 否（ID） | 仅计数 | 是（ID） |
| `artifact_name` | 契约编译 | `commit_cell` / 文件同步 | 交付控制器、UI | 否 | 仅计数 | 是 |
| `cell_key` | 契约编译（行主键 + 列） | `commit_cell` / 文件同步 | 交付控制器、UI | 否（键名与主键值） | 否 | 否 |
| `value_json` | 子代理/模型结论 | `commit_cell` | 交付控制器、UI、评测 | 答案值（法律判断），不落事件/日志 | 否 | 否 |
| `status` | 状态机 | `commit_cell` / 文件同步 | 交付控制器、UI | 否 | 仅计数 | 是（计数） |
| `document_id` / `version_id` / `page_no` | 来源引用 | `commit_cell` | 交付控制器、UI | ID 与页码，非原文 | 否 | 否 |
| `quote` | 原文短引（≤500 字符） | `commit_cell` | 交付控制器、UI | **是：原文短引** | 否 | **禁止** |
| `reason_code` | 证据状态 | `commit_cell` | UI、评测 | 否 | 仅计数/原因码 | 是（原因码） |
| `updated_at` | 数据库时钟 | `commit_cell` | UI | 否 | 否 | 否 |

硬门禁：`status='committed'` 必须提供 `document_id` 与 `page_no`，否则存储层直接拒绝；无可靠来源只能以 `needs_human` 提交并给出原因码。

### 3.3 `agent_artifacts`

| 字段 | 来源 | 写入方 | 读取方 | 含敏感内容 | 进事件 | 进日志 |
| --- | --- | --- | --- | --- | --- | --- |
| `task_id` | 任务创建 | `register_artifact` | 交付控制器、UI | 否（ID） | 否 | 是（ID） |
| `name` | 契约声明 | `register_artifact` | UI、评测 | 否 | 是 | 是 |
| `schema_version` | 契约版本 | `register_artifact` | 评测 | 否 | 否 | 否 |
| `status` | 写后重读校验 | `register_artifact` | UI、完成守卫 | 否 | 是 | 是 |
| `rows_expected` / `rows_committed` | 契约与写后校验 | `register_artifact` | UI、评测 | 计数 | 是（`rows` / `rowsExpected`） | 是 |
| `content_sha256` | 文件哈希 | `register_artifact` | 评测、审计 | 否（哈希） | 否 | 否 |
| `storage_path` | 交付目录 | `register_artifact` | 交付控制器、UI | 相对路径 | 否 | 否 |
| `updated_at` | 数据库时钟 | `register_artifact` | UI | 否 | 否 | 否 |

### 3.4 `agent_runtime_state`

| 字段 | 来源 | 写入方 | 读取方 | 含敏感内容 | 进事件 | 进日志 |
| --- | --- | --- | --- | --- | --- | --- |
| `task_id` | 任务创建 | `ensure_runtime` | 恢复、UI | 否（ID） | 否 | 是（ID） |
| `runtime_version` | 任务创建设置或已钉住值 | `ensure_runtime` | Runner、UI、评测 | 否 | 是（`runtimeVersion`） | 是 |
| `schema_version` | 常量 | `ensure_runtime` | 评测 | 否 | 否 | 否 |
| `rounds_used` | 运行计数 | `save_runtime` | 恢复、UI、指标 | 计数 | 是（`run_recovered`） | 是 |
| `tool_calls_used` | 运行计数 | `save_runtime` | 恢复、UI、指标 | 计数 | 是 | 是 |
| `model_calls_used` | 运行计数 | `save_runtime` | 恢复、UI、指标 | 计数 | 是 | 是 |
| `prompt_tokens` / `completion_tokens` | 模型用量 | `save_runtime` | 效率指标 | 计数 | 否 | 是（计数） |
| `explore_rounds_used` | 探索轮次 | `save_runtime` | 调度、评测 | 计数 | 否 | 是 |
| `started_at` / `updated_at` | 数据库时钟 | `ensure_runtime` / `save_runtime` | 评测 | 否 | 否 | 否 |
| `state_json` | 最后检查点标记 | `save_runtime` | 恢复 | 仅检查点标记 | 否 | 否 |

索引与主键：

- 主键：`agent_work_units(task_id, work_unit_id)`、`agent_cell_results(task_id, artifact_name, cell_key)`、`agent_artifacts(task_id, name)`、`agent_runtime_state(task_id)`。
- 索引：任务状态、工作单元优先级、答案格状态、答案格文档版本、交付物状态。

## 4. 共享 Schema 差异

类型唯一来源：[packages/contracts/src/index.ts](../packages/contracts/src/index.ts)。

| 类型/字段 | 变更 | Core 来源 | IPC/主进程 | Renderer |
| --- | --- | --- | --- | --- |
| `AgentTask.runtimeVersion` | 新增 `'v1' \| 'v2' \| 'legacy'` | `agent_runtime_state.runtime_version` | 透传 | 任务详情/恢复展示 |
| `AgentTask.deliveryStatus` | 新增 `'pending' \| 'partial' \| 'complete' \| 'invalid'` | `delivery_progress.status` | 透传 | 交付状态提示 |
| `AgentTask.workProgress` | 新增工作单元/答案格/交付/当前单元聚合 | `scheduler.progress` | 透传 | 进度条与步骤流 |
| `AgentTask.budgetProgress` | 新增阶段、上限、已用、原因、交付保留轮次 | `budget.snapshot` | 透传 | 预算展示与告警 |
| `AgentWorkUnitProgress` | 新增类型 | `unit_progress` | 透传 | 工作单元进度 |
| `AgentCellProgress` | 新增类型 | `cell_progress` | 透传 | 已提交/待核实/阻塞 |
| `AgentDeliveryProgress` | 新增类型 | `delivery_progress` | 透传 | 交付覆盖与缺口 |
| `AgentCurrentWorkUnit` | 新增类型 | `scheduler.progress.currentUnit` | 透传 | 当前单元 |
| `AgentWorkProgress` | 新增类型 | 上述聚合 | 透传 | 进度总览 |
| `AgentBudgetProgress` | 新增类型 | `budget.snapshot` | 透传 | 预算进度 |
| `AgentTaskEvent.type` | 新增 `work_unit_update`、`artifact_commit`、`budget_update`、`run_recovered` | `autonomous.py` 事件发射 | 事件流 | 步骤流与进度 |

约束：

1. 新增 IPC 字段必须同时更新 Core、contracts、preload、主进程与 Renderer。
2. Renderer 不复制临时类型；`store.ts` 只做结构透传与事件映射。
3. 事件 payload 只含 ID、状态、计数、错误码，不含案件原文或完整引文。

## 5. 运行时兼容矩阵

| 运行时 | 创建条件 | 工作账本 | 预算与恢复 | 允许跨版本恢复 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `v1` | 任务未声明交付契约，或设置 `agent.runtimeVersion=v1` | 只写 `agent_runtime_state`，不编译工作单元/答案格 | 统一预算；恢复按检查点连续计数 | 否 | 用于与 V1 基线对拍；不做工作全集推断 |
| `v2` | 任务声明交付契约，且设置为 `v2`（默认） | 编译并登记工作单元、答案格、交付物 | 统一预算 + 交付保留轮次；恢复时账本与检查点取最大计数 | 否 | 新任务默认路径；子代理只写暂存区 |
| `legacy` | V50 之前的旧任务 | 不补写工作单元/答案格，不猜测事实状态 | 仅按原检查点恢复 | 否 | 只读查看或原方式收尾；不得用 V2 账本推断旧任务完成度 |

迁移与兼容边界：

| 场景 | 行为 | 验证 |
| --- | --- | --- |
| V49 空库升级 V50 | 创建四表五索引，`schema_migrations` 记录 50 | `m3_migration` 用例 1 |
| 含历史任务的 V49 库升级 | 旧任务/文档/事件行数不变，旧任务可读 | 用例 2 |
| 迁移中途异常 | 单事务回滚，V50 表不残留 | 用例 3 |
| V1 任务恢复 | `runtimeVersion` 保持 `v1` | 用例 4 |
| V2 任务恢复 | 工作单元/答案格计数一致 | 用例 5 |
| 旧版本应用读取 V50 库 | 旧表查询不受影响，不执行 down migration | 用例 6 |
| 工作空间删除/重建 | 新库干净，旧账本不残留 | 用例 7 |

运行时钉住规则：

1. 任务创建时读取 `agent.runtimeVersion`，写入 `agent_runtime_state.runtime_version` 后不再改写。
2. 恢复时优先使用已钉住版本；检查点中的 `runtimeVersion` 只用于校验，不允许把 V2 任务按 V1 恢复。
3. 旧版本应用不得恢复 V2 任务；只能保留只读交付物，由用户确认后新建任务或使用兼容包安全收尾。
4. 任务级预算覆盖优先于系统默认值：显式 `maxRounds` / `maxToolCalls` / `maxModelCalls` / `maxWallSeconds` / `maxTokens` 生效；系统只在缺省时按任务规模推导，且不在续跑时放宽到已超过用户上限的值。
5. 检查点保存轮次、工具、模型、Token、子代理轮次、墙钟起点与效率计数；恢复时续接而不是重置。

## 6. 事件字段清单

| 事件 | 触发点 | payload | 敏感内容 | 可进普通日志 |
| --- | --- | --- | --- | --- |
| `work_unit_update`（登记） | 任务启动、契约编译后 | `total`、`cells`、`artifacts`、`seeded`、`runtimeVersion` | 否（计数） | 是 |
| `work_unit_update`（调度） | 租约/状态迁移后 | `workUnitId`、`status`、`attempts`、`lastErrorCode`、`reasonCode`、`progress`（`total`、`committed`、`needsHuman`、`blocked`、`pending`、`inProgress`、`terminal`、`terminalRatio`、`byStatus`） | 否（ID/状态/错误码/计数） | 是 |
| `work_unit_update`（版本失效） | 文档换版/删除后 | `staleCells`、`staleUnits`、`staleReading`、`staleEvidence`、`progress` | 否（计数） | 是 |
| `artifact_commit` | 交付物原子提交后 | `name`、`status`、`rows`、`rowsExpected` | 否（文件名/计数） | 是 |
| `budget_update` | 预算阶段或原因变化 | `stage`、`used`、`limits`、`reasons`、`deliveryReservedRounds` | 否（计数/原因码） | 是 |
| `run_recovered` | 从检查点恢复且已有用量 | `roundsUsed`、`toolCallsUsed`、`modelCallsUsed`、`tokens`、`deliveryReservedRounds` | 否（计数） | 是 |
| `turn_complete`（扩展） | 任务收尾 | `delivery`、`workProgress`、`budget`、`finalize`、`efficiency` | 否（状态/计数/原因码） | 是 |

禁止项：

1. `quote`、案件原文、OCR 全文、解密内容不得进入任何事件 payload 或普通日志。
2. 审计 metadata 只记录引用 ID、文档 ID、页码、版本与状态。
3. `scope_json` 只存 ID 与格键；不得把原文片段塞进 scope。
4. `budget_update` 的 `used` 经 `budget.public_used` 过滤，只保留轮次/调用/Token 等数值计数，不包含工具参数摘要（可能含查询词或文件路径）。
5. API Key、令牌、私钥不得进入上述任何表、事件或日志。

## 7. 验收映射

| 验收项 | 自动化入口 | 通过条件 |
| --- | --- | --- |
| V49→V50 升级与回滚 | `python -m evals.m3_migration` | 7 个用例全通过 |
| 300 格唯一工作单元 | `python -m evals.m3_harness` → `contract` | 20×15=300，重复编译数量不变 |
| 1,865 页 / 300 格无淘汰 | `m3_harness` → `largeLedger` | 阅读、答案格、证据、争点均无淘汰 |
| 共享类型一致 | `npm run typecheck` | Core、contracts、preload、Renderer 编译通过 |
| 事件不含原文 | `m3_harness` 与 `m3_security` | 事件/审计只含 ID、状态、计数、错误码 |

证据路径：`services/core/evals/reports/m3_harness_<ts>.json`、`m3_migration_<ts>.json`、`m3_security_<ts>.json`。
