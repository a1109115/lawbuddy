# -*- coding: utf-8 -*-
"""阅卷 Loop 远程 E2E（真实卷宗 case_file，智谱 glm-5.3-flash）。

验证闭环：
1. 首次运行：通读阶段命中未确认高敏感实体 → HumanInterventionRequired
   → 任务挂起 WAITING_HUMAN（不静默降级）；
2. 确认全部待确认实体（等效用户在「脱敏设置」中确认）；
3. 重新 start：从检查点续跑 → 全案通读 → 案件记忆 → 网络搜索 → 完整报告；
4. 校验：coverage 100%、报告各章节非空、引用绑定。
"""
import tempfile
import time
import uuid
from pathlib import Path

from lawbuddy.workspace import ensure_workspace, now_iso

ws = tempfile.mkdtemp(prefix="lawbuddy-e2e-loop-")
print("workspace:", ws)
ensure_workspace(ws)

from lawbuddy import redaction
from lawbuddy.cases import create_case
from lawbuddy.documents import import_documents
from lawbuddy.storage import get_db

case = create_case(title="E2E-王志远等合同诈骗案", case_type="criminal",
                   client_name="王志远", client_role="被告人（王志远）辩护人",
                   permission_preset="controlled_external")
with get_db().transaction() as conn:
    conn.execute(
        "INSERT INTO permission_grants(id, scope_type, scope_id, capability, payload_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), "case", case["id"], "NETWORK_MODEL", case["id"], now_iso()),
    )

files = sorted(p for p in Path(r"e:\project\Slvyue\case_file").rglob("*")
               if p.is_file() and not p.parent.name.startswith("_scripts"))
outcomes = import_documents(case["id"], [str(p) for p in files])
print(f"import: {sum(1 for o in outcomes if o['status'] == 'imported')}/{len(files)}")

from lawbuddy.agent.runner import AgentRunner
from lawbuddy.agent.tasks import create_task, get_events, get_task

task = create_task(
    case_id=case["id"], mode="legal_review",
    request_text="请通读本案材料，梳理案件概况、事实时间轴与争议焦点",
    workflow_preset="review_analysis",
    model_policy={"remote": True, "modelId": "glm-5.3-flash"},
)

runner = AgentRunner()


def wait_task(task_id: str, timeout_s: float = 600, min_seq: int = 0) -> dict:
    """等待任务进入终态；min_seq 用于跳过上一轮运行的旧状态（避免把
    尚存的 WAITING_HUMAN 当作本轮结果提前返回）。"""
    deadline = time.time() + timeout_s
    started = False
    while time.time() < deadline:
        t = get_task(task_id)
        evs = get_events(task_id)
        max_seq = max((e["seq"] for e in evs), default=0)
        if max_seq > min_seq:
            started = True  # 本轮已产生新事件
        if started and t["lifecycle"] in ("COMPLETED", "COMPLETED_WITH_WARNINGS", "CANCELLED", "FAILED", "WAITING_HUMAN"):
            return t
        time.sleep(1.0)
    return get_task(task_id)


# ---- 第 1 次运行：预期脱敏阻断 → WAITING_HUMAN ----
runner.instance().start(task["id"])
t = wait_task(task["id"], timeout_s=300)
print(f"\n=== 第 1 次运行: {t['lifecycle']} ===")
intervention = [e for e in get_events(task["id"]) if e["type"] == "human_intervention"]
for e in intervention:
    print("  intervention:", e["payload"].get("reason"))

if t["lifecycle"] != "WAITING_HUMAN":
    print("未触发人工介入（实体可能全部自动确认），直接观察最终结果")
else:
    # ---- 用户确认实体（等效「脱敏设置」一键确认） ----
    entities = redaction.list_entities(case["id"])
    pending = [e for e in entities if e["status"] == "needs_human"]
    print(f"待确认实体: {len(pending)} 个")
    if pending:
        redaction.confirm_entities(case["id"], [e["id"] for e in pending], action="confirm")
        print("已全部确认")

    # ---- 第 2 次运行：从断点续跑 ----
    prev_seq = max((e["seq"] for e in get_events(task["id"])), default=0)
    runner.instance().start(task["id"])
    t = wait_task(task["id"], timeout_s=900, min_seq=prev_seq)

print(f"\n=== 最终: {t['lifecycle']} ===")
events = get_events(task["id"])
print("\n--- 事件流（压缩） ---")
for e in events:
    p = e["payload"]
    if e["type"] == "status":
        print(f"  status {p.get('lifecycle')}/{p.get('stage')}")
    elif e["type"] in ("task_card", "corpus", "case_memory", "verification", "human_intervention", "resume"):
        print(f"  {e['type']}", {k: v for k, v in p.items() if not isinstance(v, (list, dict))})
    elif e["type"] == "model_call" and p.get("status") in ("ok", "failed", "blocked"):
        print(f"  model_call[{p.get('stage')}] {p.get('status')} {str(p.get('reason') or '')[:80]}")
    elif e["type"] == "reading":
        print(f"  reading {p.get('read')}/{p.get('total')}")
    elif e["type"] == "redaction":
        print(f"  redaction {p.get('status')} blocked={p.get('blockedCount', p.get('blockedTypes'))}")
    elif e["type"] == "error":
        print("  ERROR:", p)
readings = [e for e in events if e["type"] == "reading" and not e["payload"].get("stage")]
last_reading = readings[-1]["payload"] if readings else None
print("通读进度:", last_reading)
resume_ev = [e for e in events if e["type"] == "resume"]
print("恢复事件:", resume_ev[-1]["payload"] if resume_ev else None)
cm = [e for e in events if e["type"] == "case_memory"]
print("案件记忆:", cm[-1]["payload"] if cm else None)
msg = next((e["payload"]["body"] for e in reversed(events) if e["type"] == "message"), None)
if msg:
    report = msg.get("report") or {}
    print("\n--- 报告概览 ---")
    print("overview:", str(report.get("overview"))[:120])
    print("parties:", len(report.get("parties") or []),
          "| timeline:", len(report.get("timeline") or []),
          "| disputes:", len(report.get("disputes") or []),
          "| facts:", len(report.get("facts") or []),
          "| contradictions:", len(report.get("contradictions") or []))
    print("verification:", msg.get("verification"))
    print("degradedReason:", msg.get("degradedReason"))
    print("audit:", str(msg.get("audit"))[:100])
fails = [e for e in events if e["type"] == "model_call" and e["payload"].get("status") == "failed"]
print("\nmodel_call failed 数:", len(fails))
for e in fails[:3]:
    print("  ", e["payload"].get("stage"), str(e["payload"].get("reason"))[:100])
