"""Deterministic mode isolation gates in an isolated encrypted workspace."""

from __future__ import annotations

import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch


def main() -> int:
    from lawbuddy import contract, docgen, service
    from lawbuddy.agent.runner import AgentRunner
    from lawbuddy.agent.tasks import create_task, update_lifecycle
    from lawbuddy.cases import CaseError, archive_case, create_case, get_case
    from lawbuddy.documents import import_documents
    from lawbuddy.rpc import RpcServer
    from lawbuddy.storage import get_db
    from lawbuddy.storage.db import reset_database
    from lawbuddy.workspace import ensure_workspace

    def rejects(action, fragment: str) -> None:
        try:
            action()
        except (RuntimeError, ValueError) as exc:
            assert fragment in str(exc), str(exc)
        else:
            raise AssertionError(f"Expected rejection: {fragment}")

    with tempfile.TemporaryDirectory(prefix="lawbuddy_modes_") as temp:
        try:
            ensure_workspace(str(Path(temp) / "ws"))
            server = RpcServer()
            service.register(server)
            legal = create_case("Legal", "civil", "Client")
            review = create_case("Contract", "contract", "", default_mode="contract_review",
                                 mode_config={"contractType": "待识别", "reviewBackground": "人工确认"})
            args = dict(title="", case_type="other", client_name="",
                        default_mode="document_generation", client_request_id="same-click")
            with ThreadPoolExecutor(max_workers=4) as pool:
                created = list(pool.map(lambda _: create_case(**args), range(4)))
            generated = created[0]
            assert len({c["id"] for c in created}) == 1
            assert generated["title"].startswith("文书生成 ")
            assert generated["clientName"] == ""
            rejects(lambda: create_case(**{**args, "title": "Different"}), "IDEMPOTENCY_CONFLICT")
            rejects(lambda: create_case("Missing client", "civil", ""), "委托人")
            assert get_case(review["id"])["modeConfig"]["contractType"] == "待识别"
            print("[PASS] Mode-specific creation and concurrent idempotency")
            with patch.object(AgentRunner, "start", side_effect=lambda runner_id, **_: __import__(
                "lawbuddy.agent.tasks", fromlist=["get_task"]).get_task(runner_id)):
                dialogue = server._handlers["agent.start"]({
                    "caseId": generated["id"], "mode": "document_generation",
                    "workflowKind": "docgen_dialogue", "input": "请帮我整理起草目标",
                })
            assert dialogue["mode"] == "document_generation"
            prompt_template = docgen.import_template("# 测试\n{{事实}}", "字段测试", "测试",
                                                     case_id=generated["id"])
            prompt = {"schemaVersion": 1, "templateId": prompt_template["id"], "templateVersion": 1,
                      "segments": [], "fieldValues": {"事实": "用户填写的合成信息"}}
            with patch.object(AgentRunner, "start", side_effect=lambda runner_id, **_: __import__(
                "lawbuddy.agent.tasks", fromlist=["get_task"]).get_task(runner_id)):
                structured = server._handlers["agent.start"]({
                    "caseId": generated["id"], "mode": "document_generation",
                    "workflowKind": "docgen_dialogue", "input": "整理信息",
                    "docgenInput": {"templateId": prompt_template["id"], "promptDocument": prompt},
                })
                rejects(lambda: server._handlers["agent.start"]({
                    "caseId": generated["id"], "mode": "document_generation",
                    "workflowKind": "docgen_dialogue", "input": "整理信息",
                    "docgenInput": {"templateId": prompt_template["id"],
                                    "promptDocument": {**prompt, "fieldValues": {"伪造字段": "x"}}},
                }), "字段值")
            assert structured["mode"] == "document_generation"
            from lawbuddy.agent.autonomous import _execute_tool, _system_prompt, _tools_for

            free = _execute_tool(
                {"case_id": generated["id"], "task_id": dialogue["id"], "mode": "document_generation"},
                "docgen_create_free_draft",
                {"docType": "测试意见书", "purpose": "合成目标", "headings": ["审查范围", "分析意见"]},
            )
            assert docgen.get_draft(free["draftId"])["outlineConfirmed"] is False
            names = {t["function"]["name"] for t in _tools_for(
                {"mode": "contract_review", "contract_input": {"workflowKind": "contract_dialogue"}})}
            assert "contract_create_review" not in names and "write_file" not in names
            # 阅卷模式不接 docgen，Word/Excel/PPT 成品文件只能靠沙箱工具落盘：
            # 点名交付文件时必须下发写文件与命令工具，否则模型会误报「无法生成 docx」
            deliver = {t["function"]["name"] for t in _tools_for(
                {"mode": "legal_review", "request": "请把答辩状导出成 Word 文件"})}
            assert {"write_file", "bash"} <= deliver
            analyze = {t["function"]["name"] for t in _tools_for(
                {"mode": "legal_review", "request": "分析本案争议焦点"})}
            assert "write_file" not in analyze and "bash" not in analyze
            print("[PASS] Direct docgen dialogue / free outline draft / contract read-only dialogue tools")

            # 阅卷认知提示：runner 必须传入案件类型/阶段，认知内核先于格式与预设交付。
            prompt_case = create_case(
                "Prompt", "criminal", "Client", client_role="被告人辩护人", stage="审查起诉",
            )
            prompt_task = create_task(prompt_case["id"], "legal_review", "分析指控与证据")
            prompt_runner = AgentRunner()
            with patch.object(prompt_runner, "_ensure_workers"):
                prompt_runner.start(prompt_task["id"])
            captured_state = {}

            def capture_prompt_state(state, emit, cancel_check):
                captured_state.update(state)
                return {"answer": ""}

            with patch("lawbuddy.agent.runner.run_autonomous", side_effect=capture_prompt_state):
                prompt_runner._run(prompt_task["id"])
            assert captured_state["case_type"] == "criminal"
            assert captured_state["case_stage"] == "审查起诉"
            criminal_prompt = _system_prompt(captured_state)
            assert "律师阅卷认知内核" in criminal_prompt
            assert "严格区分五层信息" in criminal_prompt
            assert "最强合理解释" in criminal_prompt and "同源重复" in criminal_prompt
            assert "刑事案件分析侧重" in criminal_prompt and "合理怀疑" in criminal_prompt
            assert "民商事案件分析侧重" not in criminal_prompt
            assert "程序阶段（案件设置）：审查起诉" in criminal_prompt
            assert "法条与类案仅在材料或检索结果提及时引用" not in criminal_prompt

            civil_prompt = _system_prompt({
                "mode": "legal_review", "case_type": "civil", "case_stage": "一审",
                "workflow_preset": "review_analysis",
            })
            assert "民商事案件分析侧重" in civil_prompt and "请求权基础" in civil_prompt
            assert "刑事案件分析侧重" not in civil_prompt
            assert civil_prompt.index("律师阅卷认知内核") < civil_prompt.index("呈现纪律")
            assert civil_prompt.index("呈现纪律") < civil_prompt.index("预设交付标准")
            assert "律师阅卷认知内核" not in _system_prompt({"mode": "contract_review"})
            print("[PASS] Legal reasoning core / criminal-civil branches / case context injection")

            for container in (legal, review, generated):
                for mode in ("legal_review", "contract_review", "document_generation"):
                    if mode == container["defaultMode"]:
                        task = create_task(container["id"], mode, "test")
                        update_lifecycle(task["id"], "CANCELLED")
                    else:
                        rejects(lambda: create_task(container["id"], mode, "test"), "TASK_MODE_MISMATCH")
                        rejects(lambda: server._handlers["agent.start"]({
                            "caseId": container["id"], "mode": mode, "input": "test",
                        }), "TASK_MODE_MISMATCH")
            print("[PASS] Domain and RPC cross-mode guards")

            path = Path(temp) / "contract.txt"
            path.write_text("第一条 标的\n甲方向乙方出售办公设备。", encoding="utf-8")
            doc = import_documents(legal["id"], [str(path)])[0]["documentId"]
            from lawbuddy.cases import copy_materials
            from lawbuddy.documents import get_pages

            rejects(lambda: copy_materials(legal["id"], review["id"], [doc], "copy-test", False), "确认")
            copied = copy_materials(legal["id"], review["id"], [doc], "copy-test", True)
            assert copied == copy_materials(legal["id"], review["id"], [doc], "copy-test", True)
            copied_id = copied["mappings"][0]["documentId"]
            assert copied_id != doc and get_pages(copied_id)[0]["text"] == get_pages(doc)[0]["text"]
            rejects(lambda: copy_materials(review["id"], legal["id"], [doc], "foreign-copy", True), "归属")
            print("[PASS] Confirmed material copying / new source IDs / local pages / idempotency / ownership")

            # 文书生成关联案件：只读共享材料与案件记忆（不复制、不改变来源归属）
            from lawbuddy import memory
            from lawbuddy.cases import readable_case_ids
            from lawbuddy.documents import list_task_documents
            from lawbuddy.search import search_pages

            memory.remember(
                scope="case", kind="fact", key="标的", value="办公设备",
                case_id=legal["id"],
                source={"documentId": doc, "pageNo": 1, "quote": "办公设备"},
            )
            linked = create_case("", "other", "", default_mode="document_generation",
                                 linked_case_id=legal["id"])
            assert get_case(linked["id"])["linkedCaseId"] == legal["id"]
            assert readable_case_ids(linked["id"]) == [linked["id"], legal["id"]]
            assert {d["id"] for d in list_task_documents(linked["id"])} == {doc}
            assert search_pages(linked["id"], "办公设备", extra_case_ids=(legal["id"],))[0]["documentId"] == doc
            shared_memories = memory.recall(
                case_id=linked["id"], query="标的", extra_case_ids=(legal["id"],),
            )
            assert shared_memories and shared_memories[0]["caseId"] == legal["id"]
            linked_tpl = docgen.import_template("标题\n{{事实}}", "Link", "Link")
            linked_draft = docgen.create_draft(linked["id"], linked_tpl["id"], "Link", "Link")
            docgen.fill_slots(linked_draft["id"], [{
                "name": "事实", "value": "办公设备", "sourceType": "case_evidence",
                "sourceRef": {"documentId": doc, "pageNo": 1, "quote": "办公设备"},
            }])
            rejects(lambda: create_case("Linked", "civil", "Client", default_mode="legal_review",
                                        linked_case_id=legal["id"]), "仅文书生成")
            print("[PASS] Linked document-generation task reads shared materials / memory read-only")
            rejects(lambda: contract.create_review(legal["id"], doc, "buyer"), "TASK_MODE_MISMATCH")
            tpl = docgen.import_template("标题\n{{事实}}\n事实与理由：\n{{事实}}", "Test", "Test")
            rejects(lambda: docgen.create_draft(legal["id"], tpl["id"], "Test", "Test"), "TASK_MODE_MISMATCH")
            draft = docgen.create_draft(generated["id"], tpl["id"], "Test", "Test")
            assert docgen.get_draft(draft["id"])["caseId"] == generated["id"]
            rejects(lambda: docgen.fill_slots(draft["id"], [{
                "name": "事实", "value": "办公设备", "sourceType": "case_evidence",
                "sourceRef": {"documentId": doc, "pageNo": 1, "quote": "办公设备"},
            }]), "无法核验")
            with get_db().transaction() as conn:
                # Simulate a pre-upgrade mixed container without a public mode-update API.
                conn.execute("UPDATE cases SET default_mode = 'legal_review' WHERE id = ?", (generated["id"],))
            assert docgen.get_draft(draft["id"])["id"] == draft["id"]
            rejects(lambda: docgen.update_outline(draft["id"], [{"heading": "Facts"}]), "TASK_MODE_MISMATCH")
            rejects(lambda: docgen.open_editor(draft["id"]), "TASK_MODE_MISMATCH")
            print("[PASS] Historical mixed-mode drafts remain readable, not writable")

            task = create_task(legal["id"], "legal_review", "resume")
            update_lifecycle(task["id"], "FAILED")
            runner = AgentRunner()
            with patch.object(runner, "_ensure_workers"):
                resumed = runner.start(task["id"])
            assert resumed["lifecycle"] == "RUNNING" and resumed["stage"] == "resume"
            update_lifecycle(task["id"], "FAILED")
            with get_db().transaction() as conn:
                conn.execute("UPDATE tasks SET mode = 'contract_review' WHERE id = ?", (task["id"],))
            rejects(lambda: AgentRunner.instance().start(task["id"]), "TASK_MODE_MISMATCH")
            archive_case(review["id"])
            rejects(lambda: create_task(review["id"], "contract_review", "test"), "只读")
            with patch("lawbuddy.cases.get_case", return_value={**legal, "defaultMode": "unknown"}):
                rejects(lambda: create_task(legal["id"], "legal_review", "test"), "TASK_MODE_UNCONFIRMED")
            print("[PASS] Resume, archive and unknown-mode guards")
        finally:
            reset_database()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
