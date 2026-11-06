# 贡献指南 / Contributing to LawBuddy

感谢你愿意参与 LawBuddy。这是一个本地优先的法律 AI 桌面应用，面向律师与法务的案卷阅卷、合同审查与法律文书起草场景。

因为它的使用场景涉及**真实案件材料、当事人隐私与执业责任**，本项目的贡献门槛比一般开源项目略高。请先读完本文件再动手，尤其第 5 节与第 7 节。

- [1. 开始之前](#1-开始之前)
- [2. 开发环境](#2-开发环境)
- [3. 分支与提交](#3-分支与提交)
- [4. 代码约定](#4-代码约定)
- [5. 安全与隐私硬规则](#5-安全与隐私硬规则)
- [6. 测试要求](#6-测试要求)
- [7. 法律与证据类改动的额外要求](#7-法律与证据类改动的额外要求)
- [8. 提交合并请求](#8-提交合并请求)
- [9. 提交前检查清单](#9-提交前检查清单)

---

## 1. 开始之前

**先搜索，再提问。** 提交 Issue 前请先在 [Issues](https://github.com/a1109115/lawbuddy/issues) 中搜索是否已有同类问题。

**安全问题不要开 Issue。** 涉及密钥泄露、路径逃逸、解密绕过、Prompt 注入等的漏洞，请按 [SECURITY.md](SECURITY.md) 走私密披露渠道。

**不要在 Issue / PR / 提交信息中粘贴真实案件材料。** 包括当事人姓名、身份证号、案号、法院名称、合同原文。复现问题时请自行构造虚构样本，或对原文做脱敏替换（人名改为"甲/乙/丙"，金额乘一个系数，案号改写）。

**本项目是辅助工具，不是法律意见提供者。** 贡献的代码与文档不得暗示系统输出可直接作为法律意见使用。

参与本项目即表示你同意遵守 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。

---

## 2. 开发环境

### 必需

| 依赖 | 版本 | 说明 |
| --- | --- | --- |
| Node.js | ≥ 20（推荐 22） | 版本要求见 `package.json` 的 `engines` 与 `.nvmrc` |
| Python | 3.12.x | `sqlcipher3` 在 Windows 仅提供 cp312 预编译 wheel，**不要用 3.13** |
| 操作系统 | Windows 10/11 | 密钥环使用 DPAPI；macOS/Linux 需自行适配 Keychain / Secret Service |

### 安装

```powershell
# 1. 前端依赖（npm workspaces，勿用 pnpm/yarn）
npm install

# 2. Python Core 虚拟环境
cd services/core
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
cd ..\..

# 3. 启动
npm run dev
```

### 常用命令

| 命令 | 作用 |
| --- | --- |
| `npm run dev` | 同时拉起 Electron 与 Python Core |
| `npm run build` | 构建渲染层与主进程 |
| `npm run typecheck` | TypeScript 类型检查（主进程 + 渲染层双配置） |
| `npm run test` | 渲染层单元测试（Vitest） |
| `npm run scan:secrets` | 密钥扫描门禁（需先 `npm run build`） |
| `npm run dist:win` | 完整 Windows 打包链路 |

Python 评测从 `services/core` 目录执行，使用 `.venv`：

```powershell
python -m evals.m2_baseline
python -m evals.m3_contract
python -m evals.m3_security
```

评测报告默认写入 `services/core/evals/reports/`（已加入 `.gitignore`，可能含案件文本片段，**不得提交**）。

---

## 3. 分支与提交

### 分支命名

从 `main` 切出，格式 `<type>/<short-slug>`：

| 前缀 | 用途 | 示例 |
| --- | --- | --- |
| `feat/` | 新功能 | `feat/case-merge` |
| `fix/` | 缺陷修复 | `fix/ocr-blank-page` |
| `docs/` | 仅文档 | `docs/ocr-migration` |
| `refactor/` | 不改变行为的重构 | `refactor/citation-verify` |
| `test/` | 仅测试与评测 | `test/m4-docx-footnotes` |
| `ci/` `chore/` `build/` | 工程链路 | `ci/add-workflows` |

`main` 受保护，**不接受直接推送**，一律走 Pull Request。

### 提交信息

遵循 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/v1.0.0/)：

```text
<type>(<scope>): <subject>

[body]

[footer]
```

- `type`：`feat` `fix` `docs` `refactor` `test` `ci` `chore` `build` `perf`
- `scope`：模块名，建议用 `core` / `desktop` / `contracts` / `ocr` / `citations` / `contract` / `docgen` / `evals`
- `subject`：祈使句、小写开头、不加句号、不超过 72 字符
- 破坏性变更在 type 后加 `!`，并在 footer 写 `BREAKING CHANGE:`

```text
fix(citations): 拒绝空白规范化之外的引用差异

verify_quote 之前会忽略标点差异，导致改标点后的引用仍被判为 supported。
现在只允许空白、断行与全半角空格差异，其余一律降级为 needs_human。

Closes #42
```

**一次提交只做一件事。** 把"改逻辑"与"调格式"分开，便于回滚与 review。避免出现单个提交跨 100 个文件的巨型改动。

---

## 4. 代码约定

### 协议与类型

`packages/contracts/src/index.ts` 是 IPC / RPC / 事件 / 产物 Schema 的**唯一来源**。禁止在渲染层或主进程复制一份"临时类型"——这是历史上最容易产生前后端漂移的地方。

新增一个 IPC 方法必须**同时**改五处，缺一不可：

1. `services/core/lawbuddy/service.py` 中的方法注册
2. `packages/contracts/src/index.ts` 的请求/响应类型
3. `apps/desktop/src/main/` 的桥接
4. `apps/desktop/src/preload/index.ts` 的暴露
5. 对应 UI 与评测

### TypeScript

- 两个 tsconfig 都已开启 `strict`，不要用 `any` 绕过；确需时必须注释理由
- 组件放在 `apps/desktop/src/renderer/src/components/`，服务层放 `services/`，全局状态放 `state/store.ts`
- 样式使用既有 `styles/tokens.css` 的变量，不硬编码颜色（本项目涉及涨跌/状态色语义，硬编码会破坏可访问性）

### Python

- 3.12 语法，`from __future__ import annotations` 保持现状
- 对外部输入（文件、路径、JSON-RPC 参数）一律在边界处校验
- 日志中禁止出现案件原文、当事人信息、密钥；用 ID 与状态码代替
- 新依赖必须同时更新 `services/core/requirements.txt` 并说明许可

### 复用优先

新增 Agent、检索、文档处理能力时，先评估 LangChain/LangGraph 官方能力与现有模块，避免重复造轮子。引入第三方资源前必须核对：**许可证、商用限制、依赖安全、维护活跃度、数据出境行为、离线可用性**。项目坚持本地优先，**不接受默认联网才能工作的依赖**。

---

## 5. 安全与隐私硬规则

这几条不是建议，是合并与否的门槛。

1. **不得硬编码任何密钥。** API Key、令牌、私钥、`builtin-key.bin` 一律不入库、不入日志、不入 Prompt、不入前端状态回传。用户密钥经系统密钥环（DPAPI / Keychain / Secret Service）保护，密钥环不可用时 **fail-closed**，不得降级为明文。
2. **不得新增默认外发行为。** 任何把案件内容发送到用户未配置端点的改动都会被拒绝。外发必须有明确用途、目标、数据摘要与用户审批。
3. **不得把敏感原文写入日志、审计 metadata 或错误消息。** 审计只记录引用 ID、文档 ID、页码、版本、状态。
4. **不得破坏原件只读边界。** 工作副本永不覆盖原件。
5. **不得降低 Electron 隔离。** `contextIsolation: true`、`sandbox: true`、`nodeIntegration: false` 是基线，不接受为调试方便临时放宽后忘记恢复的改动。
6. **不得跳过密钥扫描。** `npm run scan:secrets` 是打包链路的强制门禁，命中即阻断，不接受 `--no-verify` 式绕过。

提交前请自查：`git diff` 里有没有 `.env`、密钥串、真实案号、`profile.db`、`evals/reports/` 下的产物。

---

## 6. 测试要求

| 改动范围 | 至少需要跑 |
| --- | --- |
| 渲染层组件 / 状态 | `npm run test` |
| 主进程 / preload / contracts | `npm run typecheck` |
| 引用校验、OCR、合同、文书、加密 | 对应 `python -m evals.*` |
| 打包相关 | `npm run build` + `npm run scan:secrets` |

- **不要只依赖手工点击验证。** 修改共享协议、RPC、引用校验或合同审查逻辑时，评测必须跑。
- 新增行为按风险补测试：引用、OCR 置信度、文档版本失效、合同风险来源、人工核实流程优先覆盖；纯 UI 调整不必套用过重的测试流程。
- 评测失败请在 PR 中**如实记录失败项与原因**，不要删除或跳过断言来"变绿"。
- CI 会跑 `typecheck`、`test`、密钥扫描与 Python 语法检查；大型评测（需模型服务商 Key）不在 CI 中运行，由本地执行后在 PR 里贴结论。

---

## 7. 法律与证据类改动的额外要求

任何影响**结论生成**的改动（阅卷分析、证据整理、合同审查、文书生成）必须满足：

1. **结论可回溯**：每条事实性结论携带文档标识、页码或条款号、原文片段；引用经 `citations.py` 的 `verify_quote` 校验，只允许空白、断行、全半角空格差异。
2. **状态不伪装**：来源缺失、引用不匹配、OCR 低置信度、版本失效时，必须标为 `needs_human` / `conflict` / `stale`，**不得**输出为 `supported`。
3. **不确定就询问**：缺页、版本冲突、当事人角色不明时，应向用户提问或显著标注待核实，不得编造页码、条款号、金额、日期、当事人。
4. **确定性规则只做守门**：规则只能声明"原文中检测到/未检测到"，语义判断交给模型 + 人工复核。
5. **UI 可定位**：结论到原文必须有跳转入口（引证芯片），不能只展示"AI 认为"。

违反以上任一条的 PR 将被要求返工，无论功能是否"看起来能用"。

---

## 8. 提交合并请求

1. 从 `main` 切分支，完成改动并本地跑通对应检查
2. 推送分支，按 [PR 模板](.github/PULL_REQUEST_TEMPLATE.md) 填写
3. 有对应 Issue 时在描述中写 `Closes #123`，合并时自动关闭
4. 一个 PR 只解决一件事；大改动请先开 Issue 讨论方向，避免白做
5. 维护者可能要求拆分提交或补测试；请以 review 意见为准
6. 合并策略：**Squash merge**，提交信息取 PR 标题（需符合 Conventional Commits）

首次贡献者无需担心流程生疏，PR 里说明"首次贡献"即可，维护者会给出具体修改指引。

---

## 9. 提交前检查清单

复制到 PR 描述里逐条勾选：

- [ ] 改动范围明确，未覆盖他人未提交的改动
- [ ] 新增/修改的 IPC 方法已同步 Core 注册、主进程桥接、preload、contracts 与 UI
- [ ] 每条法律结论都有可回溯原文出处（文档 + 页码/条款号 + 片段）
- [ ] 引用已通过 `verify_quote` 校验，不确定项标为 `needs_human`
- [ ] 未把案件原文、当事人信息、密钥写入日志、审计 metadata、错误消息或测试 fixture
- [ ] 已运行相关 `typecheck` / `test` / `evals`，失败项已如实记录
- [ ] 新依赖已核对许可证与离线可用性，并更新 `requirements.txt` 或 `package.json`
- [ ] `git diff` 中不含 `.env`、密钥、真实案号、`profile.db`、`evals/reports/`
- [ ] 提交信息符合 Conventional Commits
