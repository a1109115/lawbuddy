"""AgentRunner：后台线程驱动自主 Agent Loop（DEV-AGT-005 断点续跑基础）。

- agent.start 立即返回，任务在后台线程执行，事件经 get_events 轮询；
- 所有模式（阅卷 / 合同审查 / 文书生成）统一走自主循环（autonomous.py）：
  模型自主决策与调用工具，唯一硬限制是最大轮数；
- 取消与恢复（§11）：CANCELLED/FAILED 任务可重新 start，从最近检查点
  恢复对话消息序列（已完成轮次不重复）；取消只阻止新调用，不删除
  已产生的消息与产物；
- 异常进 FAILED 终态 + error 事件（错误文案含影响与恢复方式，不暴露堆栈）。
"""

from __future__ import annotations

import queue
import re
import sys
import threading
import time
import traceback
from typing import Any

from . import budget
from . import tasks as task_store
from ..cases import get_case
from .autonomous import run_autonomous

# 并发上限缺省值（settings agent.maxConcurrent 可覆盖，1~8）
DEFAULT_MAX_CONCURRENT = 2

# 任务落盘文件登记为产物的扩展名白名单（脚本/临时文件不登记）
_FILE_ARTIFACT_EXTS = {".docx", ".pdf", ".xlsx", ".pptx", ".md", ".txt", ".csv", ".html"}
_FILE_ARTIFACT_PROTECTED = {"lawbuddy.db", "lawbuddy.db-wal", "lawbuddy.db-shm"}
_FILE_TEXT_PREVIEW_EXTS = {".md", ".txt", ".csv"}


def _file_attributed_after(name: str, mtime: float) -> bool:
    """该文件名的当前内容是否已被某个任务登记为产物（跨全部案件）。

    工作空间根目录为全案件共享、同名文件即同一份文件：文件落盘后只要已有
    产物登记（任何案件/任务，created_at ≥ 落盘时刻），就说明这次写入已归属，
    收尾扫描/孤儿补登记都不得再重复登记到别的任务上。
    """
    from datetime import datetime, timezone

    from ..storage import get_db

    try:
        threshold = datetime.fromtimestamp(mtime - 2, timezone.utc).isoformat(
            timespec="microseconds"
        )
        return (
            get_db().query_one(
                "SELECT 1 FROM artifacts WHERE kind = 'file' AND title = ? AND created_at >= ?"
                " LIMIT 1",
                (name, threshold),
            )
            is not None
        )
    except Exception:
        return False


def _task_windows(case_id: str) -> list[tuple[str, float, float]]:
    """本案各任务的时间窗（task_id, 起始秒, 结束秒），用于把工作空间文件归到具体任务。

    孤儿补登记时没有 task_id 可用，只能按「文件落盘时刻落在哪个任务的执行窗口」
    归属；不在本案任何任务窗口内的文件属于别的任务（如文书生成任务），不补登记。
    """
    from datetime import datetime

    from ..storage import get_db

    try:
        rows = get_db().query(
            "SELECT id, created_at, updated_at, finished_at FROM tasks WHERE case_id = ?",
            (case_id,),
        )
    except Exception:
        return []
    out: list[tuple[str, float, float]] = []
    for r in rows:
        try:
            start = datetime.fromisoformat(str(r["created_at"])).timestamp()
            end_s = r["finished_at"] or r["updated_at"] or r["created_at"]
            end = datetime.fromisoformat(str(end_s)).timestamp()
        except (TypeError, ValueError):
            continue
        out.append((str(r["id"]), start - 2, end + 2))
    return out


def _register_workspace_files(
    task_id: str | None,
    case_id: str,
    started_ts: float,
    *,
    skip_titles: frozenset[str] = frozenset(),
    intro_prefix: str = "本次任务落盘的文件",
    task_windows: list[tuple[str, float, float]] | None = None,
) -> int:
    """把工作空间根目录新落盘的文档登记为产物。

    模型经沙箱 write_file / bash 生成的文件（如 Word 报告）此前不进 artifacts
    表——产物面板「暂无 AI 产物」而文件实际已在磁盘，用户无从发现。登记规则：
    - 仅根目录一层、文档扩展名白名单、mtime 晚于 started_ts（±2s 容差）；
    - 文本类（md/txt/csv）读前 2000 字作预览；二进制类只登记名称与位置说明；
    - body 只含文件名与工作空间目录名，不含绝对路径（防路径泄漏入产物/审计）；
    - skip_titles：已登记文件名集合（孤儿补登记查重防重复）；
    - task_windows 给定（孤儿补登记）时，只有落盘时刻在本案任务窗口内的文件才
      登记，并归属到该任务；不在窗口内的文件属于别的任务，不作近似归属。
    """
    from .. import artifacts as artifact_store
    from ..sandbox import workspace_root

    try:
        root = workspace_root()
        entries = [p for p in root.iterdir() if p.is_file()]
    except Exception:
        return 0
    # 用户已移除的文件产物不再登记（dismiss 后防止收尾登记把它找回）
    try:
        skip_titles = skip_titles | frozenset(artifact_store.dismissed_file_titles(case_id))
    except Exception:
        pass
    registered = 0
    for p in entries:
        try:
            name = p.name
            if name in _FILE_ARTIFACT_PROTECTED or name.startswith("."):
                continue
            if artifact_store.is_temp_file_title(name):
                continue  # 调试/临时残留（resp.txt 等）不是交付物，不登记
            if name in skip_titles:
                continue
            if p.suffix.lower() not in _FILE_ARTIFACT_EXTS:
                continue
            mtime = p.stat().st_mtime
            if mtime < started_ts - 2:
                continue
            owner_task_id = task_id
            if task_windows is not None:
                matched = next(((tid, s, e) for tid, s, e in task_windows if s <= mtime <= e), None)
                if matched is None:
                    continue  # 落盘时刻不在本案任何任务窗口内：属于其他任务的产物
                owner_task_id = matched[0]
            # 该次写入已被某个任务登记（工具内主动登记或收尾登记，任何案件）时不重复
            # 登记，保留原有归属与元数据（前端据此路由到文书工作台/会话文件卡）
            if _file_attributed_after(name, mtime):
                continue
            body: dict[str, Any] = {
                "intro": f"{intro_prefix}，位于工作空间根目录（{root.name}）：{name}",
                "fileName": name,
                "workspaceDirName": root.name,
            }
            if p.suffix.lower() in _FILE_TEXT_PREVIEW_EXTS:
                try:
                    body["intro"] = p.read_text(encoding="utf-8", errors="replace")[:2000]
                except OSError:
                    pass
            # 文件大小：气泡底部文件卡与产物概览展示（元数据级，无路径泄漏）
            try:
                body["sizeBytes"] = p.stat().st_size
            except OSError:
                pass
            artifact_store.save_artifact(owner_task_id, case_id, "file", name, body)
            registered += 1
        except OSError:
            continue
    return registered


def register_orphan_files(case_id: str) -> int:
    """补登记孤儿产物文件（自愈）：已落盘但未进 artifacts 表的文档。

    场景：任务收尾登记未生效（旧版本 Core / 进程中断 / CHECK 约束失败）时，
    文件在盘上而产物概览为空。artifact.list 打开概览时按当前案件补登记一次。
    工作空间根目录全案件共享，补登记只能按「落盘时刻落在本案哪个任务的执行
    窗口」归属并写入该 task_id；不在本案任务窗口内、或已被其他任务登记过的
    文件一律不补登记，避免把文书生成等别的任务的产物算到阅卷任务上。
    """
    import sys

    from .. import artifacts as artifact_store
    from ..cases import get_case

    try:
        get_case(case_id)
        windows = _task_windows(case_id)
    except Exception:
        return 0
    if not windows:
        return 0
    try:
        skip = frozenset(
            a["title"] for a in artifact_store.list_artifacts(case_id) if a.get("kind") == "file"
        )
        # 含用户已移除的文件：孤儿补登记不得把 dismiss 过的文件找回
        skip = frozenset(skip) | frozenset(artifact_store.dismissed_file_titles(case_id))
    except Exception:
        skip = frozenset()
    try:
        return _register_workspace_files(
            None, case_id, 0.0,
            skip_titles=skip,
            intro_prefix="工作空间根目录的文件",
            task_windows=windows,
        )
    except Exception:
        # 自愈失败不阻断产物列表读取，堆栈进 stderr 供诊断
        traceback.print_exc(file=sys.stderr)
        return 0


# 回复中"声称已生成/保存"的交付文件名提取（收尾守门用）。
# 动作词限定语境，避免把"之前生成的《x.docx》可参考"这类引用误判为本轮产物；
# 扩展名白名单与 _FILE_ARTIFACT_EXTS 对齐（脚本/临时文件不核验）。
_CLAIMED_FILE_RE = re.compile(
    r"(?:已生成|生成了|已保存为?|保存为|另存为|已另存为?|已导出|导出为|已输出|已写入|已更新|已覆盖)"
    r"[^《》「」\"'\n]{0,16}?"
    r"[《》「」\"']?"
    r"([^《》「」\"'\n，。；、,!:：？?]{1,100}?\.(?:docx|doc|xlsx|xls|pptx|pdf|md|csv|html|txt)\b)",
    re.IGNORECASE,
)
_CLAIM_SCAN_SKIP_DIRS = {".lawbuddy", ".lawbuddy-temp", "blobs", "skills"}

# 动作词可能被正则一并抓进文件名（"保存为 report.docx" → "保存为 report.docx"），
# 这里剥掉前缀动词，否则真实已落盘的文件会被误报为未生成（§11.1 产物守门）。
_CLAIMED_NAME_PREFIX_RE = re.compile(
    r"^(?:已生成|生成了|已生成文件|已保存为?|保存为|另存为|已另存为?|已导出|导出为|"
    r"已输出|已写入|已更新|已覆盖|文件|副本为?)\s*"
)


def _clean_claimed_name(raw: str) -> str:
    name = str(raw or "").strip().strip("《》「」\"'")
    prev = None
    while prev != name:
        prev = name
        name = _CLAIMED_NAME_PREFIX_RE.sub("", name).strip().strip("《》「」\"'")
    return name


def verify_claimed_files(answer: str) -> list[str]:
    """核验回复中声称已生成/保存的交付文件是否真实落盘。

    确定性守门（只核验不拦截）：保存失败（如原文件被 Word 锁定）后模型可能
    虚构"另存为副本成功"，气泡无文件卡而文本自称已生成，用户被误导。这里把
    回复中的声称文件名与工作空间实际文件比对，返回未找到的文件名列表，由
    调用方决定标注方式。只查存在性，不做 mtime 归属（历史文件引用不算虚报）。
    """
    text = str(answer or "")
    claimed: list[str] = []
    for m in _CLAIMED_FILE_RE.finditer(text):
        name = _clean_claimed_name(m.group(1))
        if name and name not in claimed:
            claimed.append(name)
    if not claimed:
        return []
    from ..sandbox import workspace_root

    try:
        root = workspace_root()
    except Exception:
        return []
    missing: list[str] = []
    for name in claimed:
        found = False
        try:
            for p in root.rglob(name):
                if set(p.parts) & _CLAIM_SCAN_SKIP_DIRS:
                    continue
                if p.is_file():
                    found = True
                    break
        except OSError:
            continue
        if not found:
            missing.append(name)
    return missing


class AgentError(RuntimeError):
    pass


class AgentRunner:
    _instance: "AgentRunner | None" = None

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancel_flags: dict[str, threading.Event] = {}
        # worker 池（DEV-AGT-005）：任务入队由固定数量 worker 消费，
        # 避免每任务一线程导致大卷宗任务无上限并发挤占模型/DB 资源
        self._queue: queue.Queue = queue.Queue()
        self._pending: set[str] = set()  # 已入队未完成的任务（去重）
        self._workers: list[threading.Thread] = []
        self._started = False
        # 随问附件（输入坞临时性补充材料）：内存随任务传递，不落库不进检查点；
        # worker 消费后即清除（Core 重启 / 断点续跑不保留，属临时材料语义）
        self._attachments: dict[str, list[dict[str, Any]]] = {}

    @classmethod
    def instance(cls) -> "AgentRunner":
        if cls._instance is None:
            cls._instance = AgentRunner()
        return cls._instance

    def _ensure_workers(self) -> None:
        """惰性启动 worker 线程（数量读 settings agent.maxConcurrent，1~8）。"""
        with self._lock:
            if self._started:
                return
            try:
                count = int(task_store.get_setting("agent.maxConcurrent"))
            except (TypeError, ValueError):
                count = 0
            count = max(1, min(8, count or DEFAULT_MAX_CONCURRENT))
            for i in range(count):
                t = threading.Thread(target=self._worker_loop, daemon=True, name=f"agent-worker-{i}")
                t.start()
                self._workers.append(t)
            self._started = True

    def _worker_loop(self) -> None:
        while True:
            task_id = self._queue.get()
            if task_id is None:  # shutdown 哨兵
                self._queue.task_done()
                return
            try:
                self._run(task_id)
            finally:
                with self._lock:
                    self._pending.discard(task_id)
                self._attachments.pop(task_id, None)  # 随问附件一次性消费（防内存滞留）
                self._queue.task_done()

    def start(self, task_id: str, attachments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        task = task_store.get_task(task_id)
        # §11：CANCELLED/FAILED 可从最近检查点续跑（COMPLETED 不再执行）
        if task["lifecycle"] in ("COMPLETED", "COMPLETED_WITH_WARNINGS"):
            return task
        from ..cases import require_case_mode

        require_case_mode(task["caseId"], task["mode"])
        # 同一案件只允许一个任务继续推进。重新发起时自动请求取消旧任务，
        # 防止旧任务与新任务同时消耗模型并同时写入同一会话。
        for old in task_store.list_active_tasks(task["caseId"], exclude_task_id=task_id):
            old_kind = (old.get("clientPosition") or {}).get("workflowKind")
            new_kind = (task.get("clientPosition") or {}).get("workflowKind")
            independent = {"contract_risk", "contract_dialogue"}
            if old_kind in independent and new_kind in independent and old_kind != new_kind:
                continue
            self.cancel(old["id"])
        self._ensure_workers()
        with self._lock:
            if task_id in self._pending:  # 已排队/执行中：幂等返回
                return task_store.get_task(task_id)
            self._pending.add(task_id)
            self._cancel_flags[task_id] = threading.Event()
            if attachments:
                self._attachments[task_id] = attachments
        if task["lifecycle"] in ("CANCELLED", "FAILED", "WAITING_HUMAN"):
            task_store.update_lifecycle(task_id, "RUNNING", "resume")
        self._queue.put(task_id)
        return task_store.get_task(task_id)

    def cancel(self, task_id: str) -> dict[str, Any]:
        task = task_store.get_task(task_id)
        flag = self._cancel_flags.get(task_id)
        if task["lifecycle"] in ("CREATED", "PLANNING", "RUNNING", "VERIFYING", "WAITING_HUMAN", "WAITING_APPROVAL", "PAUSED"):
            if flag:
                flag.set()
            # 尚未进入 worker 的排队任务可立即终止；执行中的任务保留
            # CANCELLING，模型流退出后由 _run 统一写入 CANCELLED。
            if task_id not in self._pending or task["lifecycle"] in ("CREATED", "PLANNING"):
                task_store.update_lifecycle(task_id, "CANCELLED")
            else:
                task_store.update_lifecycle(task_id, "CANCELLING")
        return task_store.get_task(task_id)

    def _run(self, task_id: str) -> None:
        cancel = self._cancel_flags[task_id]
        started_ts = time.time()
        try:
            task = task_store.get_task(task_id)
            if task["lifecycle"] in ("CANCELLED", "COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED"):
                return
            from ..cases import require_case_mode

            case = require_case_mode(task["caseId"], task["mode"])
            resumed = task.get("stage") == "resume" or task["lifecycle"] in ("CANCELLED", "FAILED", "CANCELLING", "WAITING_HUMAN")

            state: dict[str, Any] = {
                "task_id": task_id,
                "case_id": task["caseId"],
                "mode": task["mode"],
                "workflow_preset": task.get("workflowPreset"),
                "request": task["request"],
                "case_title": case["title"],
                "case_type": case.get("caseType"),
                "case_stage": case.get("stage"),
                "client_role": case.get("clientRole"),
                "model_policy": task.get("modelPolicy") or {},
                "tool_policy": task.get("toolPolicy") or {},
                "skills": task.get("skills") or [],
                "history": task.get("history") or [],
                "cancel_event": cancel,
                "selected_document_ids": task.get("selectedDocumentIds") or [],
                # 会话持久化锚点（V26）：同会话任务共享全文历史
                "conversation_id": task.get("conversationId"),
            }
            # 随问附件（临时性补充材料）：内存取出随任务进入 state，供自主循环
            # 在模型请求层并入图片/文本内容分片（不进 messages/检查点/会话归档）
            atts = self._attachments.pop(task_id, None)
            if atts:
                state["attachments"] = atts
            # 有会话时用 Core 全文历史覆盖前端截断 history（单条 4000 字，含引证）
            if state["conversation_id"]:
                try:
                    from .. import conversation as conversation_store

                    full = conversation_store.recent_history(str(state["conversation_id"]))
                    if full:
                        state["history"] = full
                except Exception:
                    pass  # 会话读取失败回退任务内置 history，不阻断执行
            # 模式专属输入（PRD 14.2 / 15.2）：经 clientPosition 通道进入任务
            if task["mode"] == "contract_review":
                state["contract_input"] = dict(task.get("clientPosition") or {})
            if task["mode"] == "document_generation":
                state["docgen_input"] = dict(task.get("clientPosition") or {})
            if task["mode"] == "legal_review":
                # 策略立场（REQ-STR-001 放宽）：本次任务显式传入的立场覆盖案件设置，
                # 不回写案件；缺省沿用案件委托人身份，仍缺失由模型标注「立场未明确」
                stance = str((task.get("clientPosition") or {}).get("stance") or "").strip()
                if stance:
                    state["client_role"] = stance

            # §11.1：从最近检查点恢复对话消息序列（已完成轮次不重复）
            if resumed:
                ckpt = task_store.latest_checkpoint(task_id)
                if ckpt and isinstance(ckpt.get("state"), dict):
                    messages = ckpt["state"].get("messages")
                    if isinstance(messages, list) and messages:
                        state["messages"] = messages
                        # 任务契约与阅读账本随消息一起恢复（05 方案 §5.3 §5.4）：
                        # 续跑不重新推导深度，也不把已读页面当成未读
                        contract = ckpt["state"].get("taskContract")
                        if isinstance(contract, dict):
                            state["task_contract"] = contract
                        ledger = ckpt["state"].get("readingLedger")
                        if isinstance(ledger, dict):
                            state["reading_ledger"] = ledger
                        # 任务计划同属任务状态（§6.4 C3）：续跑后模型仍能看到已完成的步骤，
                        # 不必从旧 system 文本里反推
                        plan = ckpt["state"].get("taskPlan")
                        if isinstance(plan, list) and plan:
                            state["task_plan"] = plan
                        # 证据/争点账本随检查点恢复（05 方案 §6.4 C3）：恢复后已有来源
                        # 不会变成"未调查"，也不会被压缩摘要当成新事实。Schema 版本不兼容时
                        # 丢弃旧账本重新建立，避免混用两套字段语义。
                        from .autonomous import LEDGER_SCHEMA_VERSION

                        ledger_version = ckpt["state"].get("ledgerSchemaVersion")
                        compatible = ledger_version in (None, LEDGER_SCHEMA_VERSION)
                        evidence = ckpt["state"].get("evidenceLedger")
                        issues = ckpt["state"].get("issueLedger")
                        restored_evidence = restored_issues = 0
                        if compatible:
                            if isinstance(evidence, dict):
                                state["evidence_ledger"] = evidence
                                restored_evidence = len(evidence)
                            if isinstance(issues, dict):
                                state["issue_ledger"] = issues
                                restored_issues = len(issues)
                            repair_left = ckpt["state"].get("repairRoundsLeft")
                            if isinstance(repair_left, int):
                                state["_repair_rounds_left"] = repair_left
                            explore_used = (ckpt["state"].get("budget") or {}).get("exploreRoundsUsed")
                            if isinstance(explore_used, int):
                                state["_explore_rounds_used"] = explore_used
                        task_store.append_event(task_id, "resume", {
                            "fromNode": ckpt.get("node"),
                            "restoredMessages": len(messages),
                            "restoredReadingLedger": len(ledger) if isinstance(ledger, dict) else 0,
                            "restoredEvidenceLedger": restored_evidence,
                            "restoredIssueLedger": restored_issues,
                            "ledgerSchemaStale": not compatible,
                            "createdAt": ckpt.get("createdAt"),
                        })
                task_store.update_lifecycle(task_id, "RUNNING", "resume")
            else:
                task_store.update_lifecycle(task_id, "RUNNING", "agent_loop")

            if cancel.is_set():
                task_store.update_lifecycle(task_id, "CANCELLED")
                return

            def emit(event_type: str, payload: dict[str, Any]) -> None:
                task_store.append_event(task_id, event_type, payload)

            if (state.get("contract_input") or {}).get("workflowKind") == "contract_checklist":
                from ..contract_workflow import run_checklist

                result = run_checklist(state, emit, cancel.is_set)
            elif (state.get("contract_input") or {}).get("workflowKind") == "contract_risk":
                from ..contract_workflow import run_risk_review

                result = run_risk_review(state, emit, cancel.is_set)
            else:
                result = run_autonomous(state, emit, cancel_check=cancel.is_set)
            # 完成状态由收尾守门决定（05 方案 §7.3）：有可交付结果但仍有明确列出的
            # 待核实/未读/产物警告时记为带警告完成，不伪装成干净完成
            finalize = (result or {}).get("finalize") or {}
            # 必要用户决策：任务已置 WAITING_HUMAN 并下发问题卡，不判定完成；
            # 用户回答后经 human_questions.answer 从同一检查点续跑
            waiting_human = bool(state.get("_paused_question_set")) or (
                str((result or {}).get("reason") or "") == "waiting_human"
            )
            if waiting_human:
                task_store.update_lifecycle(task_id, "WAITING_HUMAN", "human_question")
            elif finalize and finalize.get("ok") is False:
                task_store.update_lifecycle(task_id, "COMPLETED_WITH_WARNINGS")
            else:
                task_store.update_lifecycle(task_id, "COMPLETED")
            # 落盘文件登记为产物（V26）：模型经沙箱生成的 Word/MD 等文档
            # 自动进 artifacts 表，产物面板可见可打开；失败可见（stderr）不阻断
            try:
                _register_workspace_files(task_id, str(task.get("caseId") or ""), started_ts)
            except Exception:
                traceback.print_exc(file=sys.stderr)
            # 会话归档（V26）：正常完成的任务把本轮问答全文落库（含 max_rounds
            # 强制终答的结论）；失败不阻断任务收尾
            if state.get("conversation_id"):
                try:
                    from .. import conversation as conversation_store

                    answer = str((result or {}).get("answer") or "")
                    if answer.strip():
                        conversation_store.append_pair(
                            str(state["conversation_id"]), task_id,
                            str(task.get("request") or ""), answer,
                        )
                except Exception:
                    pass
        except task_store.TaskCancelled:
            # F-012：已接收内容保留（text / assistant_message 事件已在流内落库）；
            # §11.2：检查点已保存，可重新 start 从断点续跑
            task_store.update_lifecycle(task_id, "CANCELLED")
            # 取消前已落盘的文件同样登记为产物（已产生的内容用户应可见）
            try:
                _register_workspace_files(task_id, str(task.get("caseId") or ""), started_ts)
            except Exception:
                traceback.print_exc(file=sys.stderr)
        except Exception as exc:
            # 堆栈只进 stderr 日志（协议消息只走 stdout，UI 不暴露堆栈）
            traceback.print_exc(file=sys.stderr)
            from . import model_gateway

            # 云端模型不可用（网关失败/中断/密钥无效）：模型统一走云端，
            # 失败不降级本地——置 FAILED，按异常自带的 recover 指引恢复
            if isinstance(exc, task_store.ModelCallUnavailable):
                message, recover = str(exc), exc.recover
            elif isinstance(exc, model_gateway.ModelAuthError):
                message = str(exc)
                recover = (
                    "在底部输入坞打开「模型 API Key」设置，更新或清除无效密钥后重新发起任务"
                    "（将从断点继续，已完成的轮次不会重复）"
                )
            elif isinstance(exc, model_gateway.ModelGatewayError):
                # 平台返回的可读错误（如 429 余额不足）：直接展示，便于用户处理账户或稍后重试
                message = str(exc)
                recover = "请检查模型服务可用性与账户余额后重试；已完成的步骤会保留，不会重复执行"
            elif isinstance(exc, budget.BudgetExceeded):
                # 预算耗尽（如长合同逐项审查超墙钟/调用量上限）：展示具体超限项，
                # 并明确检查点续跑语义——已完成项不会重复执行。
                message = str(exc)
                recover = "本次作业因预算上限停止；已完成部分保留在检查点，可在放宽预算后从断点恢复续跑"
            else:
                # 合同工作流抛的是面向用户的可读错误；直接用原文，避免把
                # 可操作的失败原因压成一句无信息的 “WorkflowError”。
                from ..contract_workflow import WorkflowError as ContractWorkflowError

                if isinstance(exc, ContractWorkflowError):
                    detail = {
                        "SNAPSHOT_STALE": "审查设置或清单版本已变化，请重新保存概览后再生成",
                        "VERSION_CONFLICT": "审查设置已被更新，请刷新后重试",
                    }.get(str(exc), str(exc))
                    message = detail
                    recover = "请核对审查概览与原文版本后重新发起；已完成内容会保留"
                else:
                    message, recover = f"任务执行失败：{type(exc).__name__}", "请稍后重试；若持续失败请检查本地服务状态"
            task_store.append_event(
                task_id, "error",
                {
                    "message": message,
                    "impact": "已完成的对话轮次保留在检查点中，可重新发起任务续跑",
                    "recover": recover,
                },
            )
            task_store.update_lifecycle(task_id, "FAILED")

    def shutdown(self, timeout: float = 10.0) -> None:
        """通知 worker 退出并等待在跑任务收尾（排队中的任务不再执行）。"""
        self.suspend(timeout=timeout)

    def suspend(self, timeout: float = 10.0) -> bool:
        """停止全部 worker（切换工作空间 / 退出用），返回是否全部退出。

        与 shutdown 的区别：worker 全部退出后把状态复位为「未启动」，
        下次 start 会按新工作空间重新拉起（切换后仍可用）。
        """
        with self._lock:
            workers = list(self._workers)
            # 先置取消位：执行中的任务看到取消后尽快收尾，避免切换被迫中断
            for flag in self._cancel_flags.values():
                flag.set()
            self._workers = []
            self._started = False
            self._pending.clear()
            self._attachments.clear()
            for _ in workers:
                self._queue.put(None)
        alive = False
        for t in workers:
            t.join(timeout=max(0.1, timeout / max(1, len(workers))))
            alive = alive or t.is_alive()
        with self._lock:
            self._cancel_flags.clear()
        # 清空残留队列（哨兵之外的待执行任务已不可执行：连接即将关闭）
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
            else:
                self._queue.task_done()
        return not alive
