"""Basic DOCX work-copy roundtrip, encryption and optimistic-concurrency gates."""

from __future__ import annotations

import io
import tempfile
from pathlib import Path


def main() -> int:
    from docx import Document
    from lawbuddy import crypto, editor, word
    from lawbuddy.cases import archive_case, create_case
    from lawbuddy.documents import import_documents
    from lawbuddy.storage import get_db
    from lawbuddy.storage.db import reset_database
    from lawbuddy.workspace import ensure_workspace

    def rejects(action, fragment):
        try:
            action()
        except (ValueError, RuntimeError) as exc:
            assert fragment in str(exc), str(exc)
        else:
            raise AssertionError(fragment)

    model = {"type": "doc", "content": [
        {"type": "heading", "attrs": {"level": 1}, "content": [{"type": "text", "text": "合成标题"}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "合成正文",
            "marks": [{"type": "bold"}, {"type": "italic"}, {"type": "underline"}]}]},
        {"type": "bulletList", "content": [{"type": "listItem", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "列表项"}]}]}]},
        {"type": "table", "content": [{"type": "tableRow", "content": [
            {"type": "tableCell", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "表格"}]}]}]}]},
    ]}
    data = word.to_docx(model)
    decoded, _ = word.from_docx(data)
    assert "合成正文" in word.model_text(decoded)
    native = Document(io.BytesIO(data))
    assert native.paragraphs[1].runs[0].bold and native.paragraphs[1].runs[0].italic
    assert native.tables[0].cell(0, 0).text == "表格"
    rejects(lambda: word.validate_model({"type": "doc", "content": [{"type": "image", "attrs": {"src": "https://example.com"}}]}), "节点")
    rejects(lambda: word.validate_model({"type": "doc", "content": [{"type": "paragraph", "attrs": {"onclick": "x"}}]}), "样式")
    print("[PASS] Basic headings, marks, lists and table DOCX roundtrip / unknown node rejection")
    with tempfile.TemporaryDirectory(prefix="lawbuddy_word_") as temp:
        try:
            root = Path(temp)
            ensure_workspace(str(root / "ws"))
            case = create_case("Word", "civil", "Client")
            path = root / "source.docx"
            path.write_bytes(data)
            doc_id = import_documents(case["id"], [str(path)])[0]["documentId"]
            source = get_db().query_one("SELECT active_version_id FROM documents WHERE id = ?", (doc_id,))["active_version_id"]
            original = get_db().query_one("SELECT sha256 FROM document_versions WHERE id = ?", (source,))["sha256"]
            copy = word.create_work_copy(case["id"], document_id=doc_id)
            edited = word.text_model("人工修改的合成正文")
            saved = word.save_model(copy["id"], edited, copy["revision"], "save-once")
            assert saved["revision"] == copy["revision"] + 1
            assert word.save_model(copy["id"], edited, copy["revision"], "save-once")["revision"] == saved["revision"]
            rejects(lambda: word.save_model(copy["id"], model, copy["revision"], "other"), "VERSION_CONFLICT")
            rejects(lambda: word.save_model(copy["id"], model, saved["revision"], "save-once"), "IDEMPOTENCY_CONFLICT")
            assert get_db().query_one("SELECT sha256 FROM document_versions WHERE id = ?", (source,))["sha256"] == original
            blob = get_db().query_one(
                "SELECT blob_uri FROM word_work_copy_versions WHERE target_id = ? AND revision = ?",
                (copy["id"], saved["revision"]),
            )["blob_uri"]
            assert crypto.is_encrypted(root / "ws" / blob)
            exported = root / "draft.docx"
            word.export(copy["id"], str(exported))
            assert "人工修改" in Document(exported).paragraphs[0].text
            assert len(word.list_versions(copy["id"])) == 2
            diff = word.diff_versions(copy["id"], 1, 2)
            assert diff["stats"]["added"] > 0
            restored = word.restore_version(copy["id"], 1, 2)
            assert restored["revision"] == 3 and len(word.list_versions(copy["id"])) == 3
            patched = word.apply_patches(copy["id"], [{"quote": "合成正文", "replacement": "明确确认的修订"}], 3, "patch-once")
            assert "明确确认的修订" in word.model_text(patched["model"])
            rejects(lambda: word.apply_patches(copy["id"], [{"quote": "不存在的原文", "replacement": "x"}],
                                              patched["revision"], "bad-patch"), "SOURCE_MISMATCH")
            opened = word.open_editor(copy["id"])
            assert not opened["pending"] and editor.list_pending(case["id"]) == []
            autosaved = word.save_model(copy["id"], edited, patched["revision"], "recover-auto",
                                        "autosave", opened["id"])
            pending = editor.list_pending(case["id"])
            assert len(pending) == 1 and pending[0]["title"] == copy["title"]
            assert pending[0]["autosaveVersionNo"] == autosaved["revision"]
            assert word.open_editor(copy["id"])["pending"]
            history_count = len(word.list_versions(copy["id"]))
            editor.resolve(opened["id"], "discard")
            discarded = word.get_model(copy["id"])
            assert discarded["model"] == patched["model"]
            assert len(word.list_versions(copy["id"])) == history_count + 1
            assert editor.list_pending(case["id"]) == []
            reopened = word.open_editor(copy["id"])
            saved_auto = word.save_model(copy["id"], edited, discarded["revision"], "keep-auto",
                                         "autosave", reopened["id"])
            editor.resolve(reopened["id"], "recover")
            recovered = word.get_model(copy["id"])
            assert recovered["model"] == edited and recovered["revision"] == saved_auto["revision"] + 1
            normal = word.open_editor(copy["id"])
            rejects(lambda: word.save_model(copy["id"], model, recovered["revision"], "wrong-session",
                                             "autosave", "missing-session"), "编辑会话")
            assert word.get_model(copy["id"])["revision"] == recovered["revision"]
            editor.close_session(normal["id"])
            rejects(lambda: word.save_model(copy["id"], model, recovered["revision"], "closed-session",
                                             "autosave", normal["id"]), "编辑会话")
            assert editor.list_pending(case["id"]) == []
            print("[PASS] Word sessions / recovery metadata / discard and recover append history / closed-session rejection")
            archive_case(case["id"])
            rejects(lambda: word.save_model(copy["id"], model, recovered["revision"], "archived"), "只读")
            assert word.get_model(copy["id"])["revision"] == recovered["revision"]
            print("[PASS] Work-copy encryption / original hash / idempotency / conflict / versions / archive")
            from lawbuddy import docgen

            generated = create_case("", "other", "", default_mode="document_generation")
            template = docgen.import_template("# 测试\n## 事实与理由\n{{事实}}", "测试模板", "测试")
            draft = docgen.create_draft(generated["id"], template["id"], "测试", "合成目的")
            docgen.update_outline(draft["id"], [{"heading": "事实与理由", "points": "合成", "sourceDoc": ""}])
            docgen.confirm_outline(draft["id"])
            docgen.render_body(draft["id"])
            linked = word.create_work_copy(generated["id"], draft_id=draft["id"])
            synchronized = word.save_model(linked["id"], model, linked["revision"], "linked-save")
            latest_draft = docgen.get_draft(draft["id"])
            assert "合成正文" in latest_draft["bodyMd"] and latest_draft["status"] == "draft"
            # 模板字段已改为全部可选（未填以 xx 占位），不再以“必需字段”阻断定稿；
            # 此处正文已经过工作副本编辑，定稿必须走当前版本的人工核实门禁。
            rejects(lambda: docgen.confirm_draft(draft["id"]), "CONTENT_REVIEW_REQUIRED")
            output = docgen.export_draft(draft["id"], str(root), "docx")
            assert output["draftMarked"]
            exported_doc = Document(output["path"])
            assert exported_doc.paragraphs[1].runs[0].bold
            docgen.fill_slots(draft["id"], [{"name": "事实", "value": "合成事实", "sourceType": "user_confirmed"}])
            refreshed = word.create_work_copy(generated["id"], draft_id=draft["id"])
            changed = word.save_model(refreshed["id"], edited, refreshed["revision"], "review-gate")
            current_draft = docgen.get_draft(draft["id"])
            rejects(lambda: docgen.confirm_draft(draft["id"]), "CONTENT_REVIEW_REQUIRED")
            rejects(lambda: docgen.confirm_draft(draft["id"], True, "old-version"), "CONTENT_REVIEW_REQUIRED")
            assert docgen.confirm_draft(draft["id"], True, current_draft["updatedAt"])["status"] == "confirmed"
            print("[PASS] Edited facts require explicit current-version human confirmation")
            docgen.save_body(draft["id"], "其他窗口最新正文")
            rejects(lambda: word.save_model(linked["id"], edited, synchronized["revision"], "stale-draft"),
                    "VERSION_CONFLICT")
            assert docgen.get_draft(draft["id"])["bodyMd"] == "其他窗口最新正文"
            print("[PASS] Word/draft shared version projection / format export / concurrent draft conflict")
        finally:
            reset_database()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
