# 第三方组件与许可清单

本文件列出 LawBuddy 直接依赖的第三方组件及其许可。**以各依赖包内自带的 LICENSE 原文为准**；若本表与实际不符，请提 Issue 更正。

生成方式：依据 `services/core/requirements.txt`、三个 `package.json` 与仓库内随包分发的资源人工核对。
最后更新：2026-11-06（对应 v0.1.0）

> 为什么需要这份文件：本项目以 Apache-2.0 分发，而 Apache-2.0 第 4(d) 条要求分发时保留第三方声明。安装包内除本项目代码外，还包含 Python 依赖、Electron 运行时、OCR 模型与种子技能资源，用户有权知道其中各部分的版权归属。

---

## 一、Python Core 运行时依赖

| 组件 | 许可 | 用途 |
| --- | --- | --- |
| [pydantic](https://github.com/pydantic/pydantic) | MIT | JSON-RPC 参数与产物 Schema 校验 |
| [langgraph](https://github.com/langchain-ai/langgraph) | MIT | 阅卷 Agent 的状态图与循环控制 |
| [langchain-core](https://github.com/langchain-ai/langchain) | MIT | 模型抽象与消息协议 |
| [paddlepaddle](https://github.com/PaddlePaddle/Paddle) | Apache-2.0 | OCR 推理框架 |
| [paddleocr](https://github.com/PaddlePaddle/PaddleOCR) | Apache-2.0 | 文本检测与识别 |
| [pypdf](https://github.com/py-pdf/pypdf) | BSD-3-Clause | PDF 文本抽取与页操作 |
| [pdfplumber](https://github.com/jsvine/pdfplumber) | MIT | 线框表格结构重建（依赖 pdfminer.six，MIT） |
| [python-docx](https://github.com/python-openxml/python-docx) | MIT | DOCX 读写 |
| [openpyxl](https://openpyxl.readthedocs.io/) | MIT | XLSX 读写 |
| [python-pptx](https://github.com/scanny/python-pptx) | MIT | PPTX 生成与编辑 |
| [sqlcipher3](https://github.com/coleifer/sqlcipher3) | BSD 风格（含 SQLCipher Community Edition） | 整库加密 |
| [cryptography](https://github.com/pyca/cryptography) | Apache-2.0 或 BSD-3-Clause（双许可） | AES-256-GCM / HKDF 文件级加密 |
| [msoffcrypto-tool](https://github.com/nolze/msoffcrypto-tool) | MIT | 加密 Office 文档解密 |
| [reportlab](https://www.reportlab.com/opensource/) | BSD-3-Clause | PDF 导出 |
| [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) | Apache-2.0 或 BSD-3-Clause（底层 pdfium 为 BSD-3-Clause） | 扫描页渲染 |
| [rapidocr_onnxruntime](https://github.com/RapidAI/RapidOCR) | Apache-2.0 | ONNX OCR 推理 |
| [onnxruntime-directml](https://github.com/microsoft/onnxruntime) | MIT | ONNX 运行时与 DirectML 加速 |
| [rapid-table](https://github.com/RapidAI/RapidTable) | Apache-2.0 | 表格结构还原（SLANet-plus） |

## 二、随包分发的模型与资源

| 资源 | 许可 | 说明 |
| --- | --- | --- |
| `services/core/models/slanet-plus.onnx` | Apache-2.0（源自 PaddleOCR / RapidTable 模型库） | SLANet-plus 表格结构模型，7.4 MB，本地离线推理 |
| PaddleOCR 检测/识别模型 | Apache-2.0 | 首次运行时按需获取或随包分发（见构建脚本） |

> **字体说明**：PDF 导出使用 Adobe CID 字体 `STSong-Light`，通过引用方式调用，**不捆绑字体文件**，以规避中文字体授权问题。字体本身的使用须遵守 Adobe 相应许可；本项目不对字体授予任何权利。

## 三、桌面端（前端）依赖

| 组件 | 许可 | 用途 |
| --- | --- | --- |
| [electron](https://github.com/electron/electron) | MIT | 桌面运行时 |
| [react](https://github.com/facebook/react) / react-dom | MIT | 渲染层 UI |
| [zustand](https://github.com/pmndrs/zustand) | MIT | 前端状态管理 |
| [@tiptap/*](https://github.com/ueberdosis/tiptap) | MIT | 富文本与文档编辑 |
| [pdfjs-dist](https://github.com/mozilla/pdf.js) | Apache-2.0 | PDF 渲染 |
| [mermaid](https://github.com/mermaid-js/mermaid) | MIT | 图表渲染 |
| [markdown-it](https://github.com/markdown-it/markdown-it) | MIT | Markdown 渲染 |
| [jszip](https://github.com/Stuk/jszip) | MIT 或 GPLv3（双许可，本项目按 MIT 使用） | OOXML 压缩包读写 |
| [docx-preview](https://github.com/VolodymyrBaydalka/docxjs) | MIT | DOCX 预览 |
| [xlsx (SheetJS)](https://github.com/SheetJS/sheetjs) | Apache-2.0 | XLSX 解析（**存在已知漏洞，见 SECURITY.md**） |
| [pptx-preview](https://github.com/yuhangxuan/pptx-preview) | MIT | PPTX 预览 |
| [electron-vite](https://github.com/alex8088/electron-vite) / [vite](https://github.com/vitejs/vite) | MIT | 构建工具 |
| [vitest](https://github.com/vitest-dev/vitest) | MIT | 单元测试 |
| [typescript](https://github.com/microsoft/TypeScript) | Apache-2.0 | 类型系统 |
| [electron-builder](https://github.com/electron-userland/electron-builder) | MIT | 安装包构建 |

## 四、随仓库分发的第三方内容

| 内容 | 权利状态 | 说明 |
| --- | --- | --- |
| `services/core/seed/skills/@team/case-file-review/references/cause-of-action-wiki/` | **著作权归原作者及出版方所有，仅限个人学习研究，禁止商业使用** | 对《民事案件案由适用要点与请求权规范指引（第三版）》的结构化整理，约 3.9 万行。**不适用本仓库 Apache-2.0 授权**。详见该目录内 `DISCLAIMER.md` 与根目录 `NOTICE` |

## 五、参考实现与配方

以下内容仅作为设计参考、未直接复制代码，列出以便追溯：

- 05-OCR 迁移方案中提及的 ONNX OCR 客户端实现思路
- 各模型服务商（DeepSeek、智谱 BigModel、阿里云百炼、月之暗面）的 OpenAI 兼容 API 文档

---

## 维护说明

1. 新增依赖时，同步在本表登记许可与用途
2. 许可为 GPL / AGPL / SSPL 等**强著佐权**协议的依赖，需在 PR 中单独说明理由（本项目为 Apache-2.0 分发，不接受会传染整个代码库的许可）
3. 若发现本表遗漏或错误，请提 Issue 并注明依赖名与正确的许可
