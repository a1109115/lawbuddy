# -*- coding: utf-8 -*-
"""M4 评测器：AI 生成文档直接编辑（无工作副本）。

用法（services/core 目录下）：
    python -m evals.m4_artifact_editing

覆盖：
1. 文本类产物（HTML/Markdown）：open_editable 返回源码；save_editable 写回原文件，
   createVersion=true 在版本组内追加编辑版本，列表只回当前稿；
2. DOCX 产物：open_editable 解析为 Word 模型；保存后原文件可被 python-docx 读回
   编辑后的正文；自动保存（createVersion=false）不追加版本；
3. 安全边界：非文件类产物、文件名路径穿越、案件归档后编辑均被拒绝；
4. 产物按任务区分：工作空间根目录全案件共享时，别的任务（文书生成）落盘的
   文件不会被孤儿补登记算进本案产物概览，本案任务的文件仍按任务归属自愈；
5. 案件卷宗原件不在直接编辑范围内（只有 artifacts kind=file 可进入）；
6. 导出保真：同格式导出字节复制源文件；跨格式导出取源文件真实内容
   （非 intro 空壳）；导出只读，归档案件也应可导出。

M4 门槛：全部断言通过。
"""

from __future__ import annotations

import tempfile
from pathlib import Path


def main() -> int:
    from docx import Document
    from lawbuddy import artifacts, word
    from lawbuddy.cases import archive_case, create_case
    from lawbuddy.storage import get_db
    from lawbuddy.workspace import ensure_workspace

    def rejects(action, fragment: str) -> None:
        try:
            action()
        except (ValueError, RuntimeError) as exc:
            assert fragment in str(exc), str(exc)
        else:
            raise AssertionError(fragment)

    with tempfile.TemporaryDirectory(
        prefix="lawbuddy_artifact_edit_", ignore_cleanup_errors=True
    ) as temp:
        root = Path(temp)
        ws = root / "ws"
        ensure_workspace(str(ws))
        case = create_case("直接编辑", "civil", "委托人")

        # 1. 文本类产物：源码编辑 + 版本追加
        html = "<html><body><h1>标题</h1><p>原文</p></body></html>"
        (ws / "report.html").write_text(html, encoding="utf-8")
        text_art = artifacts.save_artifact(None, case["id"], "file", "report.html", {
            "fileName": "report.html", "sizeBytes": len(html.encode("utf-8")),
        })
        opened = artifacts.open_editable(text_art)
        assert opened["kind"] == "text" and "原文" in opened["text"], opened
        auto = artifacts.save_editable(text_art, text=html.replace("原文", "自动保存"))
        assert auto["ok"] and auto["versionNo"] is None, auto
        manual = artifacts.save_editable(
            text_art, text=html.replace("原文", "手动保存"), create_version=True
        )
        assert manual["versionNo"] == 2, manual
        assert "手动保存" in (ws / "report.html").read_text(encoding="utf-8")
        meta = next(a for a in artifacts.list_artifacts(case["id"]) if a["title"] == "report.html")
        assert meta["versionNo"] == 2 and meta["versionCount"] == 2, meta
        print("[PASS] 文本产物直接写回 / 自动保存不建版本 / 手动保存追加版本")

        # 2. DOCX 产物：Word 模型往返，保存后原文件更新
        model = {"type": "doc", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "AI 生成的原文"}]},
        ]}
        (ws / "brief.docx").write_bytes(word.to_docx(model))
        docx_art = artifacts.save_artifact(None, case["id"], "file", "brief.docx", {
            "fileName": "brief.docx", "sizeBytes": (ws / "brief.docx").stat().st_size,
        })
        opened_docx = artifacts.open_editable(docx_art)
        assert opened_docx["kind"] == "word", opened_docx
        assert "AI 生成的原文" in word.model_text(opened_docx["model"])
        artifacts.save_editable(docx_art, model=word.text_model("直接编辑后的正文"))
        assert "直接编辑后的正文" in Document(str(ws / "brief.docx")).paragraphs[0].text
        assert len(artifacts.list_artifact_versions(docx_art)) == 1
        print("[PASS] DOCX 产物 Word 模型解析 / 保存写回原文件")

        # 3. 安全边界
        non_file = artifacts.save_artifact(None, case["id"], "report", "报告", {"cites": []})
        rejects(lambda: artifacts.open_editable(non_file), "不是文件类产物")
        (ws / "safe.md").write_text("原文", encoding="utf-8")
        traversal = artifacts.save_artifact(None, case["id"], "file", "safe.md", {
            "fileName": "../safe.md",
        })
        rejects(lambda: artifacts.open_editable(traversal), "非法")
        rejects(lambda: artifacts.save_editable(text_art, text=None), "文本内容")
        print("[PASS] 非文件类产物 / 文件名穿越 / 空内容拒绝")

        # 4. 产物按任务区分（工作空间根目录全案件共享）
        from lawbuddy.agent.runner import register_orphan_files
        from lawbuddy.agent.tasks import create_task, update_lifecycle

        gen_case = create_case("", "other", "", default_mode="document_generation")
        gen_task = create_task(gen_case["id"], "document_generation", "起草起诉状")
        draft_name = "草稿_起诉状_v1.docx"
        (ws / draft_name).write_bytes(b"PK\x03\x04")  # 占用同名文件（归属判定不读正文）
        artifacts.save_artifact(gen_task["id"], gen_case["id"], "file", draft_name, {
            "fileName": draft_name, "sizeBytes": 4, "docgenDraftId": "dg-attribution",
            "draftMarked": True, "workspaceDirName": ws.name,
        })
        # 打开阅卷任务产物概览（触发孤儿补登记）：文书生成任务的产物不得被算进来
        register_orphan_files(case["id"])
        assert draft_name not in {a["title"] for a in artifacts.list_artifacts(case["id"])}
        assert draft_name in {a["title"] for a in artifacts.list_artifacts(gen_case["id"])}
        # 旧版孤儿补登记（task_id 为空、近似归属本案）的残留不再回显
        legacy = artifacts.save_artifact(None, case["id"], "file", draft_name, {
            "fileName": draft_name, "workspaceDirName": ws.name,
            "intro": f"工作空间根目录的文件，位于工作空间根目录（{ws.name}）：{draft_name}",
        })
        assert legacy not in {a["id"] for a in artifacts.list_artifacts(case["id"])}
        # 本案任务真正落盘的孤儿文件仍可自愈，并归到对应任务（不再 task_id 为空）
        review_task = create_task(case["id"], "legal_review", "阅卷分析")
        own_name = "阅卷报告.md"
        (ws / own_name).write_text("本任务落盘", encoding="utf-8")
        update_lifecycle(review_task["id"], "COMPLETED")
        assert register_orphan_files(case["id"]) >= 1
        owned = [a for a in artifacts.list_artifacts(case["id"]) if a["title"] == own_name]
        assert owned and owned[0]["taskId"] == review_task["id"], owned
        print("[PASS] 产物按任务区分：其他任务落盘文件不进本案产物概览")

        # 5. 归档案件拒绝编辑
        archive_case(case["id"])
        rejects(lambda: artifacts.save_editable(text_art, text="归档后修改"), "归档")
        print("[PASS] 归档案件拒绝直接编辑")

        # 6. 导出保真：同格式字节复制；跨格式取源文件真实内容（不是 intro 空壳）；
        #    导出为只读操作，归档案件也应可导出
        out = root / "out"
        out.mkdir()
        docx_dest = out / "brief_out.docx"
        res = artifacts.export_artifact(docx_art, "docx", str(docx_dest))
        assert docx_dest.read_bytes() == (ws / "brief.docx").read_bytes(), "同格式导出应字节复制"
        assert res["ok"] and res["sizeBytes"] == docx_dest.stat().st_size, res
        md_dest = out / "report_out.md"
        artifacts.export_artifact(text_art, "markdown", str(md_dest))
        assert "手动保存" in md_dest.read_text(encoding="utf-8"), "HTML 跨格式导出应含真实正文"
        md2 = out / "brief_out.md"
        artifacts.export_artifact(docx_art, "markdown", str(md2))
        assert "直接编辑后的正文" in md2.read_text(encoding="utf-8"), "DOCX 跨格式导出应含编辑后正文"
        print("[PASS] 同格式字节复制 / 跨格式真实内容导出 / 归档案件可导出")

        get_db().close()

    print("M4 AI 生成文档直接编辑：PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
