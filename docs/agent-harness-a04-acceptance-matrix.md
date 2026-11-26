# LawBuddy Harness A-04：验收矩阵与故障注入

| 项目 | 内容 |
| --- | --- |
| 依据 | [agent-harness-upgrade-plan.md](agent-harness-upgrade-plan.md) 阶段 A-04、§6 C-01–C-06 |
| 状态 | 确定性验收已实现并通过；真实模型复测（C-04/C-05）待执行 |
| 生成日期 | 2026-11-16 |

## 1. 使用方式

确定性验收不依赖模型、API Key 与外部数据快照，在 `services/core` 目录执行：

```powershell
python -m evals.m3_harness
python -m evals.m3_migration
python -m evals.m3_security
python -m evals.wfa_law --profile fast --out law-fast-v2.json
python -m evals.wfa_law --profile fast --out law-fast-v2-2.json
python -m evals.wfa_law --compare law-fast-v2.json law-fast-v2-2.json
```

真实模型复测需要模型服务商配置与数据快照，命令见第 7 节；运行期不得读取官方 `reference/`、真值或评分器内部答案。

## 2. HAR-T-001–015 验收矩阵

| 编号 | 场景 | 自动化入口 | 断言要点 | 证据 |
| --- | --- | --- | --- | --- |
| HAR-T-001 | 300 格任务契约编译 | `m3_harness` → `contract`（`_run_contract`） | 20×15=300 个唯一工作单元；重复编译 ID 与数量不变；派生产物已声明 | `evals/reports/m3_harness_<ts>.json` |
| HAR-T-002 | 1,865 页 / 300+ 证据 | `m3_harness` → `largeLedger`（`_run_large_ledger`） | 1,865 条阅读记录、320 个答案格、300 条证据、60 个争点均无淘汰；覆盖可复算 | 同上 |
| HAR-T-003 | 同词不同文档范围 | `m3_harness` → `searchScope`（`_run_search_scope`） | A/B 文档集合范围键不同；集合顺序稳定；不同案件/版本不串用 | 同上 |
| HAR-T-004 | 预算取消与恢复 | `m3_harness` → `budgetRecovery`（`_run_budget_recovery`） | 轮次、工具、模型、Token、墙钟连续计数；恢复不重置 | 同上 |
| HAR-T-005 | 30% 中断 | `m3_harness` → `delivery`（`_run_delivery`） | 只答 90/300 行时物化补齐 300 行；主/派生文件可解析 | 同上 |
| HAR-T-006 | 60% 中断 | `m3_harness` → `idempotency` + `delivery` | 重复提交不新增行；已提交结果保留；已处理格数不重复 | 同上 |
| HAR-T-007 | 90% 中断 | `m3_harness` → `budgetRecovery` + `delivery` | 交付保留预算独立；收尾物化与校验可完成 | 同上 |
| HAR-T-008 | 首字前断连 | `m3_harness` → `concurrency`（`_run_concurrency`）+ `disconnect`（`_run_disconnect`） | 首字前中断不可续写；并发降级但不静默放行；熔断期请求等待而不是空返回 | 同上 |
| HAR-T-009 | 正文与工具参数断连 | `m3_harness` → `disconnect` | 正文可续写；工具参数不完整时整轮丢弃并标记 `partial_tool_calls` | 同上 |
| HAR-T-010 | 子代理重复提交 | `m3_harness` → `subagent`（`_run_subagent_idempotent`）+ `idempotency` | 暂存区写入；正式文件未被直接改写；重复提交只有一行；正式文件仅控制器原子写入 | 同上 |
| HAR-T-011 | 单工作单元连续两次证据类失败 | `m3_harness` → `scheduler`（`_run_scheduler`） | 两次证据类失败 → `needs_human`；服务类失败 → `blocked`；其他单元继续 | 同上 |
| HAR-T-012 | 文档版本变化 | `m3_harness` → `versionStale`（`_run_version_stale`） | 旧版本已提交格标 `stale`；旧缓存/旧引用不显示为已支持 | 同上 |
| HAR-T-013 | 未确认值参与聚合 | `m3_harness` → `delivery` + `valueDomain`（`_run_value_domain`） | 空值不参与派生计数；未确认值不被当作否定值 | 同上 |
| HAR-T-014 | 交付不完整强制终答 | `m3_harness` → `delivery` | 删行后缺口可见、状态为 `partial`；结构完整但答案格未终态时不得显示 `complete` | 同上 |
| HAR-T-015 | 跨案件与原件访问 | `m3_harness` → `sandboxBoundary`（`_run_sandbox_boundary`）+ `m3_security` | 跨案召回 0；原件旁路读取 0；检索强制案件过滤 | 同上 + `m3_security_<ts>.json` |

补充 gate（同样进入 `m3_harness` 门槛）：

| gate | 覆盖 |
| --- | --- |
| `workUnitFlow` | 工作单元从租约、暂存、校验到终态的端到端流程 |
| `context` | 压缩/恢复后未提交单元格与证据状态仍可从账本重建；单元简报只含本单元材料 |
| `runtimePinning` | V1/V2 任务恢复不跨运行时；运行时版本不被改写 |
| `valueDomain` | 合法值域、多选规则、扁平 JSON 键图与来源列排除 |

## 3. Fixture 清单

| fixture | 规模与内容 | 对应验收 |
| --- | --- | --- |
| 20×15 合同网格 | 20 个协议 × 15 个 deal point = 300 格；派生 `deal_summary.csv` 按 deal point 聚合 | HAR-T-001/005/006/007/013/014 |
| 13 被告 JSON 交付 | `defendants` 下 D01–D13，每名 3 个字段；只答 9 名，缺 4 名由物化补齐 | HAR-T-005/014 |
| 大账本 | 1,865 页阅读记录、320 个答案格、300 条证据、60 个争点 | HAR-T-002 |
| 同词不同范围 | 文档集合 A、B、A+B、B+A 与不同案件/版本戳 | HAR-T-003 |
| 预算恢复 | 工具调用 12 次上限、5 次工具 + 2 次模型调用后保存/恢复 | HAR-T-004/007 |
| 工作型子代理 | 两个实体 × 一个字段；暂存区写入；重复提交 | HAR-T-010 |
| 并发与熔断 | 初始并发 2；429/超时降为 1；连续失败打开熔断 | HAR-T-008 |
| 故障网关 | 429、timeout、disconnect 三类注入 | HAR-T-008/009、C-04 |

实现位置：

- 单元级 fixture 与断言：[m3_harness.py](../services/core/evals/m3_harness.py)
- 迁移与兼容 fixture：[m3_migration.py](../services/core/evals/m3_migration.py)
- 安全与跨案：[m3_security.py](../services/core/evals/m3_security.py)
- 模型网关故障注入：[wfa_law/harness.py](../services/core/evals/wfa_law/harness.py)

## 4. 真实模型评测选项

`evals/wfa_law` 支持定向与自主两种口径，以及运行时、任务、重复次数、中断点与故障类型：

| 选项 | 取值 | 用途 |
| --- | --- | --- |
| `--profile` | `directed` / `autonomous`（与既有 `fast` / `full` 并列） | 定向链路与自主链路分开统计，不混算 |
| `--runtime` | `v1` / `v2` | 钉住任务运行时；V1 不登记工作账本 |
| `--task` | `WFA-LAW-001` / `013` / `019` | 代表任务复测 |
| `--all-law` | 开关 | 22 题全量回归 |
| `--repeat N` | 整数 | 同一任务连跑 N 次并聚合中位数/最差值/方差 |
| `--interrupt-at` | `30` / `60` / `90` | 在指定轮次百分比中断，检查交付物可解析 |
| `--inject-fault` | `disconnect` / `timeout` / `429` | 在指定调用注入模型网关故障 |
| `--collect-harness-metrics` | 开关 | 附加工作单元、答案格、交付、预算、Token、重复读取/检索指标 |
| `--workspace` | 目录 | 隔离工作空间根目录；`--repeat` / `--all-law` 自动按运行次数分目录 |
| `--data-dir` | 目录 | 含 `inputs/` 与 `reference/` 的数据快照 |
| `--compare` | 两个结果 JSON | 两次确定性运行逐字段比较，输出 `diffs` |

## 5. 中断与故障注入

### 5.1 中断点

`--interrupt-at 30|60|90` 按最大轮次百分比换算停止轮次，在收尾时执行：

1. 主交付物与派生物必须存在且可解析；
2. 已提交行保留，未提交行由物化补齐；
3. 重复提交不得新增行或重复副作用；
4. 报告写入 `interrupt` 与 `submissionParseable`。

### 5.2 模型网关故障

`FaultInjector` 在第 N 次模型调用注入一次故障，之后恢复正常：

| 故障 | 注入 | 预期 |
| --- | --- | --- |
| `429` | `ModelGatewayError`（限流） | 降并发/退避，不产生静默空批次 |
| `timeout` | `StreamInterrupted`（正文断连） | 可续写或有限重试，已确认工作不重复扣预算 |
| `disconnect` | `StreamInterrupted`（RemoteDisconnected） | 不产生空批次；按工作单元重试；不重放已提交副作用 |

故障注入报告写入结果 JSON 的 `faultInjection` 字段。

## 6. 结果模板

### 6.1 `m3_harness` 确定性报告

```json
{
  "evalName": "m3_harness",
  "startedAt": "2026-11-16T15:42:45",
  "mode": "deterministic",
  "contract": { "ok": true, "rows": [] },
  "largeLedger": { "ok": true, "rows": [] },
  "searchScope": { "ok": true, "rows": [] },
  "budgetRecovery": { "ok": true, "rows": [] },
  "delivery": { "ok": true, "rows": [] },
  "scheduler": { "ok": true, "rows": [] },
  "versionStale": { "ok": true, "rows": [] },
  "idempotency": { "ok": true, "rows": [] },
  "concurrency": { "ok": true, "rows": [] },
  "disconnect": { "ok": true, "rows": [] },
  "subagent": { "ok": true, "rows": [] },
  "workUnitFlow": { "ok": true, "rows": [] },
  "context": { "ok": true, "rows": [] },
  "runtimePinning": { "ok": true, "rows": [] },
  "sandboxBoundary": { "ok": true, "rows": [] },
  "valueDomain": { "ok": true, "rows": [] },
  "gates": {
    "contract": true,
    "largeLedger": true,
    "searchScope": true,
    "budgetRecovery": true,
    "delivery": true,
    "scheduler": true,
    "versionStale": true,
    "idempotency": true,
    "concurrency": true,
    "disconnect": true,
    "subagent": true,
    "workUnitFlow": true,
    "context": true,
    "runtimePinning": true,
    "sandboxBoundary": true,
    "valueDomain": true
  },
  "passed": true,
  "finishedAt": "2026-11-16T15:42:45"
}
```

### 6.2 真实任务结果（`wfa_law`）

数值仅为结构占位，实际以运行结果为准：

```json
{
  "profile": "autonomous",
  "runtime": "v2",
  "task": "WFA-LAW-001",
  "kind": "contract",
  "ok": true,
  "lifecycle": "COMPLETED",
  "deliveryStatus": "partial",
  "composite": 0.0,
  "branch": 0.0,
  "merge": 0.0,
  "durationS": 0,
  "approvals": 0,
  "faultInjection": { "fault": "disconnect", "atCall": 1, "calls": 1, "injected": 1 },
  "interrupt": { "atPercent": 30, "stopRound": 1, "maxRounds": 1 },
  "submissionParseable": { "files": {}, "allParseable": true },
  "harness": {
    "taskId": "<task-id>",
    "units": { "total": 0, "committed": 0, "needsHuman": 0, "blocked": 0, "pending": 0, "inProgress": 0, "terminal": 0, "terminalRatio": 0, "byStatus": {} },
    "cells": { "total": 0, "committed": 0, "needsHuman": 0, "blocked": 0, "stale": 0, "pending": 0, "byStatus": {} },
    "delivery": { "status": "pending", "structuralStatus": "pending", "workDone": false, "terminalRatio": 0, "cellsPending": 0, "artifactCount": 0, "completeCount": 0, "incomplete": [], "rowsExpected": 0, "rowsCommitted": 0, "rowCoverage": 0 },
    "budget": { "stage": "ok", "limits": {}, "used": {}, "reasons": [], "noProgressRounds": 0 },
    "runtime": { "runtimeVersion": "v2", "roundsUsed": 0, "toolCallsUsed": 0, "modelCallsUsed": 0, "promptTokens": 0, "completionTokens": 0 },
    "tokensPerCommittedCell": null,
    "duplicateReadRate": null,
    "duplicateSearchRate": null,
    "documentCoverage": null,
    "recovered": false,
    "redundantCommits": 0
  }
}
```

聚合报告（`--repeat N`）在单次结果之外附加：

```json
{
  "runs": 3,
  "branch": { "median": 0.0, "min": 0.0, "max": 0.0, "stdev": 0.0 },
  "merge": { "median": 0.0, "min": 0.0, "max": 0.0, "stdev": 0.0 },
  "composite": { "median": 0.0, "min": 0.0, "max": 0.0, "stdev": 0.0 },
  "structuralDeliveryRate": 1.0,
  "unitTerminalRate": 1.0
}
```

## 7. 执行顺序与门槛

### 7.1 确定性门禁（当前可通过）

```powershell
# 项目根目录
npm run typecheck
npm run test
npm run build
node scripts/scan-secrets.mjs
ruff check services/core
python -m compileall -q services/core -x "\.venv"

# services/core
python -m evals.m3_harness
python -m evals.m3_migration
python -m evals.m3_security
python -m evals.wfa_law --profile fast --out law-fast-v2.json
python -m evals.wfa_law --profile fast --out law-fast-v2-2.json
python -m evals.wfa_law --compare law-fast-v2.json law-fast-v2-2.json
```

通过条件：退出码 0；`m3_harness.passed=true`；`m3_migration` 全部用例通过；快速档两次比较 `ok=true`；敏感扫描无发现。

### 7.2 代表任务三次复测（C-04，待执行）

```powershell
python -m evals.wfa_law --profile autonomous --runtime v2 --task WFA-LAW-001 --repeat 3 --collect-harness-metrics --out e2e-001-v2.json
python -m evals.wfa_law --profile autonomous --runtime v2 --task WFA-LAW-013 --repeat 3 --collect-harness-metrics --out e2e-013-v2.json
python -m evals.wfa_law --profile autonomous --runtime v2 --task WFA-LAW-019 --repeat 3 --collect-harness-metrics --out e2e-019-v2.json
```

通过条件：9 次运行均有契约声明的全部交付物；001 中位数 ≥82.4；019 中位数 ≥78.1；013 三次均 >0 且 300 行结构覆盖 100%；工作单元终态率 100%；重复提交 0；静默空批次 0。

### 7.3 22 题全量回归（C-05，待执行）

```powershell
python -m evals.wfa_law --profile directed --runtime v1 --all-law --collect-harness-metrics --out law22-directed-v1.json
python -m evals.wfa_law --profile autonomous --runtime v2 --all-law --collect-harness-metrics --out law22-autonomous-v2.json
```

通过条件：定向链路平均综合分 ≥29.6；V2 自主链路 22/22 均有结构交付物；无跨案、无真值路径访问。

## 8. 当前状态（2026-11-16）

| 项 | 状态 |
| --- | --- |
| `m3_harness` 16 项 gate | 通过（最近报告 `m3_harness_20261116_161643.json`） |
| `m3_migration` 7 个用例 | 通过（最近报告 `m3_migration_20261116_161536.json`） |
| `m3_security` / `m3_evidence` / 既有回归 | 通过（2026-11-16 16:15 复测） |
| `wfa_law --profile fast` 两次比较 | 通过，`diffs: []` |
| 运行时版本失效 | 主循环启动时按当前活动版本失效旧阅读/证据/答案，并退回对应工作单元 |
| 交付保留预算 | 主循环按 `scheduler.split_budget` 拆分并随检查点恢复，收尾阶段禁止新检索 |
| 子代理预算记账 | 工作型子代理与 `case_explore` 的模型调用、Token、工具调用、轮次均计入统一预算 |
| C-04 代表任务三次复测 | 未执行（需模型配置与数据快照） |
| C-05 22 题全量回归 | 未执行（需完整 22 题快照） |
| C-06 效率报告 | 指标采集已实现，真实运行数据待补 |

证据目录：`services/core/evals/reports/`（该目录被 `.gitignore` 忽略，仅本机留存）。
