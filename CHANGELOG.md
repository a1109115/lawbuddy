# 更新日志

本文件的格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增

- **贡献与社区规范**：`CONTRIBUTING.md`、`SECURITY.md`、`CODE_OF_CONDUCT.md`
- **协作模板**：`.github/ISSUE_TEMPLATE/`（缺陷报告、功能建议、联系入口）、`.github/PULL_REQUEST_TEMPLATE.md`、`.github/CODEOWNERS`
- **持续集成**：`.github/workflows/ci.yml`（前端 typecheck + Vitest、Python ruff + 语法编译、密钥扫描）、`.github/workflows/release.yml`（手动触发的 Windows 打包）
- **依赖更新**：`.github/dependabot.yml`（npm 与 GitHub Actions 周更）
- **许可声明**：`NOTICE`（含许可范围例外声明）、`THIRD_PARTY_NOTICES.md`（第三方组件、模型与内容清单）
- **工具链固定**：`.nvmrc`、`.python-version`、`pyproject.toml`（ruff 配置）、`.editorconfig`、`.gitattributes`

### 变更

- 三个 `package.json` 补充 `license`、`repository`、`author`、`homepage`、`bugs` 元数据；`apps/desktop` 补齐 `engines`
- `README.md` 增加徽章、参与贡献入口、版本文件指引，并补充许可范围说明
- `.gitignore` 改用通用规则（`case_file*/`、`/*.pdf`、`/*.docx`），不再以具名文件暴露本地案件材料；移除 `skills-lock.json` 规则以解决"已忽略却仍被跟踪"的矛盾
- `services/core/requirements.txt` 修正关于锁文件的不实注释，明确当前未锁定及其影响

### 已知问题

- `xlsx@0.18.5` 携带两个已知漏洞（原型污染、ReDoS），npm 上游无修复版本，迁移方案见 `SECURITY.md`
- Python 依赖未生成锁文件，构建不完全可复现

## [0.1.0] - 2026-11-05

### 新增

- 首次开源发布
- 案卷阅卷 Agent（LangGraph 自主循环、事件可审计）
- 引用与证据门禁（Claim Ledger、`verify_quote` 原文校验、`needs_human` 人工核实流程）
- 合同审查工作流（原合同 / 条款 / 风险项 / 用户决策 / 工作副本分层）
- 法律文书生成与在线编辑（DOCX 含脚注与修订痕迹、XLSX、PPTX）
- 本地 OCR 与表格结构还原（ONNX Runtime DirectML、SLANet-plus 模型离线推理）
- 安全底座（SQLCipher 整库加密、系统密钥环保护 API Key、脱敏、审计、备份导出）
- Windows NSIS 安装包打包链路（PyInstaller 冻结 Python Core + electron-builder）

[Unreleased]: https://github.com/a1109115/lawbuddy/commits/main
[0.1.0]: https://github.com/a1109115/lawbuddy/releases/tag/v0.1.0
