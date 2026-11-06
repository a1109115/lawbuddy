"""临时排查脚本：打印合同审查任务模型策略与最近事件（不含密钥/原文）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lawbuddy import keyring  # noqa: E402
from lawbuddy.storage import init_database  # noqa: E402

root = Path(sys.argv[1] if len(sys.argv) > 1 else r"D:\workagent\lawbuddy")
dek = keyring.load_or_create_dek(root)
db = init_database(root / "lawbuddy.db", dek)

print("cases:", json.dumps(db.query("SELECT id,title,default_mode,status FROM cases"), ensure_ascii=False))
rows = db.query(
    "SELECT id, mode, lifecycle, stage, created_at, client_position_json, model_policy_json "
    "FROM tasks WHERE mode = 'contract_review' ORDER BY created_at DESC LIMIT 5"
)
for r in rows:
    ctx = json.loads(r["client_position_json"] or "{}")
    policy = json.loads(r["model_policy_json"] or "{}")
    print("-" * 70)
    print("task:", r["id"], r["lifecycle"], r["stage"], r["created_at"])
    print("workflowKind:", ctx.get("workflowKind"), "| overviewId:", ctx.get("overviewId"))
    print("modelPolicy:", json.dumps(policy, ensure_ascii=False))
    for ev in db.query(
        "SELECT event_type, payload_json, created_at FROM task_events WHERE task_id = ? ORDER BY seq DESC LIMIT 8",
        (r["id"],),
    ):
        print("  ev:", ev["event_type"], (ev["payload_json"] or "")[:260])
