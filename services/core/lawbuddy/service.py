"""Core 服务能力注册。

- core.handshake / ping / shutdown（DEV-FND-004）
- agent.graph_info：LangGraph 三模式子图骨架（DEV-AGT-002 前置）
- ocr.status：PaddleOCR 可用性探测（DEV-DOC-006 前置，惰性导入）
- workspace.ensure：工作空间初始化（DEV-FND-006 / REQ-CASE-001）
- case.*：案件管理（DEV-DOC-001 / REQ-CASE-002）
- document.*：导入管线与查询（DEV-DOC-003 / DEV-DOC-004）
"""

from __future__ import annotations

import platform
import time
from importlib.util import find_spec
from typing import Any

from . import PROTOCOL_VERSION, __version__
from .rpc import RpcServer

_started_at = time.time()


def _probe(module: str) -> bool:
    """惰性探测依赖可用性：只查模块规格，不执行导入（握手必须保持毫秒级）。"""
    try:
        return find_spec(module) is not None
    except Exception:
        return False


def register(server: RpcServer) -> None:
    @server.method("core.handshake")
    def handshake(_params: dict[str, Any]) -> dict[str, Any]:
        # apiKeyConfigured：是否已配置任一大模型服务商的 Key（仅布尔，不回传值）。
        # 握手先于 workspace.ensure：设置库未初始化时视为未配置，不因此报错
        from .agent.model_gateway import has_api_key

        try:
            key_ok = has_api_key()
        except Exception:  # noqa: BLE001  # 工作空间未初始化：视为无自配键
            key_ok = False
        return {
            "protocol": PROTOCOL_VERSION,
            "coreVersion": __version__,
            "python": platform.python_version(),
            "langgraph": _probe("langgraph"),
            "paddleocr": _probe("paddleocr"),
            "apiKeyConfigured": key_ok,
        }

    @server.method("ping")
    def ping(_params: dict[str, Any]) -> dict[str, Any]:
        return {"pong": True, "uptimeSec": round(time.time() - _started_at, 3)}

    @server.method("shutdown")
    def shutdown(_params: dict[str, Any]) -> dict[str, Any]:
        from . import content, editor

        # 正常退出：关闭编辑会话；强杀不经过此处 → 会话保持打开，下次启动提示恢复（DEV-MOD-009）
        try:
            editor.close_all_open()
        except Exception:
            pass
        # 正常退出：释放原件预览物料化的临时明文（REQ-DOC-014 / AC-SEC-007）
        try:
            content.close_all()
        except Exception:
            pass
        server.request_shutdown()
        return {"bye": True}

    @server.method("agent.graph_info")
    def graph_info(_params: dict[str, Any]) -> dict[str, Any]:
        from .agent.graph import MODE_GRAPHS, build_graph

        return {
            "modes": list(MODE_GRAPHS.keys()),
            "graphs": {mode: build_graph(mode).info() for mode in MODE_GRAPHS},
        }

    @server.method("ocr.status")
    def ocr_status(_params: dict[str, Any]) -> dict[str, Any]:
        from .ocr.engine import status

        return status()

    # ---- 工作空间（REQ-CASE-001 / 004） ------------------------------------

    def _resume_pending_ocr() -> None:
        """工作空间就绪后自动续跑待识别页（系统级自愈）。

        解析能力不随工作空间路径变化：Core 重启、切换工作空间后，库中仍有
        `ocr_status='pending'` 的页必须自动恢复，不能依赖渲染进程是否恰好
        打开了文档列表并轮询到。
        """
        try:
            from .ocr.pipeline import OcrManager

            manager = OcrManager.instance()
            manager.resume_after_switch()
            manager.resume_pending()
        except Exception:  # noqa: BLE001  # 自动续跑失败不得影响工作空间可用性
            pass

    @server.method("workspace.ensure")
    def workspace_ensure(params: dict[str, Any]) -> dict[str, Any]:
        from .workspace import WorkspaceError, ensure_workspace

        root_uri = str(params.get("rootUri") or "").strip()
        if not root_uri:
            raise WorkspaceError("rootUri is required")
        # seedDir：随包分发的技能 / MCP 种子目录（首次初始化时导入，可缺省）
        seed_dir = str(params.get("seedDir") or "").strip() or None
        result = ensure_workspace(root_uri, seed_dir=seed_dir)
        _resume_pending_ocr()
        return result

    @server.method("workspace.open")
    def workspace_open(params: dict[str, Any]) -> dict[str, Any]:
        """切换到指定目录的本地工作空间（REQ-CASE-004）。"""
        from .workspace import WorkspaceError, open_workspace

        root_uri = str(params.get("rootUri") or "").strip()
        if not root_uri:
            raise WorkspaceError("rootUri is required")
        seed_dir = str(params.get("seedDir") or "").strip() or None
        name = str(params.get("name") or "").strip() or "默认工作空间"
        result = open_workspace(root_uri, name=name, seed_dir=seed_dir)
        _resume_pending_ocr()
        return result

    # ---- 案件（DEV-DOC-001） ------------------------------------------------

    @server.method("case.create")
    def case_create(params: dict[str, Any]) -> dict[str, Any]:
        from .cases import create_case

        return create_case(
            title=str(params.get("title") or ""),
            case_type=str(params.get("caseType") or "other"),
            client_name=str(params.get("clientName") or ""),
            client_role=(str(params["clientRole"]) if params.get("clientRole") else None),
            stage=(str(params["stage"]) if params.get("stage") else None),
            default_mode=str(params.get("defaultMode") or "legal_review"),
            permission_preset=str(params.get("permissionPreset") or "strict_local"),
            client_request_id=params.get("clientRequestId"),
            mode_config=params.get("modeConfig"),
            linked_case_id=(str(params["linkedCaseId"]) if params.get("linkedCaseId") else None),
        )

    @server.method("case.copy_materials")
    def case_copy_materials(params: dict[str, Any]) -> dict[str, Any]:
        from .cases import copy_materials

        return copy_materials(str(params["fromCaseId"]), str(params["toCaseId"]),
                              params["documentIds"], params["clientRequestId"], params.get("confirmed"))

    @server.method("case.list")
    def case_list(params: dict[str, Any]) -> list[dict[str, Any]]:
        from .cases import list_cases

        return list_cases(include_archived=bool(params.get("includeArchived")))

    @server.method("case.get")
    def case_get(params: dict[str, Any]) -> dict[str, Any]:
        from .cases import get_case

        return get_case(str(params["caseId"]))

    @server.method("case.archive")
    def case_archive(params: dict[str, Any]) -> dict[str, Any]:
        from .cases import archive_case

        return archive_case(str(params["caseId"]))

    @server.method("case.restore")
    def case_restore(params: dict[str, Any]) -> dict[str, Any]:
        from .cases import restore_case

        return restore_case(str(params["caseId"]))

    @server.method("case.delete")
    def case_delete(params: dict[str, Any]) -> dict[str, Any]:
        from .cases import delete_case

        return delete_case(case_id=str(params["caseId"]))

    # ---- 文档（DEV-DOC-003 / 004） ------------------------------------------

    @server.method("document.import_files")
    def document_import(params: dict[str, Any]) -> list[dict[str, Any]]:
        from .documents import import_documents

        case_id = str(params.get("caseId") or "")
        paths = [str(p) for p in (params.get("paths") or []) if str(p).strip()]
        if not case_id or not paths:
            raise ValueError("caseId and paths are required")
        return import_documents(case_id, paths)

    @server.method("document.list")
    def document_list(params: dict[str, Any]) -> list[dict[str, Any]]:
        from .documents import list_task_documents

        return list_task_documents(str(params["caseId"]))

    @server.method("document.get_pages")
    def document_pages(params: dict[str, Any]) -> list[dict[str, Any]]:
        from .documents import get_pages

        return get_pages(str(params["documentId"]))

    @server.method("document.get_text_blocks")
    def document_text_blocks(params: dict[str, Any]) -> list[dict[str, Any]]:
        from .documents import get_page_text_blocks

        return get_page_text_blocks(str(params["documentId"]), int(params["pageNo"]))

    # ---- 原件内容会话（REQ-DOC-014：按格式原样预览） --------------------------
    # Renderer 只拿不透明 contentId 与字节分块，路径不出 Core 边界

    @server.method("document.open_content")
    def document_open_content(params: dict[str, Any]) -> dict[str, Any]:
        from .content import open_content

        page_no = params.get("pageNo")
        return open_content(
            str(params["documentId"]),
            int(page_no) if page_no else None,
        )

    @server.method("document.read_content")
    def document_read_content(params: dict[str, Any]) -> dict[str, Any]:
        from .content import read_content

        return read_content(
            str(params["contentId"]),
            offset=int(params.get("offset") or 0),
            length=int(params["length"]) if params.get("length") else None,
        )

    @server.method("document.close_content")
    def document_close_content(params: dict[str, Any]) -> dict[str, Any]:
        from .content import close_content

        return close_content(str(params["contentId"]))

    @server.method("document.create_editable_copy")
    def document_create_editable_copy(params: dict[str, Any]) -> dict[str, Any]:
        """为导入的 Excel/PPT 原件创建可编辑副本（原件只读）。"""
        from .artifacts import create_editable_copy

        return create_editable_copy(str(params["documentId"]))

    @server.method("document.export_bundle")
    def document_export_bundle(params: dict[str, Any]) -> dict[str, Any]:
        """证据卷宗装订：多份材料合并为带封面/目录/页码/书签的 PDF。"""
        from .bundle import export_bundle

        ids = params.get("documentIds")
        return export_bundle(
            str(params["caseId"]),
            [str(item) for item in ids] if isinstance(ids, list) else [],
            str(params["destPath"]),
        )

    @server.method("document.search")
    def document_search(params: dict[str, Any]) -> list[dict[str, Any]]:
        from . import cases
        from .search import search_pages

        case_id = str(params.get("caseId") or "")
        ids = cases.readable_case_ids(case_id) if case_id else []
        return search_pages(
            case_id=case_id,
            query=str(params.get("query") or ""),
            limit=int(params.get("limit") or 20),
            extra_case_ids=tuple(ids[1:]),
        )

    # ---- 加密文档解锁（REQ-DOC-004/011/013 / DEV-DOC-005） --------------------
    # 密码只经本方法入参进入隔离 Worker，不入库、不进日志、不进入 Prompt

    @server.method("document.request_unlock")
    def document_request_unlock(params: dict[str, Any]) -> dict[str, Any]:
        from .decrypt import DecryptError, unlock_document

        password = str(params.get("password") or "")
        if not password:
            raise ValueError("请输入文档密码")
        try:
            return unlock_document(str(params["documentId"]), password)
        except DecryptError as exc:
            raise ValueError(str(exc)) from exc

    # ---- OCR（DEV-DOC-006） ---------------------------------------------------

    @server.method("document.ocr.start")
    def ocr_start(params: dict[str, Any]) -> dict[str, Any]:
        from .ocr.pipeline import OcrManager

        return OcrManager.instance().start(str(params["documentId"]))

    @server.method("document.ocr.status")
    def ocr_status(params: dict[str, Any]) -> dict[str, Any]:
        from .ocr.pipeline import OcrManager

        return OcrManager.instance().status(str(params["documentId"]))

    @server.method("document.ocr.pause")
    def ocr_pause(params: dict[str, Any]) -> dict[str, Any]:
        from .ocr.pipeline import OcrManager

        return OcrManager.instance().pause(str(params["documentId"]))

    @server.method("document.ocr.resume")
    def ocr_resume(params: dict[str, Any]) -> dict[str, Any]:
        from .ocr.pipeline import OcrManager

        return OcrManager.instance().resume(str(params["documentId"]))

    @server.method("document.ocr.cancel")
    def ocr_cancel(params: dict[str, Any]) -> dict[str, Any]:
        from .ocr.pipeline import OcrManager

        return OcrManager.instance().cancel(str(params["documentId"]))

    # ---- Agent（DEV-AGT-001/002/005） -----------------------------------------

    @server.method("agent.start")
    def agent_start(params: dict[str, Any]) -> dict[str, Any]:
        from .agent.runner import AgentRunner
        from .agent.tasks import create_task
        from .cases import require_active_case

        case_id = str(params.get("caseId") or "")
        case = require_active_case(case_id)  # 归档案件不允许运行 Agent（REQ-CASE-006）
        workflow_preset = str(params.get("workflowPreset") or "") or None
        mode = str(params.get("mode") or "legal_review")
        from .cases import require_case_mode

        require_case_mode(case_id, mode)
        request_text = str(params.get("input") or "")
        client_position: dict[str, Any] | None = None
        if workflow_preset == "strategy" and mode == "legal_review":
            # 策略立场（本次任务覆盖，不回写案件设置）：输入明确或经对话框确认后传入；
            # 未传时回退案件委托人身份，仍缺失则由模型在任务理解中标注「立场未明确」
            stance = str(params.get("strategyStance") or "").strip()
            if stance:
                client_position = {"stance": stance}
        if mode == "contract_review":
            # 合同审查输入（PRD 14.2）：同步校验入参后经 clientPosition 通道随任务入库，
            # runner 注入 state.contract_input，node_plan 再做文档归属/状态校验
            from . import contract as contract_store

            inp = dict(params.get("contractInput") or {})
            if params.get("workflowKind") == "contract_dialogue":
                from .contract_workflow import require_ready, get_checklist

                overview = require_ready(str(inp.get("overviewId") or ""))
                if overview["caseId"] != case_id:
                    raise ValueError("概览不属于当前任务")
                snapshot = get_checklist(overview["id"])
                inp = {**overview["config"], "reviewerRole": "custom", "customRole": overview["config"]["stance"],
                       "workflowKind": "contract_dialogue", "overviewId": overview["id"],
                       "checklistSnapshotId": snapshot["id"] if snapshot else None}
            primary_doc = str(inp.get("primaryDocumentId") or "")
            reviewer_role = str(inp.get("reviewerRole") or "")
            custom_role = str(inp.get("customRole") or "").strip()
            risk_appetite = str(inp.get("riskAppetite") or "balanced")
            if not primary_doc:
                raise ValueError("请选择主合同文档")
            if reviewer_role not in contract_store.REVIEWER_ROLES:
                raise ValueError("请选择审查方角色")
            if reviewer_role == "custom" and not custom_role:
                raise ValueError("自定义审查方必须填写具体角色")
            if risk_appetite not in contract_store.RISK_APPETITES:
                raise ValueError(f"风险偏好不合法：{risk_appetite}")
            inp.update({
                "primaryDocumentId": primary_doc,
                "reviewerRole": reviewer_role,
                "customRole": custom_role or None,
                "riskAppetite": risk_appetite,
            })
            client_position = inp
            if not request_text.strip():
                request_text = "合同风险审查"
        elif mode == "document_generation":
            # 文书生成输入（PRD 15.2）：同步校验入参，模板归属与草稿创建由 node_plan 校验
            from . import docgen as docgen_store

            inp = dict(params.get("docgenInput") or {})
            revision_draft_id = str(inp.get("revisionDraftId") or "").strip()
            if revision_draft_id:
                # 修订分叉（交付物迭代改写）：不建新草稿；提纲确认/正文存在校验由
                # node_plan 修订分叉完成，此处仅做归属与必填校验
                revision_instruction = str(inp.get("revisionInstruction") or "").strip()
                if not revision_instruction:
                    raise ValueError("请填写修订意见（说明需要修改的内容）")
                try:
                    draft = docgen_store.get_draft(revision_draft_id)
                except docgen_store.DocGenError as exc:
                    raise ValueError(str(exc)) from exc
                if draft["caseId"] != case_id:
                    raise ValueError("草稿不属于当前案件")
                client_position = {
                    "revisionDraftId": revision_draft_id,
                    "revisionInstruction": revision_instruction,
                }
                if not request_text.strip():
                    request_text = revision_instruction
            elif params.get("workflowKind") == "docgen_dialogue":
                template_id = str(inp.get("templateId") or "")
                template = None
                if template_id:
                    template = docgen_store.get_template(template_id)
                    if template["caseId"] not in (None, case_id) or template["status"] != "active":
                        raise ValueError("所选模板不可用于当前任务")
                doc_type = str(inp.get("docType") or "").strip()
                if not doc_type and template is not None:
                    doc_type = str(template.get("docType") or "").strip()
                doc_type = doc_type or "法律文书"
                # 只选模板、不写任何文本也能发起起草：内部生成任务描述，
                # 由模型先通读关联案件材料再据材料起草（升级方案 §4.1）
                if not request_text.strip():
                    request_text = (
                        f"请根据当前关联案件的材料与案件记忆，按照已选模板起草《{doc_type}》。"
                        "可从原文确认的信息自动使用；普通缺失信息写 xx；"
                        "仅在必要决策不明确时向我提问。"
                        if template is not None else
                        f"请根据当前关联案件的材料与案件记忆起草《{doc_type}》。"
                        "可从原文确认的信息自动使用；普通缺失信息写 xx；"
                        "仅在必要决策不明确时向我提问。"
                    )
                purpose = str(inp.get("purpose") or "").strip() or request_text.strip()
                client_position = {"workflowKind": "docgen_dialogue", "freeDraft": not bool(template_id),
                                   "templateId": template_id or None,
                                   "docType": doc_type, "purpose": purpose[:4000]}
                prompt = inp.get("promptDocument")
                if prompt is not None:
                    if not isinstance(prompt, dict) or set(prompt) - {
                        "schemaVersion", "templateId", "templateVersion", "segments", "fieldValues",
                    }:
                        raise ValueError("起草字段结构不合法")
                    if not template_id or prompt.get("schemaVersion") != 1 or prompt.get("templateId") != template_id or (
                        prompt.get("templateVersion") != template["versionNo"]
                    ):
                        raise ValueError("起草模板版本不匹配")
                    values = prompt.get("fieldValues")
                    known = {field["name"] for field in template["fields"]}
                    if not isinstance(values, dict) or set(values) - known or any(
                        not isinstance(value, str) or len(value) > 100_000 for value in values.values()
                    ):
                        raise ValueError("起草字段值不合法")
                    # Rebuild labels from the trusted template; renderer labels never grant fact status.
                    client_position["promptDocument"] = {
                        "schemaVersion": 1, "templateId": template_id,
                        "templateVersion": template["versionNo"], "fieldValues": values,
                        "segments": [{"type": "field", "name": field["name"],
                                      "label": field["name"], "required": field["required"]}
                                     for field in template["fields"]],
                    }
            else:
                template_id = str(inp.get("templateId") or "")
                doc_type = str(inp.get("docType") or "").strip()
                purpose = str(inp.get("purpose") or "").strip()
                if not template_id:
                    raise ValueError("请选择文书模板")
                try:
                    tpl = docgen_store.get_template(template_id)
                except docgen_store.DocGenError as exc:
                    raise ValueError(str(exc)) from exc
                if tpl["status"] != "active":
                    raise ValueError("所选模板解析失败，不可用于生成")
                # 文书类型与请求事项均非必填：缺省取模板文书类型与用户输入，不强制用户填写
                if not doc_type:
                    doc_type = str(tpl.get("docType") or "法律文书").strip() or "法律文书"
                if not purpose:
                    purpose = request_text.strip() or f"起草{doc_type}"
                if tpl["docType"] != doc_type and not bool(inp.get("confirmMismatch")):
                    raise ValueError(
                        f"所选模板为「{tpl['docType']}」模板，与文书类型「{doc_type}」不匹配；确认使用请重试"
                    )
                inp.update({"templateId": template_id, "docType": doc_type, "purpose": purpose})
                client_position = inp
                if not request_text.strip():
                    request_text = f"生成{doc_type}"
        # 会话持久化（V26）：传入 conversationId 校验归属（同会话续聊），
        # 缺省新建会话——每轮问答全文由 runner 归档，替代有损的 history 链路
        from . import conversation as conversation_store

        conversation_id = conversation_store.ensure_conversation(
            str(params.get("conversationId") or "").strip() or None, case_id
        )
        # 输入坞随问附件（临时性补充材料）：不导入案件文档、不走 OCR，内存随任务
        # 传给模型（不落库、不进检查点/会话归档）；图片按模型视觉能力门禁（contracts
        # MODEL_VISION_SUPPORT 的后端等价表，双端校验防绕过 UI 直发）
        raw_attachments = params.get("attachments") or []
        attachments: list[dict[str, Any]] = []
        if raw_attachments:
            if not isinstance(raw_attachments, list):
                raise ValueError("attachments 参数不合法")
            from .agent import model_gateway

            # 未指定模型时按注册表默认模型解析（内置模型目录已清空，由用户自行添加）
            model_id = model_gateway.resolve_model(
                str((params.get("modelPolicy") or {}).get("modelId") or "")
            )
            # 上限与 UI/主进程一致（最多 6 个）：超出显式拒绝，不静默截断丢材料
            if len(raw_attachments) > 6:
                raise ValueError(f"随问附件最多支持 6 个，当前 {len(raw_attachments)} 个，请移除部分附件后重试")
            for att in raw_attachments:
                if not isinstance(att, dict):
                    continue
                name = str(att.get("name") or "附件").strip()[:120]
                mime = str(att.get("mime") or "").strip().lower()
                data = str(att.get("dataBase64") or "").strip()
                if not mime or not data:
                    continue
                # base64 后约 8MB 二进制 ≈ 1100 万字符，超限直接拒绝（防超大载荷）
                if len(data) > 11_000_000:
                    raise ValueError(f"附件过大：{name}（单个附件上限 8MB）")
                if mime.startswith("image/") and not model_gateway.supports_vision(model_id):
                    raise ValueError(
                        "尚未添加可用模型，无法附带图片：请先在「模型设置」中添加支持图片理解的模型"
                        if not model_id
                        else f"当前模型（{model_id}）不支持图片理解，请切换模型后再上传图片附件"
                    )
                attachments.append({"name": name, "mime": mime, "dataBase64": data})
        task = create_task(
            case_id=case_id,
            mode=mode,
            request_text=request_text,
            model_policy=dict(params.get("modelPolicy") or {}),
            workflow_preset=workflow_preset,
            client_position=client_position,
            # DEV-AGT-006：任务级预算覆盖（轮次/时间/调用次数/token），缺省用系统默认上限
            budget=dict(params.get("budget") or {}),
            # 本次任务绑定技能（/ 菜单选中）：校验存在且启用后随任务入库
            skills=list(params.get("skills") or []),
            # 跨任务对话历史（无会话时的兜底；有 conversation 时 runner 用 Core 全文历史覆盖）
            history=list(params.get("history") or []),
            conversation_id=conversation_id,
        )
        return AgentRunner.instance().start(task["id"], attachments=attachments or None)

    @server.method("agent.get_task")
    def agent_get_task(params: dict[str, Any]) -> dict[str, Any]:
        from .agent.tasks import get_task

        return get_task(str(params["taskId"]))

    @server.method("agent.resume")
    def agent_resume(params: dict[str, Any]) -> dict[str, Any]:
        from .agent.runner import AgentRunner

        return AgentRunner.instance().start(str(params["taskId"]))

    @server.method("agent.get_events")
    def agent_get_events(params: dict[str, Any]) -> list[dict[str, Any]]:
        from .agent.tasks import get_events

        return get_events(
            task_id=str(params["taskId"]),
            after_seq=int(params.get("afterSeq") or 0),
        )

    @server.method("agent.cancel")
    def agent_cancel(params: dict[str, Any]) -> dict[str, Any]:
        from .agent.runner import AgentRunner

        return AgentRunner.instance().cancel(str(params["taskId"]))

    @server.method("agent.answer_human_questions")
    def agent_answer_human_questions(params: dict[str, Any]) -> dict[str, Any]:
        from .agent import human_questions
        from .agent.tasks import get_task

        task_id = str(params.get("taskId") or "")
        if not task_id:
            raise ValueError("缺少任务 ID")
        human_questions.answer(
            task_id,
            str(params.get("questionSetId") or ""),
            params.get("answers"),
        )
        return get_task(task_id)

    @server.method("agent.pending_questions")
    def agent_pending_questions(params: dict[str, Any]) -> dict[str, Any] | None:
        from .agent import human_questions

        return human_questions.get_pending(str(params.get("taskId") or ""))

    # ---- 策略确认（REQ-STR-005：AI 不自动选定最终方案，用户确认入审计） ---------

    @server.method("strategy.decide")
    def strategy_decide(params: dict[str, Any]) -> dict[str, Any]:
        from . import audit
        from .agent.tasks import get_task
        from .storage import get_db

        task_id = str(params["taskId"])
        plan_title = str(params["planTitle"]).strip()
        decision = str(params.get("decision") or "adopted")
        if not plan_title:
            raise ValueError("方案名称不能为空")
        if decision not in ("adopted", "rejected"):
            raise ValueError(f"无效决定：{decision}")
        task = get_task(task_id)  # 校验任务存在（审计关联案件）
        with get_db().transaction() as conn:
            audit.append(
                conn,
                action=f"strategy.{decision}",
                resource_type="task",
                resource_id=task_id,
                case_id=task["caseId"],
                metadata={"planTitle": plan_title, "decision": decision,
                          "workflowPreset": task.get("workflowPreset")},
            )
        return {"ok": True, "decision": decision, "planTitle": plan_title}

    # ---- 设置（模型 API Key 等） ----------------------------------------------

    @server.method("settings.get")
    def settings_get(params: dict[str, Any]) -> Any:
        from .agent.tasks import get_setting, is_sensitive_setting

        key = str(params.get("key") or "")
        value = get_setting(key)
        # 敏感键只回传是否已配置，不回传值本身（Renderer 无需明文）。
        # 判定必须动态：自定义服务商的 model.<providerId>ApiKey 不在静态清单里
        if is_sensitive_setting(key):
            return {"configured": bool(value)}
        return value

    @server.method("settings.set")
    def settings_set(params: dict[str, Any]) -> dict[str, Any]:
        from .agent.model_gateway import provider_for_key_setting, set_api_key, set_provider
        from .agent.tasks import set_setting

        key = str(params.get("key") or "")
        value = params.get("value")
        # 服务商专属键按「键」反查服务商写入（model.deepseekApiKey → deepseek），
        # 不能按当前 model.provider 推断，否则会把 Key 写进别的服务商的键
        provider_of_key = provider_for_key_setting(key)
        if key == "model.apiKey":
            set_api_key(str(value or ""))
        elif provider_of_key is not None:
            set_api_key(str(value or ""), provider_of_key)
        elif key == "model.provider":
            set_provider(str(value or ""))
        else:
            set_setting(key, value)
        return {"ok": True}

    # ---- 模型服务商（内置官方预设 + 自定义 API） -------------------------------
    # 目录、能力（视觉/思考档位/上下文/输出上限）由 Core 注册表统一下发，
    # Renderer 不再复制硬编码表；API Key 只回传 configured，不回传值。

    @server.method("model.catalog")
    def model_catalog(_params: dict[str, Any]) -> dict[str, Any]:
        from .agent import model_gateway, model_registry

        data = model_registry.catalog()
        for conf in data["providers"]:
            try:
                conf["configured"] = model_gateway.has_api_key(str(conf["id"]))
            except Exception:  # noqa: BLE001  # 工作空间未初始化：按未配置处理
                conf["configured"] = False
        return data

    @server.method("model.save_provider")
    def model_save_provider(params: dict[str, Any]) -> dict[str, Any]:
        from .agent import model_registry

        payload = params.get("provider")
        if not isinstance(payload, dict):
            raise ValueError("服务商配置不合法")
        saved = model_registry.save_provider(payload)
        # API Key 与配置一并提交时按服务商专属键写入（传空串表示清除）
        if "apiKey" in params:
            from .agent.model_gateway import set_api_key

            set_api_key(str(params.get("apiKey") or ""), str(saved["id"]))
        return saved

    @server.method("model.remove_provider")
    def model_remove_provider(params: dict[str, Any]) -> dict[str, Any]:
        from .agent import model_registry

        removed = model_registry.remove_provider(str(params.get("providerId") or ""))
        return {"removed": removed}

    @server.method("model.reset_provider")
    def model_reset_provider(params: dict[str, Any]) -> dict[str, Any]:
        from .agent import model_registry

        reset = model_registry.reset_provider(str(params.get("providerId") or ""))
        return {"reset": reset}

    @server.method("model.set_provider")
    def model_set_provider(params: dict[str, Any]) -> dict[str, Any]:
        from .agent.model_gateway import set_provider

        set_provider(str(params.get("providerId") or ""))
        return {"ok": True}

    @server.method("model.fetch_models")
    def model_fetch_models(params: dict[str, Any]) -> list[dict[str, Any]]:
        """「获取可用模型」：按草稿地址与密钥探测 GET /models。"""
        from .agent import model_gateway

        return model_gateway.list_provider_models(
            str(params.get("providerId") or ""),
            base=(str(params["base"]) if params.get("base") else None),
            key=(str(params["apiKey"]) if params.get("apiKey") else None),
            protocol=(str(params["protocol"]) if params.get("protocol") else None),
        )

    @server.method("model.test_connection")
    def model_test_connection(params: dict[str, Any]) -> dict[str, Any]:
        from .agent import model_gateway

        return model_gateway.test_connection(
            str(params.get("providerId") or ""),
            base=(str(params["base"]) if params.get("base") else None),
            key=(str(params["apiKey"]) if params.get("apiKey") else None),
            protocol=(str(params["protocol"]) if params.get("protocol") else None),
            model=(str(params["model"]) if params.get("model") else None),
            check_vision=bool(params.get("checkVision")),
        )

    # ---- 审批与权限（DEV-AGT-004） ---------------------------------------------

    @server.method("approval.respond")
    def approval_respond(params: dict[str, Any]) -> dict[str, Any]:
        from .policy import respond

        return respond(
            approval_id=str(params["approvalId"]),
            decision=str(params["decision"]),
            approved_payload_hash=(str(params["approvedPayloadHash"]) if params.get("approvedPayloadHash") else None),
        )

    @server.method("permission.revoke")
    def permission_revoke(params: dict[str, Any]) -> dict[str, Any]:
        from .policy import revoke_grants

        count = revoke_grants(str(params["capability"]), str(params["scopeId"]))
        return {"revoked": count}

    # ---- Claim Ledger（DEV-AGT-008） -------------------------------------------

    @server.method("claim.list")
    def claim_list(params: dict[str, Any]) -> list[dict[str, Any]]:
        from .citations import list_claims

        return list_claims(
            task_id=(str(params["taskId"]) if params.get("taskId") else None),
            case_id=(str(params["caseId"]) if params.get("caseId") else None),
            status=(str(params["status"]) if params.get("status") else None),
        )

    @server.method("claim.verify")
    def claim_verify(params: dict[str, Any]) -> dict[str, Any]:
        from .citations import verify_claim

        return verify_claim(
            claim_id=str(params["claimId"]),
            decision=str(params.get("decision") or ""),
            note=(str(params["note"]) if params.get("note") else ""),
        )

    @server.method("claim.recheck")
    def claim_recheck(params: dict[str, Any]) -> dict[str, Any]:
        from .citations import recheck_claim

        return recheck_claim(str(params["claimId"]))

    @server.method("claim.resolve_conflict")
    def claim_resolve_conflict(params: dict[str, Any]) -> dict[str, Any]:
        from .citations import resolve_conflict

        return resolve_conflict(
            claim_id=str(params["claimId"]),
            decision=str(params.get("decision") or ""),
            note=(str(params.get("note")) if params.get("note") else ""),
        )

    # ---- 任务预算（DEV-AGT-006） ------------------------------------------------

    @server.method("agent.budget")
    def agent_budget(params: dict[str, Any]) -> dict[str, Any]:
        """任务预算快照：上限、已用量、阶段与原因（A-002/A-003/A-004/A-013）。"""
        from .agent import budget

        return budget.snapshot(str(params["taskId"]))

    # ---- 备份与恢复（DEV-FND-006 / DB-006） -------------------------------------

    @server.method("backup.create")
    def backup_create(params: dict[str, Any]) -> dict[str, Any]:
        """创建加密备份（在线）。口令由调用方经用户交互获取，不进日志。"""
        from .storage import create_backup

        return create_backup(
            dest_dir=str(params.get("destDir") or ""),
            passphrase=(str(params["passphrase"]) if params.get("passphrase") else None),
        )

    @server.method("backup.verify")
    def backup_verify(params: dict[str, Any]) -> dict[str, Any]:
        """校验备份副本（完整性、数量、抽样哈希、权限、审计链，DB-006）。"""
        from .storage import verify_backup

        return verify_backup(
            db_path=str(params.get("dbPath") or ""),
            key_file=str(params.get("keyFile") or ""),
            passphrase=str(params.get("passphrase") or ""),
            compare_live=bool(params.get("compareLive", True)),
        )

    # ---- 记忆（DEV-AGT-007 / M-001~M-006） -------------------------------------

    @server.method("memory.list")
    def memory_list(params: dict[str, Any]) -> list[dict[str, Any]]:
        from . import cases, memory

        case_id = str(params["caseId"]) if params.get("caseId") else None
        extra: tuple[str, ...] = ()
        if case_id and params.get("includeShared"):
            extra = tuple(cases.readable_case_ids(case_id)[1:])
        return memory.list_memories(
            case_id=case_id,
            scope=(str(params["scope"]) if params.get("scope") else None),
            status=(str(params["status"]) if params.get("status") else None),
            limit=int(params.get("limit") or 100),
            extra_case_ids=extra,
        )

    @server.method("memory.recall")
    def memory_recall(params: dict[str, Any]) -> list[dict[str, Any]]:
        from . import cases, memory

        case_id = str(params["caseId"])
        return memory.recall(
            case_id=case_id,
            query=str(params.get("query") or ""),
            limit=int(params.get("limit") or 10),
            extra_case_ids=tuple(cases.readable_case_ids(case_id)[1:]),
        )

    @server.method("memory.remember")
    def memory_remember(params: dict[str, Any]) -> dict[str, Any]:
        from . import memory

        return memory.remember(
            scope=str(params.get("scope") or "case"),
            kind=str(params.get("kind") or "note"),
            key=str(params.get("key") or ""),
            value=str(params.get("value") or ""),
            case_id=(str(params["caseId"]) if params.get("caseId") else None),
            source=(params.get("source") if isinstance(params.get("source"), dict) else None),
        )

    @server.method("memory.correct")
    def memory_correct(params: dict[str, Any]) -> dict[str, Any]:
        from . import memory

        return memory.correct(
            memory_id=str(params["memoryId"]),
            value=str(params.get("value") or ""),
            note=str(params.get("note") or ""),
        )

    @server.method("memory.forget")
    def memory_forget(params: dict[str, Any]) -> dict[str, Any]:
        from . import memory

        return memory.forget(str(params["memoryId"]), note=str(params.get("note") or ""))

    # ---- 合同审查（DEV-MOD-005/006 / PRD §14） --------------------------------

    @server.method("contract.list_reviews")
    def contract_list_reviews(params: dict[str, Any]) -> list[dict[str, Any]]:
        from . import contract as contract_store

        return contract_store.list_reviews(str(params["caseId"]))

    @server.method("contract.get_review")
    def contract_get_review(params: dict[str, Any]) -> dict[str, Any]:
        from . import contract as contract_store

        return contract_store.build_review_payload(str(params["reviewId"]))

    @server.method("contract.decide_risk")
    def contract_decide_risk(params: dict[str, Any]) -> dict[str, Any]:
        from . import contract as contract_store
        from .cases import require_active_case

        risk_id = str(params["riskId"])
        decision = str(params.get("decision") or "")
        edited_text = params.get("editedText")
        review = contract_store.get_review(contract_store.get_risk_item(risk_id)["reviewId"])
        require_active_case(review["caseId"])  # 归档案件只读（REQ-CASE-006）
        contract_store.decide_risk(
            risk_id=risk_id,
            decision=decision,
            edited_text=(str(edited_text) if edited_text is not None else None),
        )
        return contract_store.build_review_payload(review["id"])

    @server.method("contract.create_overview")
    def contract_create_overview(params: dict[str, Any]) -> dict[str, Any]:
        from .contract_workflow import create_overview

        return create_overview(str(params["caseId"]))

    @server.method("contract.get_overview")
    def contract_get_overview(params: dict[str, Any]) -> dict[str, Any]:
        from .contract_workflow import get_overview

        return get_overview(str(params["overviewId"]))

    @server.method("contract.update_overview")
    def contract_update_overview(params: dict[str, Any]) -> dict[str, Any]:
        from .contract_workflow import update_overview

        return update_overview(str(params["overviewId"]), params["config"], int(params["expectedRevision"]))

    @server.method("contract.get_checklist")
    def contract_get_checklist(params: dict[str, Any]) -> dict[str, Any] | None:
        from .contract_workflow import get_checklist

        return get_checklist(str(params["overviewId"]), params.get("snapshotId"))

    @server.method("contract.generate_checklist")
    def contract_generate_checklist(params: dict[str, Any]) -> dict[str, Any]:
        from .contract_workflow import start_checklist

        return start_checklist(str(params["overviewId"]), int(params["expectedRevision"]), params.get("modelPolicy"))

    @server.method("contract.update_checklist")
    def contract_update_checklist(params: dict[str, Any]) -> dict[str, Any]:
        from .contract_workflow import update_checklist

        return update_checklist(str(params["overviewId"]), params["items"], int(params["expectedRevision"]))

    @server.method("contract.confirm_checklist")
    def contract_confirm_checklist(params: dict[str, Any]) -> dict[str, Any]:
        from .contract_workflow import confirm_checklist

        return confirm_checklist(str(params["overviewId"]), int(params["expectedRevision"]))

    @server.method("contract.start_risk_review")
    def contract_start_risk_review(params: dict[str, Any]) -> dict[str, Any]:
        from .contract_workflow import start_risk_review

        return start_risk_review(str(params["overviewId"]), str(params["snapshotId"]),
                                 params["clientRequestId"], params.get("modelPolicy"))

    @server.method("contract.get_risk_run")
    def contract_get_risk_run(params: dict[str, Any]) -> dict[str, Any] | None:
        from .contract_workflow import get_risk_run

        return get_risk_run(str(params["overviewId"]))

    @server.method("contract.apply_risk_suggestion")
    def contract_apply_risk_suggestion(params: dict[str, Any]) -> dict[str, Any]:
        from .contract_workflow import apply_risk_suggestion

        return apply_risk_suggestion(str(params["overviewId"]), str(params["riskId"]), str(params["targetId"]),
                                     int(params["expectedRevision"]), str(params["replacement"]), params["clientRequestId"])

    @server.method("contract.undo")
    def contract_undo(params: dict[str, Any]) -> dict[str, Any]:
        from . import contract as contract_store
        from .cases import require_active_case

        review_id = str(params["reviewId"])
        require_active_case(contract_store.get_review(review_id)["caseId"])
        contract_store.undo(review_id)
        return contract_store.build_review_payload(review_id)

    @server.method("contract.redo")
    def contract_redo(params: dict[str, Any]) -> dict[str, Any]:
        from . import contract as contract_store
        from .cases import require_active_case

        review_id = str(params["reviewId"])
        require_active_case(contract_store.get_review(review_id)["caseId"])
        contract_store.redo(review_id)
        return contract_store.build_review_payload(review_id)

    @server.method("contract.list_work_copies")
    def contract_list_work_copies(params: dict[str, Any]) -> list[dict[str, Any]]:
        from . import contract as contract_store

        return contract_store.list_work_copies(str(params["reviewId"]))

    @server.method("contract.diff_work_copies")
    def contract_diff_work_copies(params: dict[str, Any]) -> dict[str, Any]:
        from . import contract as contract_store

        to_no = params.get("toVersionNo")
        return contract_store.diff_work_copies(
            str(params["reviewId"]),
            from_version_no=int(params.get("fromVersionNo") or 0),
            to_version_no=int(to_no) if to_no is not None else None,
        )

    # ---- 派生文档编辑会话（DEV-MOD-009：崩溃恢复入口 / PRD §18.3） ----------------

    @server.method("editor.open")
    def editor_open(params: dict[str, Any]) -> dict[str, Any]:
        from . import docgen

        return docgen.open_editor(str(params["draftId"]))

    @server.method("editor.close")
    def editor_close(params: dict[str, Any]) -> dict[str, Any]:
        from . import docgen

        return docgen.close_editor(str(params["sessionId"]))

    @server.method("editor.pending")
    def editor_pending(params: dict[str, Any]) -> list[dict[str, Any]]:
        from . import editor

        case_id = params.get("caseId")
        return editor.list_pending(str(case_id) if case_id else None)

    @server.method("editor.resolve")
    def editor_resolve(params: dict[str, Any]) -> dict[str, Any]:
        from . import editor

        return editor.resolve(str(params["sessionId"]), str(params.get("action") or ""))

    # ---- 文书生成（DEV-MOD-007/008 / PRD §15、§16.3） --------------------------
    # 草稿由 agent.start(document_generation) 创建；此处为模板管理、槽位补全、
    # 提纲确认、渲染/定稿/导出与版本（REQ-GEN-003/004/005/007/009/010）。

    @server.method("docgen.import_docx_template")
    def docgen_import_docx_template(params: dict[str, Any]) -> dict[str, Any]:
        """上传 .docx 范本作为自定义模板（升级方案 §6.1 / §10 阶段三）。

        文件字节由 Main 进程读取后经 RPC 传入（路径不进 Core 的持久化状态），
        原件加密写入工作空间内容存储，模板记录来源格式与字段映射。
        """
        import base64

        from . import docgen

        payload = params.get("fileBase64")
        if not isinstance(payload, str) or not payload.strip():
            raise ValueError("缺少模板文件内容")
        try:
            data = base64.b64decode(payload, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("模板文件内容不是有效的 base64 编码") from exc
        tpl = docgen.import_docx_template(
            data,
            title=str(params.get("title") or ""),
            doc_type=str(params.get("docType") or ""),
            case_id=(str(params["caseId"]) if params.get("caseId") else None),
            source_name=(str(params["sourceName"]) if params.get("sourceName") else None),
            template_mode=(str(params["templateMode"]) if params.get("templateMode") else None),
            base_template_id=(str(params["baseTemplateId"]) if params.get("baseTemplateId") else None),
        )
        return {"template": tpl, "warnings": list(tpl.get("formatWarnings") or [])}

    @server.method("docgen.render_docx")
    def docgen_render_docx(params: dict[str, Any]) -> dict[str, Any]:
        """按 DOCX 模板原格式生成（升级方案 §6.2 / §10 阶段三）。"""
        from . import docgen

        body = params.get("bodyMd")
        return docgen.render_docx(
            str(params["draftId"]),
            body_md=(str(body) if body else None),
            allow_unconfirmed=bool(params.get("allowUnconfirmed")),
        )

    @server.method("docgen.import_template")
    def docgen_import_template(params: dict[str, Any]) -> dict[str, Any]:
        from . import docgen

        return docgen.import_template(
            md_text=str(params.get("templateMd") or ""),
            title=str(params.get("title") or ""),
            doc_type=str(params.get("docType") or ""),
            case_id=(str(params["caseId"]) if params.get("caseId") else None),
            source_name=(str(params["sourceName"]) if params.get("sourceName") else None),
            template_mode=str(params.get("templateMode") or "strict"),
            base_template_id=(str(params["baseTemplateId"]) if params.get("baseTemplateId") else None),
        )

    @server.method("docgen.list_templates")
    def docgen_list_templates(params: dict[str, Any]) -> list[dict[str, Any]]:
        from . import docgen

        return docgen.list_templates(str(params.get("caseId") or "") or None)

    @server.method("docgen.install_best_practices")
    def docgen_install_best_practices(params: dict[str, Any]) -> list[dict[str, Any]]:
        from .best_practices import install

        return install(str(params["caseId"]))

    @server.method("docgen.list_best_practices")
    def docgen_list_best_practices(params: dict[str, Any]) -> list[dict[str, Any]]:
        from .best_practices import list_templates

        return list_templates(str(params["caseId"]))

    @server.method("docgen.update_template_fields")
    def docgen_update_template_fields(params: dict[str, Any]) -> dict[str, Any]:
        from . import docgen

        return docgen.update_template_fields(
            str(params["templateId"]),
            [f for f in (params.get("fields") or []) if isinstance(f, dict)],
        )

    @server.method("docgen.delete_template")
    def docgen_delete_template(params: dict[str, Any]) -> dict[str, Any]:
        from . import docgen

        return docgen.delete_template(str(params["templateId"]))

    @server.method("docgen.list_template_versions")
    def docgen_list_template_versions(params: dict[str, Any]) -> list[dict[str, Any]]:
        from . import docgen

        return docgen.list_template_versions(str(params["templateId"]))

    @server.method("docgen.diff_template_versions")
    def docgen_diff_template_versions(params: dict[str, Any]) -> dict[str, Any]:
        from . import docgen

        to_no = params.get("toVersionNo")
        return docgen.diff_template_versions(
            str(params["templateId"]),
            int(params["fromVersionNo"]),
            to_version_no=(int(to_no) if to_no else None),
        )

    @server.method("docgen.rollback_template")
    def docgen_rollback_template(params: dict[str, Any]) -> dict[str, Any]:
        from . import docgen

        return docgen.rollback_template(str(params["templateId"]), int(params["versionNo"]))

    @server.method("docgen.get_draft")
    def docgen_get_draft(params: dict[str, Any]) -> dict[str, Any]:
        from . import docgen

        return docgen.get_draft(str(params["draftId"]))

    @server.method("docgen.list_drafts")
    def docgen_list_drafts(params: dict[str, Any]) -> list[dict[str, Any]]:
        from . import docgen

        return docgen.list_drafts(str(params["caseId"]))

    @server.method("docgen.fill_slots")
    def docgen_fill_slots(params: dict[str, Any]) -> dict[str, Any]:
        from . import docgen

        return docgen.fill_slots(
            str(params["draftId"]),
            [v for v in (params.get("values") or []) if isinstance(v, dict)],
        )

    @server.method("docgen.update_outline")
    def docgen_update_outline(params: dict[str, Any]) -> dict[str, Any]:
        from . import docgen

        return docgen.update_outline(
            str(params["draftId"]),
            [it for it in (params.get("items") or []) if isinstance(it, dict)],
        )

    @server.method("docgen.confirm_outline")
    def docgen_confirm_outline(params: dict[str, Any]) -> dict[str, Any]:
        from . import docgen

        return docgen.confirm_outline(str(params["draftId"]))

    @server.method("docgen.render")
    def docgen_render(params: dict[str, Any]) -> dict[str, Any]:
        from . import docgen

        body = params.get("bodyMd")
        return docgen.render_body(
            str(params["draftId"]),
            body_md=(str(body) if body else None),
        )

    @server.method("docgen.confirm")
    def docgen_confirm(params: dict[str, Any]) -> dict[str, Any]:
        from . import docgen

        return docgen.confirm_draft(str(params["draftId"]), params.get("contentReviewed") is True,
                                    params.get("expectedUpdatedAt"))

    @server.method("docgen.revise")
    def docgen_revise(params: dict[str, Any]) -> dict[str, Any]:
        """AI 修订落库（交付物迭代改写）：修订正文由 agent 修订分叉生成，
        或由调用方（测试/后续入口）显式提供；落 kind='ai_revise' 版本。
        baseVersionNo 给定时校验版本锚点，防止旧稿覆盖新稿（升级方案 §7.4）。"""
        from . import docgen
        from .cases import require_active_case

        draft = docgen.get_draft(str(params["draftId"]))
        require_active_case(draft["caseId"])  # 归档案件只读（REQ-CASE-006）
        base = params.get("baseVersionNo")
        return docgen.revise_body(
            str(params["draftId"]),
            str(params.get("bodyMd") or ""),
            instruction=str(params.get("instruction") or ""),
            base_version_no=int(base) if base is not None else None,
        )

    @server.method("docgen.list_versions")
    def docgen_list_versions(params: dict[str, Any]) -> list[dict[str, Any]]:
        from . import docgen

        return docgen.list_versions(str(params["draftId"]))

    @server.method("docgen.restore_version")
    def docgen_restore_version(params: dict[str, Any]) -> dict[str, Any]:
        from . import docgen

        return docgen.restore_version(str(params["draftId"]), int(params["versionNo"]))

    @server.method("docgen.save_body")
    def docgen_save_body(params: dict[str, Any]) -> dict[str, Any]:
        from . import docgen
        from .cases import require_active_case

        draft = docgen.get_draft(str(params["draftId"]))
        require_active_case(draft["caseId"])  # 归档案件只读（REQ-CASE-006）
        session_id = params.get("sessionId")
        expected = params.get("expectedVersionNo")
        return docgen.save_body(
            str(params["draftId"]),
            str(params.get("bodyMd") or ""),
            kind=str(params.get("kind") or "edit"),
            session_id=(str(session_id) if session_id else None),
            expected_version_no=int(expected) if expected is not None else None,
        )

    @server.method("docgen.diff_versions")
    def docgen_diff_versions(params: dict[str, Any]) -> dict[str, Any]:
        from . import docgen

        to_no = params.get("toVersionNo")
        return docgen.diff_versions(
            str(params["draftId"]),
            int(params["fromVersionNo"]),
            int(to_no) if to_no is not None else None,
        )

    @server.method("docgen.export")
    def docgen_export(params: dict[str, Any]) -> dict[str, Any]:
        from . import docgen

        return docgen.export_draft(
            str(params["draftId"]),
            dest_dir=str(params.get("destDir") or ""),
            fmt=str(params.get("format") or "md"),
        )

    @server.method("word.create_work_copy")
    def word_create_work_copy(params: dict[str, Any]) -> dict[str, Any]:
        from .word import create_work_copy

        return create_work_copy(str(params["caseId"]), params.get("documentId"), params.get("draftId"),
                                bool(params.get("simplify")))

    @server.method("word.get_model")
    def word_get_model(params: dict[str, Any]) -> dict[str, Any]:
        from .word import get_model

        return get_model(str(params["targetId"]))

    @server.method("word.save_model")
    def word_save_model(params: dict[str, Any]) -> dict[str, Any]:
        from .word import save_model

        return save_model(str(params["targetId"]), params["model"], int(params["expectedRevision"]),
                          params["clientRequestId"], str(params.get("kind") or "edit"), params.get("sessionId"))

    @server.method("word.open_editor")
    def word_open_editor(params: dict[str, Any]) -> dict[str, Any]:
        from .word import open_editor

        return open_editor(str(params["targetId"]))

    @server.method("word.list_versions")
    def word_list_versions(params: dict[str, Any]) -> list[dict[str, Any]]:
        from .word import list_versions

        return list_versions(str(params["targetId"]))

    @server.method("word.list_work_copies")
    def word_list_work_copies(params: dict[str, Any]) -> list[dict[str, Any]]:
        from .word import list_work_copies

        return list_work_copies(str(params["caseId"]))

    @server.method("word.export")
    def word_export(params: dict[str, Any]) -> dict[str, Any]:
        from .word import export

        return export(str(params["targetId"]), str(params["destPath"]))

    @server.method("word.restore_version")
    def word_restore_version(params: dict[str, Any]) -> dict[str, Any]:
        from .word import restore_version

        return restore_version(str(params["targetId"]), int(params["revision"]), int(params["expectedRevision"]))

    @server.method("word.diff_versions")
    def word_diff_versions(params: dict[str, Any]) -> dict[str, Any]:
        from .word import diff_versions

        return diff_versions(str(params["targetId"]), int(params["fromRevision"]), int(params["toRevision"]))

    @server.method("word.apply_patches")
    def word_apply_patches(params: dict[str, Any]) -> dict[str, Any]:
        from .word import apply_patches

        return apply_patches(str(params["targetId"]), params["patches"], int(params["expectedRevision"]),
                             params["clientRequestId"])

    @server.method("word.resolve_revisions")
    def word_resolve_revisions(params: dict[str, Any]) -> dict[str, Any]:
        """接受/拒绝修订（可指定 revisionIds；缺省全部），并形成新的工作副本修订。"""
        from .word import get_model, resolve_revisions, save_model

        target_id = str(params["targetId"])
        expected_revision = int(params["expectedRevision"])
        current = get_model(target_id)
        if current["revision"] != expected_revision:
            from .word import WordError

            raise WordError("VERSION_CONFLICT")
        resolved = resolve_revisions(current["model"], str(params.get("action") or ""),
                                     params.get("revisionIds"))
        return save_model(target_id, resolved, expected_revision, str(params["clientRequestId"]), "edit")

    # ---- 产物（M2 收尾：落库 / 概览 / 导出） ----------------------------------

    @server.method("artifact.list")
    def artifact_list(params: dict[str, Any]) -> list[dict[str, Any]]:
        from .artifacts import list_artifacts
        from .agent.runner import register_orphan_files

        # 自愈：历史任务收尾未登记的落盘文件（旧版本 Core / 中断遗留），
        # 打开概览时按当前案件补登记一次；失败不影响列表读取
        register_orphan_files(str(params["caseId"]))
        return list_artifacts(str(params["caseId"]))

    @server.method("artifact.get")
    def artifact_get(params: dict[str, Any]) -> dict[str, Any]:
        from .artifacts import get_artifact

        return get_artifact(str(params["artifactId"]))

    @server.method("artifact.open_content")
    def artifact_open_content(params: dict[str, Any]) -> dict[str, Any]:
        """文件类产物（kind=file）的原件内容会话：AI 生成的文档按原格式预览。"""
        from .content import open_artifact_content

        return open_artifact_content(str(params["artifactId"]))

    @server.method("artifact.open_editable")
    def artifact_open_editable(params: dict[str, Any]) -> dict[str, Any]:
        """直接编辑 AI 生成文档：docx → Word 模型，xlsx/pptx → 单元格/文本框模型。"""
        from .artifacts import open_editable

        return open_editable(str(params["artifactId"]))

    @server.method("artifact.save_editable")
    def artifact_save_editable(params: dict[str, Any]) -> dict[str, Any]:
        """编辑结果直接写回原文档文件（无工作副本）。"""
        from .artifacts import save_editable

        return save_editable(
            str(params["artifactId"]),
            params.get("model") if isinstance(params.get("model"), dict) else None,
            params.get("text") if isinstance(params.get("text"), str) else None,
            bool(params.get("createVersion")),
        )

    @server.method("artifact.save_sheet_edits")
    def artifact_save_sheet_edits(params: dict[str, Any]) -> dict[str, Any]:
        """Excel 单元格增量写回（只改指定单元格；图表丢失需显式确认）。"""
        from .artifacts import save_sheet_edits

        return save_sheet_edits(
            str(params["artifactId"]),
            params.get("changes") if isinstance(params.get("changes"), list) else [],
            bool(params.get("createVersion")),
            bool(params.get("confirmLoss")),
        )

    @server.method("artifact.save_slide_texts")
    def artifact_save_slide_texts(params: dict[str, Any]) -> dict[str, Any]:
        """PPTX 文本框轻量编辑（形状/版式/图表保持原样）。"""
        from .artifacts import save_slide_texts

        return save_slide_texts(
            str(params["artifactId"]),
            params.get("edits") if isinstance(params.get("edits"), list) else [],
            bool(params.get("createVersion")),
        )

    @server.method("artifact.export")
    def artifact_export(params: dict[str, Any]) -> dict[str, Any]:
        from .artifacts import export_artifact

        return export_artifact(
            artifact_id=str(params["artifactId"]),
            fmt=str(params.get("format") or ""),
            dest_path=str(params.get("destPath") or ""),
        )

    @server.method("artifact.dismiss")
    def artifact_dismiss(params: dict[str, Any]) -> dict[str, Any]:
        """用户移除产物（调试残留等非交付物）：概览/气泡卡不再显示，数据保留。"""
        from .artifacts import dismiss_artifact

        return dismiss_artifact(str(params["artifactId"]))

    # ---- 产物版本组（交付物迭代改写） ---------------------------------------

    @server.method("artifact.save_version")
    def artifact_save_version(params: dict[str, Any]) -> dict[str, Any]:
        from .artifacts import save_artifact_version

        body = params.get("body")
        return save_artifact_version(
            str(params["artifactId"]),
            body if isinstance(body, dict) else {},
            title=(str(params["title"]) if params.get("title") else None),
            task_id=(str(params["taskId"]) if params.get("taskId") else None),
        )

    @server.method("artifact.list_versions")
    def artifact_list_versions(params: dict[str, Any]) -> list[dict[str, Any]]:
        from .artifacts import list_artifact_versions

        return list_artifact_versions(str(params["artifactId"]))

    @server.method("artifact.restore_version")
    def artifact_restore_version(params: dict[str, Any]) -> dict[str, Any]:
        from .artifacts import restore_artifact_version

        return restore_artifact_version(
            str(params["artifactId"]), int(params["versionNo"])
        )

    # ---- 产物批注（AI 生成产物的派生数据；整篇单锚，绑定具体产物版本） --------

    @server.method("artifact.annotations")
    def artifact_annotations(params: dict[str, Any]) -> list[dict[str, Any]]:
        from .artifacts import list_artifact_annotations

        return list_artifact_annotations(str(params["artifactId"]))

    @server.method("artifact.add_annotation")
    def artifact_add_annotation(params: dict[str, Any]) -> dict[str, Any]:
        """新增产物批注：quote 为可选原文锚定片段；审计不落正文。"""
        from .artifacts import add_artifact_annotation

        return add_artifact_annotation(
            artifact_id=str(params["artifactId"]),
            body=str(params.get("body") or ""),
            quote=str(params.get("quote") or ""),
            color=str(params.get("color") or ""),
        )

    @server.method("artifact.update_annotation")
    def artifact_update_annotation(params: dict[str, Any]) -> dict[str, Any]:
        from .artifacts import update_artifact_annotation

        return update_artifact_annotation(
            annotation_id=str(params["annotationId"]),
            body=str(params.get("body") or ""),
            color=str(params.get("color") or ""),
        )

    @server.method("artifact.resolve_annotation")
    def artifact_resolve_annotation(params: dict[str, Any]) -> dict[str, Any]:
        from .artifacts import set_artifact_annotation_resolved

        return set_artifact_annotation_resolved(
            annotation_id=str(params["annotationId"]),
            resolved=bool(params.get("resolved")),
        )

    @server.method("artifact.delete_annotation")
    def artifact_delete_annotation(params: dict[str, Any]) -> dict[str, Any]:
        from .artifacts import delete_artifact_annotation

        return delete_artifact_annotation(str(params["annotationId"]))

    @server.method("document.correct_ocr")
    def document_correct_ocr(params: dict[str, Any]) -> dict[str, Any]:
        """OCR 人工更正（O-008 / Q-009）：原值与更正值并存，失效引用转待校验。"""
        from .documents import correct_page_text

        return correct_page_text(
            document_id=str(params["documentId"]),
            page_no=int(params["pageNo"]),
            corrected_text=str(params.get("correctedText") or ""),
            note=str(params.get("note") or ""),
        )

    @server.method("document.page_corrections")
    def document_page_corrections(params: dict[str, Any]) -> list[dict[str, Any]]:
        """某页的更正历史（OCR 原值 → 更正值，追加式）。"""
        from .documents import list_page_corrections

        return list_page_corrections(str(params["documentId"]), int(params["pageNo"]))

    # ---- 页批注（DEV-DOC-010）：派生数据，quote 锚定读取端重定位（Q-009/Q-010） --

    @server.method("document.annotations")
    def document_annotations(params: dict[str, Any]) -> list[dict[str, Any]]:
        """文档全部批注（quote_missing 读取时计算）。"""
        from .documents import list_annotations

        return list_annotations(str(params["documentId"]))

    @server.method("document.add_annotation")
    def document_add_annotation(params: dict[str, Any]) -> dict[str, Any]:
        """新增页批注：quote 为可选原文锚定片段；审计不落正文。"""
        from .documents import add_annotation

        return add_annotation(
            document_id=str(params["documentId"]),
            page_no=int(params["pageNo"]),
            body=str(params.get("body") or ""),
            quote=str(params.get("quote") or ""),
            color=str(params.get("color") or ""),
        )

    @server.method("document.update_annotation")
    def document_update_annotation(params: dict[str, Any]) -> dict[str, Any]:
        from .documents import update_annotation

        return update_annotation(
            annotation_id=str(params["annotationId"]),
            body=str(params.get("body") or ""),
            color=str(params.get("color") or ""),
        )

    @server.method("document.resolve_annotation")
    def document_resolve_annotation(params: dict[str, Any]) -> dict[str, Any]:
        from .documents import set_annotation_resolved

        return set_annotation_resolved(
            annotation_id=str(params["annotationId"]),
            resolved=bool(params.get("resolved")),
        )

    @server.method("document.delete_annotation")
    def document_delete_annotation(params: dict[str, Any]) -> dict[str, Any]:
        from .documents import delete_annotation

        return delete_annotation(str(params["annotationId"]))

    # ---- 单文档删除（R-007：删除文档 → 受影响 Claim 失效） ---------------------

    @server.method("document.delete")
    def document_delete(params: dict[str, Any]) -> dict[str, Any]:
        from .documents import delete_document

        return delete_document(str(params["documentId"]))

    # ---- 文档级脱敏（「脱敏处理」按钮）：预览计数 + 派生脱敏版生成 ----------

    @server.method("document.redact_preview")
    def document_redact_preview(params: dict[str, Any]) -> dict[str, Any]:
        """脱敏前预览：按类型返回命中计数与抽样片段（原文不回传 UI 明细行）。"""
        from .documents import get_pages
        from .redaction import mask_text

        doc_id = str(params["documentId"])
        pages = get_pages(doc_id)
        counts: dict[str, int] = {}
        total = 0
        for p in pages:
            res = mask_text(str(params["caseId"]), str(p.get("text") or ""))
            total += int(res["total"])
            for label, n in (res["counts"] or {}).items():
                counts[label] = counts.get(label, 0) + int(n)
        return {"documentId": doc_id, "total": total, "counts": counts, "pageCount": len(pages)}

    @server.method("document.redact_apply")
    def document_redact_apply(params: dict[str, Any]) -> dict[str, Any]:
        """解析文本原地脱敏（原件二进制不动；幂等，已脱敏直接返回）。"""
        from .documents import redact_document

        return redact_document(
            str(params["documentId"]),
            task_id=(str(params["taskId"]) if params.get("taskId") else None),
        )

    # ---- 审计（REQ-AUD-001 / DEV-FND-006） -----------------------------------

    @server.method("audit.verify")
    def audit_verify(_params: dict[str, Any]) -> dict[str, Any]:
        from .audit import verify

        return verify()

    @server.method("audit.list")
    def audit_list(params: dict[str, Any]) -> list[dict[str, Any]]:
        from .audit import list_logs

        return list_logs(
            case_id=(str(params["caseId"]) if params.get("caseId") else None),
            action=(str(params["action"]) if params.get("action") else None),
            limit=int(params.get("limit") or 100),
        )

    # ---- 脱敏（M3 / REQ-PRIV-001~008 / DEV-AGT-009 / D-001~D-011） ----------
    # apply/restore 供本机受信场景（产物导出/差异核对）调用；外发管道内的
    # 脱敏由 agent.graph._redact_for_remote 直接执行，不经 RPC。

    @server.method("redaction.policy")
    def redaction_policy(params: dict[str, Any]) -> dict[str, Any]:
        from .redaction import resolve_policy

        policy, types = resolve_policy(str(params["caseId"]))
        return {"policy": policy, "types": sorted(types)}

    @server.method("redaction.set_policy")
    def redaction_set_policy(params: dict[str, Any]) -> dict[str, Any]:
        from .cases import update_redaction_policy

        custom = params.get("customTypes")
        return update_redaction_policy(
            case_id=str(params["caseId"]),
            policy=str(params["policy"]),
            custom_types=([str(t) for t in custom] if custom else None),
        )

    @server.method("redaction.detect")
    def redaction_detect(params: dict[str, Any]) -> list[dict[str, Any]]:
        from .redaction import detect

        types = params.get("types")
        return detect(
            case_id=str(params["caseId"]),
            text=str(params.get("text") or ""),
            types=([str(t) for t in types] if types else None),
        )

    @server.method("redaction.preview")
    def redaction_preview(params: dict[str, Any]) -> dict[str, Any]:
        from .redaction import preview

        return preview(case_id=str(params["caseId"]), text=str(params.get("text") or ""))

    @server.method("redaction.apply")
    def redaction_apply(params: dict[str, Any]) -> dict[str, Any]:
        from .redaction import apply

        return apply(case_id=str(params["caseId"]), text=str(params.get("text") or ""))

    @server.method("redaction.restore")
    def redaction_restore(params: dict[str, Any]) -> dict[str, Any]:
        from .redaction import restore

        restored = restore(case_id=str(params["caseId"]), redacted_text=str(params.get("redactedText") or ""))
        return {"restoredText": restored}

    @server.method("redaction.list_entities")
    def redaction_list_entities(params: dict[str, Any]) -> list[dict[str, Any]]:
        from .redaction import list_entities

        return list_entities(
            case_id=str(params["caseId"]),
            status=(str(params["status"]) if params.get("status") else None),
        )

    @server.method("redaction.confirm_entities")
    def redaction_confirm_entities(params: dict[str, Any]) -> dict[str, Any]:
        from .redaction import confirm_entities

        return confirm_entities(
            case_id=str(params["caseId"]),
            entity_ids=[str(i) for i in params.get("entityIds") or []],
            action=str(params.get("action") or "confirm"),
        )

    @server.method("redaction.list_terms")
    def redaction_list_terms(params: dict[str, Any]) -> list[dict[str, Any]]:
        from .redaction import list_terms

        return list_terms(str(params["caseId"]))

    @server.method("redaction.add_term")
    def redaction_add_term(params: dict[str, Any]) -> dict[str, Any]:
        from .redaction import add_term

        return add_term(case_id=str(params["caseId"]), term=str(params.get("term") or ""))

    @server.method("redaction.remove_term")
    def redaction_remove_term(params: dict[str, Any]) -> dict[str, Any]:
        from .redaction import remove_term

        return remove_term(case_id=str(params["caseId"]), term_id=str(params["termId"]))

    # ---- 技能（REQ-AI-003 / REQ-AI-007）：安装走 Agent 工具 skill_install ----

    @server.method("skill.list")
    def skill_list(params: dict[str, Any]) -> list[dict[str, Any]]:
        from . import skills

        return skills.list_skills()

    @server.method("skill.remove")
    def skill_remove(params: dict[str, Any]) -> dict[str, Any]:
        from . import skills

        removed = skills.remove_skill(str(params["skillId"]))
        if not removed:
            raise ValueError("技能不存在或已删除")
        return {"removed": True}
