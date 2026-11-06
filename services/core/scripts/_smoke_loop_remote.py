# -*- coding: utf-8 -*-
"""阅卷 Loop 远程端到端冒烟（智谱平台）。

覆盖：任务理解 → 语料边界 → 全案通读（逐页观察）→ 案件记忆（含网络搜索）
→ 定向检索与作答 → 质量校验。授权以案件级 grant 预置（等效用户已批准
allow_case），避免冒烟脚本阻塞在审批等待。
"""
import tempfile
import time
import uuid
from pathlib import Path

from lawbuddy.workspace import ensure_workspace, now_iso

ensure_workspace(tempfile.mkdtemp(prefix="lawbuddy-smoke-loop-"))

from lawbuddy.cases import create_case
from lawbuddy.documents import import_documents
from lawbuddy.storage import get_db

case = create_case(
    title="远程冒烟-借款纠纷",
    case_type="civil",
    client_name="张三",
    client_role="原告（张三）代理人",
    permission_preset="controlled_external",
)

# 案件级授权（等效审批 allow_case）：NETWORK_MODEL 外发放行
with get_db().transaction() as conn:
    conn.execute(
        "INSERT INTO permission_grants(id, scope_type, scope_id, capability, payload_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), "case", case["id"], "NETWORK_MODEL", case["id"], now_iso()),
    )

src = Path(tempfile.mkdtemp(prefix="lawbuddy-smoke-docs-")) / "借款协议.txt"
src.write_text(
    "借款协议\n"
    "甲方（出借人）：张三。乙方（借款人）：李四。\n"
    "2024年3月15日，乙方向甲方借款人民币伍拾万元整（¥500,000），约定2024年9月15日一次性归还。\n"
    "2024年3月16日，张三通过银行转账向李四支付伍拾万元。\n"
    "李四出具借条一张，载明借款金额与还款日期，并由李四签名确认。\n"
    "还款期限届满后，李四未归还任何款项。张三多次催要，李四以资金周转困难为由拖延。\n"
    "2025年1月10日，张三委托律师向李四发送律师函，要求十日内还款，李四签收但未回复。\n",
    encoding="utf-8",
)
outcomes = import_documents(case["id"], [str(src)])
print("import:", [(o["status"], o.get("pageCount")) for o in outcomes])

from lawbuddy.agent.runner import AgentRunner
from lawbuddy.agent.tasks import create_task, get_events, get_task

task = create_task(
    case_id=case["id"],
    mode="legal_review",
    request_text="请通读本案材料，评估借款追讨的请求权基础与证据完整性，并给出策略",
    model_policy={"remote": True, "modelId": "glm-5.3-flash"},
)
AgentRunner.instance().start(task["id"])
for _ in range(300):
    t = get_task(task["id"])
    if t["lifecycle"] in ("COMPLETED", "COMPLETED_WITH_WARNINGS", "CANCELLED", "FAILED"):
        break
    time.sleep(0.5)

print("lifecycle:", t["lifecycle"])
for e in get_events(task["id"]):
    p = e["payload"]
    if e["type"] == "status":
        print(f"  [{e['type']}] {p.get('lifecycle')} / {p.get('stage')}")
    elif e["type"] in ("task_card", "corpus", "reading", "case_memory", "verification"):
        print(f"  [{e['type']}]", {k: v for k, v in p.items() if not isinstance(v, (list, dict))})
    elif e["type"] == "model_call" and p.get("status") in ("ok", "failed", "blocked"):
        print(f"  [model_call] {p.get('stage')} {p.get('status')} {p.get('provider', '')} {str(p.get('reason'))[:120]}")
    elif e["type"] == "tool_call":
        print(f"  [tool_call] {p.get('tool')} {str(p.get('query'))[:40]}")
    elif e["type"] == "message":
        body = p.get("body") or {}
        report = body.get("report") or {}
        print("  [message] body keys:", sorted(body))
        print("    overview:", str(report.get("overview"))[:80])
        print("    timeline:", len(report.get("timeline") or []), "facts:", len(report.get("facts") or []))
        print("    verification:", body.get("verification"))
    elif e["type"] == "error":
        print("  [error]", p)
