"""LawBuddy Python Core —— Agent Harness / 权限策略 / 检索 / 引用 / 网关。

进程职责（技术方案 §2.1）：
- SQLite 唯一写入者（后续迭代接入）；
- LangGraph 编排任务状态图，不承担权限判断；
- 通过 stdio 上的 JSON-RPC 2.0 与 Electron Main 通信；
- 本进程与 Worker 之外的代码不得直接联网。
"""

__version__ = "0.1.0"
PROTOCOL_VERSION = 1
