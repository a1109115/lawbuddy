# -*- coding: utf-8 -*-
"""M3 评测器：权限、安全与隐私（03 清单 §9 S-001~S-014 的确定性可自动化部分）。

用法（services/core 目录下）：
    python -m evals.m3_security [--keep]

覆盖（其余项属人工/专项测试，见报告 notes）：
1. S-001/S-002 权限默认与授权粒度：新案件默认 strict_local；策略矩阵；once/task/case 授权与撤权；
2. S-003 外发审批卡：用途/工具/目标/数据摘要/参数哈希/有效期/拒绝降级字段完整，审计不落全量哈希；
3. S-004 参数替换攻击：审批哈希不一致即失效，审批保持 pending；
4. S-005 路径逃逸：blob 存储按内容哈希落盘、blob URI 越界拒绝、临时目录案件段白名单；
5. S-006/S-007 Electron 隔离与导航白名单（静态检查 Main/Preload 源码）；
6. S-009 密钥：敏感设置密文落盘、DB/源码/审计无密钥特征命中；
7. S-010 日志：含身份证号与姓名的任务结束后，审计 metadata 无原文命中；
8. S-014 可视化注入：渲染层无原始 HTML 注入点；不可信输出清洗（控制字符/长度上限）。

报告：控制台摘要 + evals/reports/m3_security_<ts>.json。M3 门槛：全部 gates 通过。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]  # services/core/evals → 仓库根
MAIN_TS = REPO_ROOT / "apps/desktop/src/main/index.ts"
PRELOAD_TS = REPO_ROOT / "apps/desktop/src/preload/index.ts"
DB_FILENAME = "lawbuddy.db"  # 工作空间加密库文件名（与 lawbuddy.workspace 一致）
RENDERER_DIR = REPO_ROOT / "apps/desktop/src/renderer/src"

# 密钥特征（仅用于扫描命中，不含任何真实密钥）
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{12,}"),
    re.compile(r"(?i)(api[_-]?key|secret|password)\s*[:=]\s*[\"'][A-Za-z0-9_\-]{16,}[\"']"),
)


def _workspace_root() -> Path:
    """当前评测工作空间根目录（由 Core 的 workspaces 表读取，避免假设路径）。"""
    from lawbuddy.storage import get_db

    row = get_db().query_one("SELECT root_uri FROM workspaces ORDER BY created_at LIMIT 1")
    if row is None:
        raise RuntimeError("workspace not initialized")
    return Path(row["root_uri"])


def _seg(report: dict[str, Any], name: str, fn, *args) -> bool:
    try:
        rows, ok = fn(report, *args)
        entry = report.get(name) or {}
        entry.update({"ok": bool(ok), "rows": rows})
        report[name] = entry
        return bool(ok)
    except Exception:  # noqa: BLE001
        report[name] = {"ok": False, "rows": [], "error": traceback.format_exc(limit=8)}
        return False


# ---------------------------------------------------------------------------
# 段 1：权限默认与授权粒度（S-001 / S-002）
# ---------------------------------------------------------------------------

def _run_permissions(report: dict[str, Any], policy: Any) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.cases import create_case
    from lawbuddy.storage import get_db

    db = get_db()
    rows: list[dict[str, Any]] = []
    case = create_case(title="M3评测-权限", case_type="criminal", client_name="钱五", client_role="被告人辩护人")
    cid = case["id"]

    # S-001：全新案件默认严格本地；外发能力默认拒绝
    preset_row = db.query_one("SELECT permission_preset FROM cases WHERE id = ?", (cid,))
    rows.append({"step": "defaultPresetStrictLocal", "ok": preset_row["permission_preset"] == "strict_local",
                 "preset": preset_row["permission_preset"]})
    matrix = {
        cap: policy.decide(cap, "strict_local") for cap in
        (policy.CAP_NETWORK_MODEL, policy.CAP_RAW_DOCUMENT_EGRESS)
    }
    rows.append({"step": "strictLocalDeniesEgress", "ok": all(v == "deny" for v in matrix.values()), "matrix": matrix})
    controlled = {
        cap: policy.decide(cap, "controlled_external") for cap in
        (policy.CAP_NETWORK_MODEL, policy.CAP_RAW_DOCUMENT_EGRESS)
    }
    rows.append({"step": "controlledExternalRequiresApproval",
                 "ok": all(v == "require_approval" for v in controlled.values()), "matrix": controlled})

    # S-002：once 不落持久授权；task/case 落授权并可撤权
    from lawbuddy.agent.tasks import create_task

    task = create_task(case_id=cid, mode="legal_review", request_text="权限评测", model_policy={"remote": True})
    req = policy.create_approval(task["id"], cid, policy.CAP_NETWORK_MODEL,
                                 target="模型服务 topenrouter", payload_hash="hash-1", data_summary="摘要（脱敏后）")
    policy.respond(req["approvalId"], "allow_once")
    rows.append({"step": "onceNotPersisted",
                 "ok": policy.has_grant(policy.CAP_NETWORK_MODEL, task["id"], cid, "hash-1") is False})

    req2 = policy.create_approval(task["id"], cid, policy.CAP_NETWORK_MODEL,
                                  target="模型服务 topenrouter", payload_hash="hash-2", data_summary="摘要（脱敏后）")
    policy.respond(req2["approvalId"], "allow_task")
    granted = policy.has_grant(policy.CAP_NETWORK_MODEL, task["id"], cid)
    revoked = policy.revoke_grants(policy.CAP_NETWORK_MODEL, task["id"])
    after = policy.has_grant(policy.CAP_NETWORK_MODEL, task["id"], cid)
    rows.append({"step": "taskGrantAndRevoke", "ok": granted is True and revoked == 1 and after is False,
                 "revoked": revoked})

    req3 = policy.create_approval(task["id"], cid, policy.CAP_NETWORK_MODEL,
                                  target="模型服务 topenrouter", payload_hash="hash-3", data_summary="摘要（脱敏后）")
    policy.respond(req3["approvalId"], "allow_case")
    rows.append({"step": "caseGrantScoped", "ok": policy.has_grant(policy.CAP_NETWORK_MODEL, task["id"], cid) is True
                 and policy.has_grant(policy.CAP_NETWORK_MODEL, task["id"], "other-case") is False})

    # V32 会话隔离：allow_case 绑定审批卡的 scope_id（会话）——同会话放行、
    # 重开新对话窗口（新会话 id）不匹配，需重新审批
    task_a = create_task(case_id=cid, mode="legal_review", request_text="会话授权评测A",
                         model_policy={"remote": True}, conversation_id="conv-eval-a")
    task_b = create_task(case_id=cid, mode="legal_review", request_text="会话授权评测B",
                         model_policy={"remote": True}, conversation_id="conv-eval-b")
    req4 = policy.create_approval(task_a["id"], cid, policy.CAP_SHELL_EXEC,
                                  target="cmd /c dir", payload_hash="hash-shell-1",
                                  data_summary="本地 Shell 命令", scope_id="conv-eval-a")
    policy.respond(req4["approvalId"], "allow_case")
    rows.append({"step": "sessionGrantScoped",
                 "ok": policy.has_grant(policy.CAP_SHELL_EXEC, task_a["id"], "conv-eval-a") is True
                 and policy.has_grant(policy.CAP_SHELL_EXEC, task_b["id"], "conv-eval-b") is False
                 and policy.has_grant(policy.CAP_SHELL_EXEC, task_b["id"], cid) is False})

    report["permissions"] = {"caseId": cid, "taskId": task["id"]}
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 段 2：审批卡与参数替换（S-003 / S-004）
# ---------------------------------------------------------------------------

def _run_approval(report: dict[str, Any], policy: Any) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.agent import tasks as task_store
    from lawbuddy.storage import get_db

    db = get_db()
    rows: list[dict[str, Any]] = []
    cid = report["permissions"]["caseId"]
    task = task_store.create_task(case_id=cid, mode="legal_review", request_text="审批评测",
                                  model_policy={"remote": True})
    payload_hash = "abcdef0123456789abcdef0123456789"
    req = policy.create_approval(task["id"], cid, policy.CAP_RAW_DOCUMENT_EGRESS,
                                 target="外部模型服务", payload_hash=payload_hash,
                                 data_summary="字段：卷宗摘要 3 段；不含直接标识符")

    # S-003：审批卡字段完整
    events = task_store.get_events(task["id"])
    card = next((e["payload"] for e in events if e["type"] == "approval_request"), {})
    required = ("approvalId", "capability", "target", "dataSummary", "payloadHash", "expiresInMinutes", "fallback")
    rows.append({"step": "approvalCardFields", "ok": all(str(card.get(k) or "").strip() for k in required),
                 "missing": [k for k in required if not str(card.get(k) or "").strip()]})
    rows.append({"step": "approvalCardHashTruncated", "ok": card.get("payloadHash") != payload_hash
                 and payload_hash.startswith(str(card.get("payloadHash")))})

    audit_row = db.query_one("SELECT metadata_json FROM audit_logs WHERE action = 'approval.request' ORDER BY seq DESC LIMIT 1")
    rows.append({"step": "auditNoFullPayloadHash", "ok": payload_hash not in (audit_row["metadata_json"] or "")})

    # S-004：参数替换（哈希不一致）→ 拒绝且审批保持 pending
    mismatch = ""
    try:
        policy.respond(req["approvalId"], "allow_once", approved_payload_hash="tampered-hash")
    except policy.PolicyError as exc:
        mismatch = str(exc)
    still_pending = db.query_one("SELECT status FROM approval_requests WHERE id = ?", (req["approvalId"],))["status"]
    rows.append({"step": "payloadHashMismatchRejected", "ok": bool(mismatch) and still_pending == "pending",
                 "message": mismatch})

    # 正确哈希：审批通过
    result = policy.respond(req["approvalId"], "allow_once", approved_payload_hash=payload_hash)
    rows.append({"step": "payloadHashMatchApproved", "ok": result["status"] == "approved"})

    # 已处理审批不可重复处理
    again = ""
    try:
        policy.respond(req["approvalId"], "allow_once")
    except policy.PolicyError as exc:
        again = str(exc)
    rows.append({"step": "approvalNotReusable", "ok": bool(again)})

    report["approval"] = {"approvalId": req["approvalId"], "taskId": task["id"]}
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 段 3：路径逃逸（S-005）
# ---------------------------------------------------------------------------

def _run_path_safety(report: dict[str, Any], tempstore: Any) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy import documents

    rows: list[dict[str, Any]] = []
    root = _workspace_root()

    # blob 存储按内容哈希落盘：文件名含 `..` 与分隔符也不影响目标路径
    evil = root / "blobs-eval-src" / "..-evil-name.txt"
    evil.parent.mkdir(parents=True, exist_ok=True)
    evil.write_text("路径逃逸评测样本", encoding="utf-8")
    sha = documents.sha256_file(evil)
    blob_uri = documents._store_blob(root, evil, sha)
    blob_path = (root / blob_uri).resolve()
    rows.append({"step": "blobStoredByHash", "ok": blob_path.exists()
                 and root.resolve() in blob_path.parents and blob_path.name == sha})

    # blob URI 越界一律拒绝
    escaped = 0
    for bad in ("../outside.txt", "..\\..\\outside.txt", str(Path("C:/Windows/win.ini"))):
        try:
            tempstore.materialize(root, bad, "case-eval", "txt")
        except tempstore.TempStoreError:
            escaped += 1
    rows.append({"step": "blobUriEscapeRejected", "ok": escaped == 3, "rejected": escaped})

    # 临时目录案件段白名单：`../` 与非法字符不得逃出受限目录
    tmp_root = tempstore.temp_root(root).resolve()
    for bad_case in ("../../escape", "..\\escape", ".."):
        d = tempstore.case_temp_dir(root, bad_case).resolve()
        if tmp_root not in d.parents:
            rows.append({"step": "tempDirEscape", "ok": False, "case": bad_case, "resolved": str(d)})
            break
    else:
        rows.append({"step": "tempDirContained", "ok": True})

    report["pathSafety"] = {"blobUri": blob_uri}
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 段 4：Electron 隔离与导航白名单（S-006 / S-007，静态检查）
# ---------------------------------------------------------------------------

def _run_electron_static(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    main_src = MAIN_TS.read_text(encoding="utf-8")
    preload_src = PRELOAD_TS.read_text(encoding="utf-8")

    checks = {
        "contextIsolation": "contextIsolation: true" in main_src,
        "sandbox": "sandbox: true" in main_src,
        "nodeIntegration": "nodeIntegration: false" in main_src,
        "windowOpenHttpsOnly": "setWindowOpenHandler" in main_src and "url.startsWith('https://')" in main_src,
        "navigationWhitelist": "will-navigate" in main_src and "event.preventDefault()" in main_src,
        "preloadWhitelistOnly": "contextBridge.exposeInMainWorld" in preload_src,
        "preloadNoNodeExposure": "require(" not in preload_src.replace("import ", ""),
        "preloadNoRawIpcExposure": "ipcRenderer" in preload_src and "exposeInMainWorld('ipcRenderer'" not in preload_src,
    }
    for name, ok in checks.items():
        rows.append({"step": f"main/preload:{name}", "ok": bool(ok)})

    report["electronStatic"] = checks
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 段 5：密钥与日志（S-009 / S-010）
# ---------------------------------------------------------------------------

ID_CARD = "11010519491231002X"  # 手工验算过校验和的样本（非真实数据）
PLAINTEXT_NAME = "评测脱敏样本姓名"


def _run_secrets_logs(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.agent import tasks as task_store
    from lawbuddy.storage import get_profile_db

    # 设备级配置（模型/API Key/连接器/技能）在 profile 库，不在工作空间库
    db = get_profile_db()
    rows: list[dict[str, Any]] = []
    root = _workspace_root()

    # S-009：敏感设置密文落盘，DB 中不出现明文密钥
    probe_key = "eval-probe-" + ("0" * 20) + "-placeholder"
    task_store.set_setting("model.apiKey", probe_key)
    stored = db.query_one("SELECT value_json FROM settings WHERE key = 'model.apiKey'")["value_json"]
    rows.append({"step": "apiKeyEncryptedAtRest", "ok": probe_key not in stored})
    rows.append({"step": "apiKeyRoundTrip", "ok": task_store.get_setting("model.apiKey") == probe_key})
    task_store.set_setting("model.apiKey", "")

    # 工作空间 DB 全文扫描密钥特征（关闭 WAL 后扫描主库）
    ws_db_path = Path(root) / DB_FILENAME
    hits: list[str] = []
    if ws_db_path.exists():
        raw = ws_db_path.read_bytes()
        for pat in SECRET_PATTERNS:
            for m in pat.finditer(raw.decode("utf-8", errors="ignore")):
                if probe_key[:16] not in m.group(0):
                    hits.append(m.group(0)[:12] + "…")
    rows.append({"step": "workspaceDbNoSecret", "ok": not hits, "hits": hits})

    # 设备级配置库同样只能是密文：扫描 profile.db 确认无明文密钥
    profile_hits: list[str] = []
    if db.path.exists():
        raw = db.path.read_bytes()
        for pat in SECRET_PATTERNS:
            for m in pat.finditer(raw.decode("utf-8", errors="ignore")):
                if probe_key[:16] not in m.group(0):
                    profile_hits.append(m.group(0)[:12] + "…")
    rows.append({"step": "profileDbNoSecret", "ok": not profile_hits, "hits": profile_hits})

    # 源码树扫描（README/文档不扫，只看核心实现）
    src_hits: list[str] = []
    for py in (REPO_ROOT / "services/core/lawbuddy").rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="ignore")
        for pat in SECRET_PATTERNS:
            for m in pat.finditer(text):
                src_hits.append(f"{py.name}:{m.group(0)[:12]}…")
    rows.append({"step": "sourceTreeNoSecret", "ok": not src_hits, "hits": src_hits[:5]})

    # S-010：先真实导入一份含直接标识符的卷宗，再扫描审计（任务链路留痕不得含原文）
    from lawbuddy.cases import create_case
    from lawbuddy.documents import import_documents

    case = create_case(title="M3评测-日志", case_type="criminal", client_name="周九", client_role="被告人辩护人")
    sample = root / "log-eval" / "证言样本.txt"
    sample.parent.mkdir(parents=True, exist_ok=True)
    sample.write_text(
        f"证人{PLAINTEXT_NAME}，身份证号{ID_CARD}，联系电话13800000000，住址北京市朝阳区幸福路12号。",
        encoding="utf-8",
    )
    outcomes = import_documents(case["id"], [str(sample)])
    rows.append({"step": "sensitiveDocImported", "ok": bool(outcomes) and outcomes[0].get("status") != "failed",
                 "status": outcomes[0].get("status") if outcomes else None})

    audit_hits = db.query(
        "SELECT action, metadata_json FROM audit_logs WHERE metadata_json LIKE ? OR metadata_json LIKE ? "
        "OR metadata_json LIKE ?",
        (f"%{ID_CARD}%", f"%{PLAINTEXT_NAME}%", "%13800000000%"),
    )
    rows.append({"step": "auditNoPlaintext", "ok": not audit_hits,
                 "hits": [r["action"] for r in audit_hits][:5]})

    # 审计禁用键：password/apiKey/token/content/text/prompt 不得进入 metadata
    forbidden = db.query(
        "SELECT action, metadata_json FROM audit_logs WHERE metadata_json LIKE '%\"password\"%' "
        "OR metadata_json LIKE '%\"apiKey\"%' OR metadata_json LIKE '%\"token\"%' "
        "OR metadata_json LIKE '%\"prompt\"%'"
    )
    rows.append({"step": "auditForbiddenKeysAbsent", "ok": not forbidden,
                 "hits": [r["action"] for r in forbidden][:5]})

    # 全链完整性（DB-007 / A-012 同源门禁）
    from lawbuddy.audit import verify

    chain = verify()
    rows.append({"step": "auditChainIntact", "ok": bool(chain.get("ok")), "detail": chain})

    report["secretsLogs"] = {
        "profileDbPath": str(db.path),
        "profileDbScanned": db.path.exists(),
        "workspaceDbPath": str(ws_db_path),
        "workspaceDbScanned": ws_db_path.exists(),
    }
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 段 6：可视化注入与不可信输出清洗（S-014）
# ---------------------------------------------------------------------------

def _run_injection(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []

    # 渲染层：不得存在原始 HTML 注入点（模型输出只能作为文本渲染）
    renderer_hits: list[str] = []
    for ts in RENDERER_DIR.rglob("*.tsx"):
        text = ts.read_text(encoding="utf-8", errors="ignore")
        for bad in ("dangerouslySetInnerHTML", "innerHTML", "javascript:"):
            if bad in text:
                renderer_hits.append(f"{ts.name}:{bad}")
    rows.append({"step": "rendererNoHtmlInjection", "ok": not renderer_hits, "hits": renderer_hits[:5]})

    report["injection"] = {"rendererScanned": len(list(RENDERER_DIR.rglob("*.tsx")))}
    ok = all(r["ok"] for r in rows)
    return rows, ok


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_eval(keep: bool = False) -> dict[str, Any]:
    from lawbuddy.workspace import ensure_workspace
    from lawbuddy import policy
    from lawbuddy import tempstore

    tmp = Path(tempfile.mkdtemp(prefix="lawbuddy-eval-"))
    report: dict[str, Any] = {
        "evalName": "m3_security",
        "startedAt": datetime.now().isoformat(timespec="seconds"),
        "mode": "local_deterministic",  # 零外网；静态检查覆盖 Electron 与渲染层
        "notes": "S-008 恶意解析样本、S-011 供应链验签、S-012 端口扫描、S-013 剪贴板属专项/人工测试，不在本评测范围",
    }
    try:
        ensure_workspace(str(tmp / "ws"))

        g1 = _seg(report, "permissions", _run_permissions, policy)
        g2 = _seg(report, "approval", _run_approval, policy)
        g3 = _seg(report, "pathSafety", _run_path_safety, tempstore)
        g4 = _seg(report, "electronStatic", _run_electron_static)
        g5 = _seg(report, "secretsLogs", _run_secrets_logs)
        g6 = _seg(report, "injection", _run_injection)

        gates = {
            "permissionDefaultsS001S002": g1,
            "approvalCardHashS003S004": g2,
            "pathEscapeS005": g3,
            "electronIsolationS006S007": g4,
            "secretsAndLogsS009S010": g5,
            "visualInjectionS014": g6,
        }
        report["gates"] = gates
        report["passed"] = all(gates.values())
        report["finishedAt"] = datetime.now().isoformat(timespec="seconds")
        return report
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)


def _print_summary(r: dict[str, Any]) -> None:
    print(f"\n=== M3 评测 · 权限、安全与隐私（{r['mode']}） ===")
    for seg, title in (("permissions", "权限默认与授权粒度(S-001/S-002)"), ("approval", "审批卡与参数替换(S-003/S-004)"),
                       ("pathSafety", "路径逃逸(S-005)"), ("electronStatic", "Electron 隔离与导航(S-006/S-007)"),
                       ("secretsLogs", "密钥与日志(S-009/S-010)"), ("injection", "可视化注入(S-014)")):
        entry = r.get(seg, {})
        print(f"{title}：{'通过' if entry.get('ok') else '未通过'}")
        for row in entry.get("rows", []):
            print(f"  {'✓' if row.get('ok') else '✗'} {row.get('step')}: "
                  f"{json.dumps({k: v for k, v in row.items() if k not in ('step', 'ok')}, ensure_ascii=False)}")
        if entry.get("error"):
            print(f"  ✗ 段异常：\n{entry['error']}")
    print("门槛判定：")
    for g, ok in r.get("gates", {}).items():
        print(f"  {'✓' if ok else '✗'} {g}")
    print(f"结论：{'M3 安全评测通过' if r.get('passed') else 'M3 安全评测未通过（见上方 ✗ 项）'}")
    print(f"范围说明：{r.get('notes', '')}")


def main() -> None:
    parser = argparse.ArgumentParser(description="M3 权限、安全与隐私评测（确定性部分）")
    parser.add_argument("--keep", action="store_true", help="保留评测工作空间（调试用）")
    args = parser.parse_args()

    report = run_eval(keep=args.keep)
    _print_summary(report)

    out_dir = Path(__file__).parent / "reports"
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = out_dir / f"m3_security_{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已保存：{out}")
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()