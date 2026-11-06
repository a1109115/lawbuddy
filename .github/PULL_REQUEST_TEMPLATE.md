## 这个 PR 做了什么

<!-- 一到三句话。说明动机与结果，而不是逐文件罗列。 -->

关联 Issue：<!-- Closes #123 / Refs #456，没有则写"无" -->

## 改动类型

- [ ] `feat` 新功能
- [ ] `fix` 缺陷修复
- [ ] `refactor` 行为不变的重构
- [ ] `docs` 文档
- [ ] `test` 测试与评测
- [ ] `ci` / `build` / `chore` 工程链路
- [ ] **BREAKING CHANGE**（请在下方说明迁移方式）

## 影响范围

- [ ] Python Core（`services/core`）
- [ ] Electron 主进程 / preload（`apps/desktop/src/main`、`preload`）
- [ ] 渲染层（`apps/desktop/src/renderer`）
- [ ] 共享协议（`packages/contracts`）
- [ ] 构建 / 打包链路
- [ ] 仅文档

## 关键三问

**1. 若新增或修改了 IPC / RPC 方法，五处是否都已同步？**

- [ ] `services/core/lawbuddy/service.py` 方法注册
- [ ] `packages/contracts/src/index.ts` 类型
- [ ] Electron 主进程桥接
- [ ] `preload/index.ts` 暴露
- [ ] 对应 UI 与评测
- [ ] 不涉及 IPC / RPC 改动

**2. 若涉及结论生成（阅卷 / 证据 / 合同 / 文书），每条结论是否都有可回溯出处？**

- [ ] 携带文档标识 + 页码或条款号 + 原文片段
- [ ] 引用经 `verify_quote` 校验（仅允许空白、断行、全半角空格差异）
- [ ] 来源缺失 / 低 OCR 置信度 / 版本失效时标为 `needs_human`，**没有**伪装为 `supported`
- [ ] 不涉及结论生成

**3. 自查：没有把敏感信息带进仓库？**

- [ ] `git diff` 中无 `.env`、密钥串、`builtin-key.bin`
- [ ] 无真实案件材料、当事人信息、案号、合同原文
- [ ] 无 `profile.db`、`workspace.db`、`evals/reports/` 产物
- [ ] 日志、审计 metadata、错误消息中未新增原文片段

## 测试与验证

| 检查 | 是否运行 | 结果 |
| --- | --- | --- |
| `npm run typecheck` | | |
| `npm run test` | | |
| `npm run build` | | |
| `npm run scan:secrets` | | |
| `python -m evals.*`（写明具体模块） | | |

<!-- 评测失败请如实记录失败项与原因，不要通过删除断言来"变绿"。 -->

## 截图 / 录屏（UI 改动必填）

<!-- 涉及界面的改动请附前后对比图。若含案件内容请先脱敏或用虚构样本。 -->

## 备注

<!-- 已知限制、后续计划、需要 reviewer 特别关注的地方。 -->
