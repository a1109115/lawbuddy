# -*- coding: utf-8 -*-
"""M4 评测器：交付物版本管理（缺口补齐——AI 修订迭代 / 产物版本组 / 导出可追溯）。

用法（services/core 目录下）：
    python -m evals.m4_deliverable_versions [--keep]

覆盖（自主 Loop + mock 网关 / 直连 store 双路径；不联网）：
1. G1 docgen.revise_body 版本链：渲染 v1 → AI 修订 v2（kind=ai_revise）→ 定稿被修订
   回落到草稿态 → 恢复 v1 追加 v3（历史版本保留不删）；
2. G2 导出来源版本可追溯：export 返回 versionNo=当前版本，审计记录
   docgen.export + sourceVersionNo（不含正文/路径）；
3. G3 artifacts 版本组：save_artifact_version 组内追加 v2、list_artifacts 每组只回
   当前稿（versionNo/versionCount）、list_versions 按组返回、restore 追加式回退、
   不同标题自成组；
4. G4 修订任务闭环（document_generation 修订分叉 + 自主 Loop + mock 网关）：
   docgen_get_draft → docgen_revise_draft → 版本链落 ai_revise、正文按意见变化；
5. G5 修订输入守门：缺修订意见拒绝、跨案件草稿拒绝、提纲未确认/无正文的草稿
   由任务内工具守门拒绝（模型收到错误回灌，不落版本）。

M4 门槛：全部 gates 必须通过。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ._loop_mock import ScriptStep, install_mock_gateway, last_tool_result, user_payload

# ---------------------------------------------------------------------------
# 夹具：民事起诉状模板（与 m3_docgen 同源，保证字段/占位符行为一致）
# ---------------------------------------------------------------------------

TEMPLATE_MD = """民事起诉状

原告：{{原告姓名}}，{{原告身份信息}}。
被告：{{被告姓名}}，{{被告身份信息}}。

诉讼请求：
一、判令被告向原告支付欠款人民币{{欠款金额}}元；
二、本案诉讼费用由被告承担。

事实与理由：
原告与被告于{{签约时间}}签订借款协议，约定还款期限届满后，被告经多次催告仍未履行还款义务。

此致
××人民法院

具状人：
"""

REVISION_INSTRUCTION = "把事实与理由中「被告经多次催告仍未履行还款义务」改为「被告经原告多次催告，至今仍未履行还款义务」，其余内容保持不变"


def _revised_body(base: str) -> str:
    return base.replace(
        "被告经多次催告仍未履行还款义务",
        "被告经原告多次催告，至今仍未履行还款义务",
    )


def _log(msg: str) -> None:
    print(msg, flush=True)


def _gate(name: str, ok: bool, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    _log(f"  [{mark}] {name}{('：' + detail) if detail else ''}")
    return ok


def _err_of(fn) -> str:
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        return str(e)
    return ""


def revision_steps() -> list[ScriptStep]:
    """修订任务 mock 脚本：读当前稿 → 按意见落修订版本 → 汇报。"""

    def di(messages: list[dict[str, Any]]) -> dict[str, Any]:
        return user_payload(messages).get("文书生成输入") or {}

    def get_args(messages: list[dict[str, Any]]) -> dict[str, Any]:
        return {"draftId": di(messages).get("revisionDraftId")}

    def revise_args(messages: list[dict[str, Any]]) -> dict[str, Any]:
        d = di(messages)
        # 修订正文 = 工具结果里的当前稿按确定性规则改写（评测断言 Core 行为而非模型输出）
        base = last_tool_result(messages).get("bodyMd") or ""
        return {
            "draftId": d.get("revisionDraftId"),
            "instruction": d.get("revisionInstruction"),
            "revisedBody": _revised_body(base),
        }

    def final_text(messages: list[dict[str, Any]]) -> str:
        r = last_tool_result(messages)
        return (
            f"已按修订意见完成修订并落为新版本 v{r.get('versionNo')}（AI 修订）；"
            "历史版本保留，可在版本历史中比较或恢复。"
        )

    return [
        ScriptStep.tool("docgen_get_draft", get_args),
        ScriptStep.tool("docgen_revise_draft", revise_args),
        ScriptStep.final(final_text),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M4 交付物版本管理评测")
    parser.add_argument("--keep", action="store_true", help="保留临时工作空间")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    from lawbuddy.rpc import RpcServer
    from lawbuddy import service
    from lawbuddy.workspace import ensure_workspace
    from lawbuddy.cases import create_case
    from lawbuddy.agent.tasks import get_task
    from lawbuddy import docgen as docgen_store
    from lawbuddy import artifacts as artifact_store
    from lawbuddy.storage.db import get_db

    results: dict[str, Any] = {"gates": {}, "details": {}}
    tmp = Path(tempfile.mkdtemp(prefix="eval_m4_versions_"))
    t0 = time.time()
    all_ok = True
    try:
        ensure_workspace(str(tmp / "ws"))
        case = create_case(title="评测-交付物版本", case_type="civil", client_name="原告", default_mode="document_generation")
        other_case = create_case(title="评测-另一案件", case_type="civil", client_name="被告", default_mode="document_generation")
        server = RpcServer()
        service.register(server)
        agent_start = server._handlers["agent.start"]

        # ---- 准备：模板 → 草稿 → 确认提纲 → 渲染 v1 ----
        tpl = server._handlers["docgen.import_template"]({
            "title": "民事起诉状模板", "docType": "民事起诉状",
            "templateMd": TEMPLATE_MD, "caseId": case["id"],
            "sourceName": "起诉状模板.md",
        })
        draft = docgen_store.create_draft(
            case["id"], tpl["id"], "民事起诉状", "追讨借款本金")
        docgen_store.update_outline(draft["id"], docgen_store.outline_from_template(TEMPLATE_MD))
        server._handlers["docgen.confirm_outline"]({"draftId": draft["id"]})
        draft = server._handlers["docgen.render"]({"draftId": draft["id"]})
        draft_id = draft["id"]
        base_body = draft["bodyMd"]

        # ---- G1 修订版本链 ----
        _log("[G1] docgen.revise_body 版本链（ai_revise）")
        res = server._handlers["docgen.revise"]({
            "draftId": draft_id,
            "bodyMd": _revised_body(base_body),
            "instruction": REVISION_INSTRUCTION,
        })
        versions = docgen_store.list_versions(draft_id)
        kinds = [v["kind"] for v in versions]
        g1 = (
            _gate("修订落 v2 且 kind=ai_revise", res["versionNo"] == 2 and res["kind"] == "ai_revise",
                  f"v{res['versionNo']} kind={res['kind']}")
            and _gate("版本标签为「AI 修订」", versions[0]["kindLabel"] == "AI 修订", versions[0]["kindLabel"])
            and _gate("历史版本保留", kinds == ["ai_revise", "render"], str(kinds))
            and _gate("正文更新且占位符重新审计", res["draft"]["bodyMd"] != base_body
                      and res["draft"]["blockingCount"] > 0)
            and _gate("修订意见回显", res["instruction"] == REVISION_INSTRUCTION)
        )
        # 定稿 → 修订回落草稿态（REQ-GEN-007 门禁不被 AI 修订绕过）
        docgen_store.fill_slots(draft_id, [
            {"name": n, "value": f"值-{n}", "sourceType": "user_confirmed"}
            for n in ("原告姓名", "原告身份信息", "被告姓名", "被告身份信息", "欠款金额", "签约时间")
        ])
        # 槽位填充后重新渲染，占位符替换为槽位值才可定稿（REQ-GEN-007）
        server._handlers["docgen.render"]({"draftId": draft_id})
        confirmed = docgen_store.confirm_draft(draft_id)
        g1 &= _gate("定稿成功", confirmed["status"] == "confirmed", confirmed["status"])
        draft_after = docgen_store.revise_body(draft_id, _revised_body(base_body), "再次修订")
        g1 &= _gate(
            "修订使定稿回到草稿态",
            draft_after["draft"]["status"] == "draft",
            draft_after["draft"]["status"],
        )
        # 恢复历史版本：追加保留
        restored = server._handlers["docgen.restore_version"]({"draftId": draft_id, "versionNo": 1})
        versions = docgen_store.list_versions(draft_id)
        g1 &= _gate(
            "恢复 v1 追加为新版本（历史不删）",
            restored["bodyMd"] == base_body
            and [v["kind"] for v in versions]
            == ["restore", "ai_revise", "render", "ai_revise", "render"],
            str([v["kind"] for v in versions]),
        )
        # 修订正文为空拒绝
        g1 &= _gate("空修订正文拒绝", "不能为空" in _err_of(
            lambda: docgen_store.revise_body(draft_id, "  ")))
        results["gates"]["G1_revise_version_chain"] = g1
        all_ok &= g1

        # ---- G2 导出来源版本可追溯 ----
        _log("[G2] 导出来源版本与审计")
        exp = server._handlers["docgen.export"]({
            "draftId": draft_id, "destDir": str(tmp), "format": "md",
        })
        max_ver = get_db().query_one(
            "SELECT MAX(version_no) AS v FROM docgen_draft_versions WHERE draft_id = ?",
            (draft_id,),
        )["v"]
        audit_rows = get_db().query(
            "SELECT action, metadata_json FROM audit_logs WHERE action = 'docgen.export' "
            "ORDER BY seq DESC LIMIT 1"
        )
        meta = json.loads(audit_rows[0]["metadata_json"]) if audit_rows else {}
        g2 = (
            _gate("导出返回来源版本号", exp.get("versionNo") == max_ver, f"v{exp.get('versionNo')}")
            and _gate("审计记录 sourceVersionNo", meta.get("sourceVersionNo") == max_ver, str(meta))
            and _gate("审计不含正文与路径", "bodyMd" not in meta and "path" not in meta)
        )
        results["gates"]["G2_export_traceability"] = g2
        all_ok &= g2

        # ---- G3 artifacts 版本组 ----
        _log("[G3] artifacts 版本组（交付物迭代改写）")
        body_v1 = {"report": {"overview": "第一版阅卷报告", "facts": []}}
        a1 = artifact_store.save_artifact(None, case["id"], "report", "评测案 · 阅卷分析报告", body_v1)
        full1 = artifact_store.get_artifact(a1)
        body_v2 = {"report": {"overview": "第二版阅卷报告（按用户意见修订）", "facts": []}}
        full2 = artifact_store.save_artifact_version(a1, body_v2)
        listed = artifact_store.list_artifacts(case["id"])
        group_rows = [x for x in listed if x["groupId"] == full1["groupId"]]
        vers = artifact_store.list_artifact_versions(a1)
        g3 = (
            _gate("新版本自成一组（v1）", full1["versionNo"] == 1)
            and _gate("save_version 组内追加 v2", full2["versionNo"] == 2
                      and full2["groupId"] == full1["groupId"])
            and _gate("概览每组只回当前稿", len(group_rows) == 1
                      and group_rows[0]["versionNo"] == 2
                      and group_rows[0]["versionCount"] == 2)
            and _gate("版本历史按组返回", [v["versionNo"] for v in vers] == [2, 1])
            and _gate("当前稿内容为新版本", group_rows[0]["id"] == full2["id"])
        )
        # 回退 = 追加式（历史不删）
        cur = artifact_store.restore_artifact_version(a1, 1)
        vers = artifact_store.list_artifact_versions(a1)
        g3 &= _gate(
            "回退 v1 追加为 v3 当前稿",
            cur["versionNo"] == 3 and cur["body"] == body_v1
            and [v["versionNo"] for v in vers] == [3, 2, 1],
            f"cur=v{cur['versionNo']} total={len(vers)}",
        )
        # 不同标题自成组（对话里生成不同类型的产物不混组）
        b1 = artifact_store.save_artifact(None, case["id"], "viz", "评测案 · 可视化阅卷", {"viz": {}})
        g3 &= _gate("不同交付物各自成组", artifact_store.get_artifact(b1)["groupId"] == b1)
        # 传组内任意版本 id 均按组返回
        g3 &= _gate(
            "历史版本 id 也能按组列出",
            [v["versionNo"] for v in artifact_store.list_artifact_versions(full2["id"])] == [3, 2, 1],
        )
        results["gates"]["G3_artifact_version_groups"] = g3
        all_ok &= g3

        # ---- G4 修订任务闭环（自主 Loop + mock 网关） ----
        _log("[G4] 修订任务闭环（document_generation 修订分叉）")
        # 准备一个干净草稿：模板渲染 → 定稿前的版本链 [render]
        draft2 = docgen_store.create_draft(
            case["id"], tpl["id"], "民事起诉状", "修订闭环演示")
        docgen_store.update_outline(draft2["id"], docgen_store.outline_from_template(TEMPLATE_MD))
        server._handlers["docgen.confirm_outline"]({"draftId": draft2["id"]})
        draft2 = server._handlers["docgen.render"]({"draftId": draft2["id"]})
        before_body = draft2["bodyMd"]
        before_versions = docgen_store.list_versions(draft2["id"])

        undo = install_mock_gateway(revision_steps())
        try:
            task = agent_start({
                "caseId": case["id"], "mode": "document_generation",
                "docgenInput": {
                    "revisionDraftId": draft2["id"],
                    "revisionInstruction": REVISION_INSTRUCTION,
                },
            })
            deadline = time.time() + 90
            while (
                get_task(task["id"])["lifecycle"]
                in ("CREATED", "PLANNING", "RUNNING", "VERIFYING")
                and time.time() < deadline
            ):
                time.sleep(0.4)
        finally:
            undo()
        lifecycle = get_task(task["id"])["lifecycle"]
        after_versions = docgen_store.list_versions(draft2["id"])
        after_kinds = [v["kind"] for v in after_versions]
        draft2_now = docgen_store.get_draft(draft2["id"])
        g4 = (
            _gate("修订任务完成", lifecycle == "COMPLETED", lifecycle)
            and _gate("落 ai_revise 新版本",
                      after_kinds == ["ai_revise", "render"], str(after_kinds))
            and _gate("历史版本保留", len(after_versions) == len(before_versions) + 1)
            and _gate("正文按意见变化", draft2_now["bodyMd"] == _revised_body(before_body))
        )
        results["gates"]["G4_revision_loop"] = g4
        all_ok &= g4

        # ---- G5 修订输入守门 ----
        _log("[G5] 修订输入守门")

        def expect_reject(params: dict[str, Any], fragment: str) -> bool:
            try:
                agent_start({"caseId": case["id"], "mode": "document_generation", **params})
            except ValueError as e:
                return fragment in str(e)
            return False

        g5 = (
            _gate("缺修订意见拒绝", expect_reject(
                {"docgenInput": {"revisionDraftId": draft_id, "revisionInstruction": " "}}, "修订意见"))
            and _gate("草稿不存在拒绝", expect_reject(
                {"docgenInput": {"revisionDraftId": "no-such-draft",
                                 "revisionInstruction": "x"}}, "不存在"))
        )
        # 跨案件草稿拒绝
        foreign = docgen_store.create_draft(
            other_case["id"], tpl["id"], "民事起诉状", "另一案件的草稿")
        g5 &= _gate("跨案件草稿拒绝", expect_reject(
            {"docgenInput": {"revisionDraftId": foreign["id"],
                             "revisionInstruction": "x"}}, "不属于当前案件"))
        # 提纲未确认的草稿：任务内工具守门（agent.start 不拒绝，工具报错回灌模型）
        unconfirmed = docgen_store.create_draft(
            case["id"], tpl["id"], "民事起诉状", "未确认提纲的草稿")
        g5 &= _gate("提纲未确认草稿 revise_body 拒绝", "提纲" in _err_of(
            lambda: server._handlers["docgen.revise"]({
                "draftId": unconfirmed["id"], "bodyMd": "正文", "instruction": "x"})))
        results["gates"]["G5_revision_guards"] = g5
        all_ok &= g5

        # ---- 报告 ----
        elapsed = round(time.time() - t0, 1)
        verdict = "PASS" if all_ok else "FAIL"
        summary = {
            "milestone": "M4 交付物版本管理",
            "verdict": verdict,
            "elapsedSec": elapsed,
            "gates": results["gates"],
            "generatedAt": datetime.now().isoformat(timespec="seconds"),
        }
        results["details"]["summary"] = summary
        out_dir = Path(__file__).resolve().parent / "reports"
        out_dir.mkdir(exist_ok=True)
        out = out_dir / f"m4_deliverable_versions_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

        _log("")
        _log(f"M4 交付物版本管理：{verdict}（{elapsed}s）")
        for name, ok in results["gates"].items():
            _log(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        _log(f"报告：{out}")
        return 0 if all_ok else 1
    finally:
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
