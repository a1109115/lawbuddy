# LawBuddy

<p align="center"><img src="lawbuddy-mark.png" alt="LawBuddy" width="120"></p>

**LawBuddy** 是一个本地优先（local-first）的法律 AI 桌面应用，面向律师与法务的案卷阅卷、合同审查与法律文书起草场景。案件材料、工作副本与审计记录全部保存在本地加密存储中；AI 能力通过用户自行配置的大模型。

## 核心功能

- **案卷阅卷 Agent**：基于 LangGraph 的自主 Agent Loop，对导入的 PDF/DOCX/XLSX 案卷做通读、事实抽取、矛盾发现与证据整理，全过程事件可审计。
- **引用与证据门禁（Claim Ledger）**：每条事实性结论都必须携带可回溯的原文出处（文档、页码/条款号、原文片段），引用片段经 `verify_quote` 规范化校验；来源缺失、OCR 低置信度或版本失效的结论会进入 `needs_human` 待人工核实状态，而不是伪装成已支持事实。
- **合同审查工作流**：区分原合同、解析条款、风险项、用户决策与工作副本；风险项携带 `originalText` 与 `clauseRef` 证据基础，确定性规则只声明"原文检测到/未检测到"，语义风险保留原文并提示人工复核。
- **法律文书生成与在线编辑**：生成 Word（含脚注/修订痕迹）、XLSX、PPTX 交付物并支持原格式在线编辑；案件事实槽位区分 `case_evidence` / `user_confirmed` / `pending_verify`，无原文引证的事实拒绝正式填充。
- **本地 OCR 与表格还原**：扫描件 OCR（含 ONNX Runtime DirectML 加速）、线框表格结构重建，模型本地内置、离线推理。
- **安全与隐私底座**：SQLCipher 整库加密、DPAPI/Keychain/Secret Service 密钥环保护的 API Key 加密存储（fail-closed）、敏感内容脱敏、操作审计与备份导出。

## 架构

```text
┌──────────────────────────────────────────────┐
│  apps/desktop   Electron 桌面端                │
│   ├─ 主进程：窗口 / IPC / Python Core 子进程    │
│   └─ 渲染进程：React 19 + TypeScript + Zustand │
├──────────────────────────────────────────────┤
│  packages/contracts   共享类型契约（唯一来源）    │
│   IPC/RPC 方法、事件与产物 Schema               │
├──────────────────────────────────────────────┤
│  services/core   Python Core                  │
│   stdin/stdout 换行分隔 JSON-RPC 2.0 服务       │
│   阅卷 Agent / 合同审查 / 文书生成 / OCR        │
│   引用校验 / 加密存储 / 审计 / 技能系统          │
└──────────────────────────────────────────────┘
```

- 渲染进程与 Python Core 之间的所有通信都经过 `packages/contracts` 中定义的共享 Schema，前后端不允许各自复制临时类型。
- 模型调用支持 DeepSeek、智谱 BigModel、阿里云百炼（千问）、Kimi（月之暗面）等 OpenAI 兼容服务商，也可添加自定义服务商；API Key 按服务商隔离、本地加密保存，读取接口只返回 `configured` 状态。

## 技术栈

| 层 | 技术 |
| --- | --- |
| 桌面端 | Electron 44、electron-vite、React 19、TypeScript、Zustand、TipTap、pdf.js |
| Agent | Python 3.12、LangGraph、LangChain Core |
| OCR / 表格 | PaddleOCR、RapidOCR（ONNX Runtime DirectML）、RapidTable（SLANet-plus，离线） |
| 文档处理 | pypdf / pypdfium2、pdfplumber、python-docx、openpyxl、python-pptx、reportlab、msoffcrypto-tool |
| 存储 | SQLCipher（整库加密）、cryptography（AES-256-GCM / HKDF） |

## 快速开始（Windows）

### 环境要求

- Node.js ≥ 20
- Python 3.12（`sqlcipher3` 在 Windows 提供 cp312 预编译 wheel）
- Windows 10/11（密钥环使用 DPAPI；macOS/Linux 需自行适配 Keychain / Secret Service 路径）

### 安装与启动

```powershell
# 1. 安装前端依赖（monorepo workspaces）
npm install

# 2. 准备 Python Core 虚拟环境
cd services/core
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
cd ..\..

# 3. 启动开发模式（同时拉起 Electron 与 Python Core）
npm run dev
```

首次启动后在「设置 → API Key」中配置至少一个大模型服务商的 API Key（应用只访问你配置的模型服务商端点）。

### 常用命令

```powershell
npm run dev        # 开发模式
npm run build      # 构建渲染层/主进程
npm run typecheck  # TypeScript 类型检查
npm run test       # 渲染层单元测试（Vitest）

# Python Core 评测（在 services/core 下，使用 .venv 执行）
python -m evals.m2_baseline
python -m evals.m3_contract
```

修改共享协议、RPC、引用校验或合同审查逻辑时，请运行对应的 typecheck 与评测，不要只依赖手工点击验证。

## 安全与隐私设计

- **本地优先**：案件文档、解析结果、工作副本、审计记录全部留在本机加密库中；导出/备份内容由用户主动触发。
- **原件只读**：对原文的 OCR 更正、批注、合同工作副本与导出结果有清晰数据边界，工作副本永不覆盖原件。
- **密钥保护**：API Key 经系统密钥环（Windows DPAPI / macOS Keychain / Linux Secret Service）保护的加密机制保存，密钥环不可用时 fail-closed 而非降级明文；密钥仅在真正发起模型请求的最短生命周期内解密到内存。
- **打包门禁**：`npm run scan:secrets` 在打包链路中扫描产物，命中疑似密钥即阻断构建。
- **证据可回溯**：所有分析结论遵循「结论 → 原文出处 → 证据状态 → 待确认项」结构，UI 提供从结论跳回原文位置的引证芯片。

开发约定（含 API Key 管理、法律分析硬性规则与交付前检查清单）见 [AGENTS.md](AGENTS.md)。

## 目录结构

```text
apps/desktop            # Electron 桌面端（主进程 / preload / React 渲染层）
packages/contracts      # IPC/RPC、事件与产物 Schema 的共享类型包
services/core           # Python Core（lawbuddy 包 + evals 评测 + seed 种子数据）
docs                    # 设计与改造方案文档
scripts                 # 构建辅助、敏感信息扫描、e2e 脚本
```

## 免责声明

LawBuddy 是辅助工具，不是法律意见提供者。AI 生成的所有分析、风险提示与文书草稿都可能存在错误或遗漏，关键事实请以案件原文为准，正式交付前必须由执业律师人工核实。使用本软件处理真实案件时，请遵守所在司法辖区的执业规范与数据保护要求。

## 许可证

[Apache-2.0](LICENSE) © 2026 LawBuddy Authors
