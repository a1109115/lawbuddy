"""全量档驱动（P2-12 §4.9）：导入协议原文 → 创建带交付契约的任务 → 收集 submission/。

全量档需要：
- 数据快照目录（含 inputs/agreements/A01..A16.txt、inputs/questionnaire.json、
  inputs/merge_questions.json、reference/ 官方评分器与真值）；
- 可用的模型 API Key（走应用内统一密钥管理，不读环境变量明文）。

本模块只负责编排，不做任何评分判断——评分一律交给 `scorer`（官方 verify.py 优先）。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT_S = 4 * 3600
POLL_INTERVAL_S = 5.0


def _task_sources(agreements_dir: Path, kind: str) -> list[str]:
    """任务材料清单（只取证据类文件，不导入问卷/模板/真值）。"""
    if kind == "contract":
        return sorted(str(p) for p in agreements_dir.glob("A*.[tT][xX][tT]"))
    sources = [str(agreements_dir / "facts.md")] if (agreements_dir / "facts.md").is_file() else []
    law_dir = agreements_dir / "law"
    if law_dir.is_dir():
        sources += [str(p) for p in sorted(law_dir.glob("*.md"))]
    return sources


def resolve_reference(base: Path, task_id: str | None) -> Path | None:
    """按任务解析官方 reference（每题真值不同，必须逐任务解析）。

    支持布局：
    - `<base>/tasks/<id>/reference/`（按任务分开的快照，推荐）；
    - `<base>/reference/<id>/`；
    - `<base>/reference/`（单任务快照，兼容既有用法）。
    只接受含 verify.py 且带该类任务真值的目录。
    """
    candidates: list[Path] = []
    if task_id:
        candidates += [
            base / "tasks" / task_id / "reference",
            base / "reference" / task_id,
        ]
    candidates.append(base / "reference")
    for cand in candidates:
        if not (cand / "verify.py").is_file():
            continue
        if (cand / "task_meta.json").is_file() or (cand / "gold.json").is_file():
            return cand
    return None


def _find(base: Path, name: str) -> Path:
    """在任务目录或 `inputs/` 下定位文件（两种快照布局都支持）。"""
    direct = base / name
    if direct.exists():
        return direct
    return base / "inputs" / name


def _questionnaire_multi(base: Path) -> list[str]:
    """读出多选 deal point 名（供交付契约提示"多选用 ` | ` 连接"）。

    `questionnaire.json` 有两种真实形态，都要支持：
    - 数组：`[{"id": "mae_co_economic", "multi_select": true, ...}, ...]`
    - 对象：`{"deal_points": {"mae_co_economic": {"multi": true}, ...}}`
    """
    path = _find(base, "questionnaire.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, list):
        return [
            str(item.get("id") or "").strip()
            for item in data
            if isinstance(item, dict) and item.get("multi_select") and str(item.get("id") or "").strip()
        ]
    if isinstance(data, dict):
        deal_points = data.get("deal_points") or data.get("dealPoints") or {}
        if isinstance(deal_points, dict):
            return [
                str(dp).strip()
                for dp, meta in deal_points.items()
                if isinstance(meta, dict) and meta.get("multi") and str(dp).strip()
            ]
    return []


def _build_contract(base: Path) -> list[dict[str, Any]]:
    """交付契约：deal_points.csv（按 agreement+deal_point upsert）+ answers.json。

    B-01 起同时声明工作全集：协议全集 × 问卷全集 = 必须进入终态的答案格，
    交付控制器据此补齐行全集并校验覆盖，而不是只判断"文件存在且非空"。
    """
    multi = _questionnaire_multi(base)
    agreements = _agreement_ids(base)
    questions = _questionnaire_ids(base)
    # 合法值（问卷 options）是交付契约的一部分：官方按"逐字等于 standard option"
    # 判分，不声明就只能由模型自由发挥。实测模型会把 "Natural disaster" 写成
    # "natural disasters"、"No" 写成 "None of the above"，整列判 0。
    valid_values = _questionnaire_options(base)
    spec: dict[str, Any] = {
        "name": "deal_points.csv",
        "format": "csv",
        # 前 3 列是官方评分口径；后 3 列是本产品的来源列（方案 §2.1 第 2 条：
        # 事实性答案必须保留文档 ID、版本 ID、页码和原文短引）。官方评分器按列名
        # 读取，多出的列被忽略；没有来源列时账本只能整表判 needs_human，
        # 每提交格 Token / 已提交格这类指标就永远没有分母。
        "columns": ["agreement", "deal_point", "answer",
                    "documentId", "pageNo", "quote"],
        "key": ["agreement", "deal_point"],
        "title": "deal points 表",
        "multiPoints": multi,
        "validValues": valid_values,
        # 合法值按"行内 deal_point 的取值"选择，答案列是 answer：
        # 写入时据此确定性校验，避免把改写过的选项当作答案落盘。
        "optionKey": "deal_point",
        "valueColumn": "answer",
        "description": (
            "每份协议 × 每个 deal point 一行；多选用 ` | ` 连接。"
            "answer 必须逐字使用该题的 standard option；同时填写来源："
            "documentId（协议文件名，如 A01）、pageNo（页码，整数）、"
            "quote（支撑该答案的原文短句）。来源缺失的格子会进入待人工核实。"
        ),
    }
    if agreements and questions:
        spec["expectedEntities"] = agreements
        spec["questions"] = questions
    return [
        spec,
        {
            "name": "answers.json",
            "format": "json",
            "skeleton": {},
            "title": "聚合题答案",
            # 声明聚合题全集：不声明时模型会按自己的理解命名键（实测写成
            # deal point id），官方按 M01…M17 取值，merge 直接判 0。
            "questions": _merge_questions(base),
            "description": "聚合题 id → 答案（整数/协议列表/分选项计数/分组列表）",
        },
    ]


def _questionnaire_options(base: Path) -> dict[str, list[str]]:
    """问卷合法值（deal point id → 标准选项全集，来自任务输入而非真值）。"""
    path = _find(base, "questionnaire.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, list[str]] = {}
    items = data if isinstance(data, list) else []
    if isinstance(data, dict):
        dps = data.get("deal_points") or data.get("dealPoints") or {}
        if isinstance(dps, dict):
            items = [{"id": dp, **(meta if isinstance(meta, dict) else {})}
                     for dp, meta in dps.items()]
    for item in items:
        if not isinstance(item, dict):
            continue
        qid = str(item.get("id") or "").strip()
        options = [str(o) for o in (item.get("options") or []) if str(o or "").strip()]
        if qid and options:
            out[qid] = options
    return out


def _merge_questions(base: Path) -> list[dict[str, str]]:
    """聚合题全集（id + 题干，来自 `merge_questions.json`，不是官方真值）。"""
    path = _find(base, "merge_questions.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = data.get("questions") if isinstance(data, dict) else data
    out: list[dict[str, str]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        qid = str(item.get("id") or "").strip()
        if not qid:
            continue
        text = str(item.get("question") or item.get("text") or qid).strip()
        answer_type = str(item.get("answer_type") or "").strip()
        out.append({"id": qid, "text": f"{text}（{answer_type}）" if answer_type else text})
    return out


def _agreement_ids(base: Path) -> list[dict[str, Any]]:
    """协议全集（来自 agreements 目录的文件名，不是真值）。"""
    directory = _find(base, "agreements")
    if not directory.is_dir():
        return []
    return [{"id": p.stem, "label": p.stem, "fields": []}
            for p in sorted(directory.glob("A*.txt")) if p.stem]


def _questionnaire_ids(base: Path) -> list[dict[str, Any]]:
    """问卷条目全集（只读 id 与题干，不读任何答案）。"""
    path = _find(base, "questionnaire.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out: list[dict[str, Any]] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and str(item.get("id") or "").strip():
                qid = str(item["id"]).strip()
                out.append({"id": qid, "text": str(item.get("question") or item.get("text") or qid)})
    elif isinstance(data, dict):
        deal_points = data.get("deal_points") or data.get("dealPoints") or {}
        if isinstance(deal_points, dict):
            for dp, meta in deal_points.items():
                text = ""
                if isinstance(meta, dict):
                    text = str(meta.get("question") or meta.get("text") or "")
                out.append({"id": str(dp).strip(), "text": text or str(dp).strip()})
    return out


def _request_text(base: Path) -> str:
    instruction = _find(base, "instruction.md")
    if instruction.is_file():
        return instruction.read_text(encoding="utf-8", errors="replace")
    return (
        "请阅读 agreements 下的全部协议，按问卷逐份填写 deal point，"
        "并把结果写到 submission/deal_points.csv（列 agreement,deal_point,answer），"
        "然后回答 aggregation 问题并写入 submission/answers.json。"
    )


def _resolve_task_inputs(
    snapshot_root: Path, task_filter: str | None,
) -> tuple[Path | None, str, Path | None]:
    """定位任务输入：`inputs/tasks/<id>/`（单任务）或 `inputs/`（默认全量）。

    三种真实快照布局都要支持：
    - `inputs/tasks/<id>/`：一次下载 22 题的批量快照（--all-law）；
    - `inputs/<id>/`：按任务分开的快照目录；
    - 快照本身就是单个任务（`inputs/agreements` 或 `inputs/facts.md`）：
      此时 `--task <id>` 只用于标记报告里的 taskId，不要求目录名等于任务 id
      （单任务快照的目录名常是 snapshot/001 这类，不能因此判定"缺输入"）。

    只返回路径与请求文本；不读取 reference/、真值或评分器内部答案。
    """
    if task_filter:
        candidates = [
            snapshot_root / "inputs/tasks" / task_filter,
            snapshot_root / "inputs" / task_filter,
            snapshot_root / task_filter,
        ]
        for base in candidates:
            if _has_task_inputs(base):
                return _task_agreements(base), _request_text(base), base
        # 单任务快照兜底：inputs/ 直接就是该任务的输入
        if _has_task_inputs(snapshot_root / "inputs"):
            base = snapshot_root / "inputs"
            return _task_agreements(base), _request_text(base), base
        if _has_task_inputs(snapshot_root):
            return (_task_agreements(snapshot_root), _request_text(snapshot_root),
                    snapshot_root)
        return None, "", None
    agreements_dir = snapshot_root / "inputs/agreements"
    if not agreements_dir.is_dir():
        return None, "", None
    return agreements_dir, _request_text(snapshot_root), snapshot_root


def _has_task_inputs(base: Path) -> bool:
    """任务输入目录判定：合同类看 agreements/，刑事类看 facts.md。"""
    return (base / "agreements").is_dir() or (base / "facts.md").is_file()


def _task_agreements(base: Path) -> Path:
    """返回"材料目录"：合同类是 agreements/，刑事类直接用任务根目录（含 facts.md 与 law/）。"""
    return base / "agreements" if (base / "agreements").is_dir() else base


def task_kind(base: Path) -> str:
    """任务类型：contract（协议 × deal point）或 criminal（多被告裁判）。

    只看输入文件构成，不看任务 id、不看 reference/，避免为某个任务特调。
    与 `_find` 一致地兼容两种快照布局：任务目录本身即输入（`base/agreements`），
    或快照根目录下还有一层 `inputs/`（`base/inputs/agreements`）。
    """
    has_agreements = (base / "agreements").is_dir() or (base / "inputs" / "agreements").is_dir()
    return "contract" if has_agreements else "criminal"


def _criminal_defendants(base: Path) -> list[str]:
    """被告全集（来自 defendants.json，不是真值）。"""
    path = base / "defendants.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):
        return [str(d) for d in (data.get("defendants") or []) if str(d or "").strip()]
    if isinstance(data, list):
        return [str(d) for d in data if str(d or "").strip()]
    return []


def _criminal_skeleton(base: Path) -> dict[str, Any]:
    """答案骨架（来自 judgment.template.json；缺失时按被告生成空骨架）。"""
    path = base / "judgment.template.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    empty = {
        "charges": [], "articles": [], "roles": {}, "leader": False,
        "circumstances": [], "charge_terms": {},
        "final_term": {"type": "", "months": None}, "probation": False,
    }
    return {"defendants": {d: json.loads(json.dumps(empty))
                           for d in _criminal_defendants(base)}}


def _criminal_contract(base: Path) -> list[dict[str, Any]]:
    """刑事任务契约：judgment.json（逐被告）+ reasoning.md。

    judgment.json 声明 `expectedEntities`（被告全集，每个被告带模板字段）与
    `jsonPath=defendants`：契约据此生成「被告 × 字段」工作单元与答案格，
    交付控制器按被告键校验覆盖，而不是只判断文件非空。
    """
    defendants = _criminal_defendants(base)
    skeleton = _criminal_skeleton(base)
    fields_by_defendant: dict[str, list[str]] = {}
    for name, entry in (skeleton.get("defendants") or {}).items():
        if isinstance(entry, dict):
            fields_by_defendant[str(name)] = list(entry)
    fallback_fields = [
        "charges", "articles", "roles", "leader",
        "circumstances", "charge_terms", "final_term", "probation",
    ]
    spec: dict[str, Any] = {
        "name": "judgment.json",
        "format": "json",
        "title": "多被告裁判结果",
        "jsonPath": "defendants",
        "skeleton": skeleton,
        "description": "按模板逐被告填写，键名不得改动；无法确认的字段留空",
    }
    if defendants:
        spec["expectedEntities"] = [
            {
                "id": d,
                "label": d,
                "fields": fields_by_defendant.get(d) or fallback_fields,
            }
            for d in defendants
        ]
    return [
        spec,
        {
            "name": "reasoning.md",
            "format": "md",
            "title": "证据与推理",
            "description": "说明证据出处、推理过程与未能确定的事项",
        },
    ]


def _criminal_request_text(base: Path) -> str:
    import json as _json

    instruction = base / "instruction.md"
    template = (base / "judgment.template.json").read_text(encoding="utf-8") \
        if (base / "judgment.template.json").is_file() else _json.dumps(
            _criminal_skeleton(base), ensure_ascii=False, indent=1)
    head = instruction.read_text(encoding="utf-8", errors="replace") \
        if instruction.is_file() else "请依据案卷材料逐被告作出裁判。"
    return (
        f"{head}\n\n## 答案模板（必须逐字段填写，键名不能改）\n{template}\n\n"
        "## 交付要求\n"
        "- 写 submission/judgment.json（严格 JSON，UTF-8，不要 markdown 围栏）。\n"
        "- 写 submission/reasoning.md 说明证据出处与未能确定的事项。\n"
        "- 罪名用标准罪名并以「罪」结尾；articles 只填刑法分则条文号（整数）。\n"
        "- 无法从原文确认的字段留空并在 reasoning.md 标注，不要编造。\n"
    )


def run_full_profile(snapshot_root: Path, reference_dir: Path | None,
                     out: Path | None = None, *,
                     runtime: str = "v2",
                     collect_harness_metrics: bool = False,
                     fault: str | None = None,
                     fault_at_call: int = 1,
                     interrupt_at: int | None = None,
                     task_filter: str | None = None,
                     workspace_dir: Path | None = None) -> dict[str, Any]:
    """跑一次完整任务并评分。

    注意：这里刻意不自动创建/导入演示案件——全量档要求在真实工作空间上跑，
    导入与任务创建都通过公开服务接口，保证与用户操作路径一致。

    `runtime`（v1/v2）只影响运行时版本钉住与工作全集登记：v1 不登记工作账本，
    用于与 V1 基线对拍；`fault` 在模型网关上注入一次故障（B-06 故障注入）；
    `task_filter`（如 WFA-LAW-001）只跑 `inputs/tasks/<id>/` 下的单个任务目录。

    `workspace_dir`：每次运行使用独立工作空间（--repeat/--all-law 必须隔离），
    否则上一次的 submission 会被下一次当作"已答内容"读进账本，导致重复提交
    与分数污染。不传时沿用当前已打开的工作空间（人工操作路径）。
    """
    agreements_dir, request_text, contract_root = _resolve_task_inputs(
        snapshot_root, task_filter)
    if agreements_dir is None or contract_root is None:
        return {"profile": "full", "ok": False,
                "error": f"缺少任务输入目录（task={task_filter or 'default'}）"}
    if reference_dir is None:
        reference_dir = resolve_reference(snapshot_root, task_filter)
    if reference_dir is None:
        return {"profile": "full", "ok": False,
                "error": f"缺少 reference/（task={task_filter or 'default'} 的官方评分器与真值）"}
    # 任务类型只看输入构成（协议目录 / facts.md），不看任务 id，也不看 reference/
    kind = task_kind(contract_root)
    if kind == "contract" and not (reference_dir / "task_meta.json").is_file():
        return {"profile": "full", "ok": False,
                "error": "合同任务缺少 reference/task_meta.json（官方契约元数据）"}
    if kind == "criminal" and not (reference_dir / "gold.json").is_file():
        return {"profile": "full", "ok": False,
                "error": "刑事任务缺少 reference/gold.json（官方真值）"}
    if workspace_dir is not None:
        # 走产品公开入口初始化/切换工作空间（含密钥环、V50 迁移、首次种子），
        # 不在评测里绕开 ensure_workspace 自建库。
        from lawbuddy.workspace import open_workspace

        workspace_dir.mkdir(parents=True, exist_ok=True)
        try:
            open_workspace(str(workspace_dir), name=f"WFA {task_filter or 'snapshot'}")
        except Exception as exc:  # noqa: BLE001 工作空间不可用时如实报错
            return {"profile": "full", "ok": False,
                    "error": f"工作空间初始化失败：{type(exc).__name__}: {str(exc)[:160]}"}
    # 全量档必须在已打开的工作空间上跑（与用户操作路径一致）：未初始化时给出
    # 可执行的提示，而不是抛 "workspace not initialized" 堆栈。
    try:
        from lawbuddy.storage import get_db

        get_db()
    except RuntimeError:
        return {
            "profile": "full",
            "ok": False,
            "error": (
                "工作空间未初始化：请先启动应用（或调用 workspace.open）打开一个工作空间，"
                "或用 --workspace 指定隔离目录；评测不自行创建演示案件，以免污染真实工作空间。"
            ),
        }

    from lawbuddy import cases, documents
    from lawbuddy.agent.runner import AgentRunner
    from lawbuddy.agent.tasks import create_task, get_events, get_task, set_setting

    # 运行时版本（B-06 / D-01）：通过统一设置项切换，任务创建时据此钉住，
    # 不使用评测专用旁路；v1 用于与 V1 基线对拍。
    if runtime in ("v1", "v2"):
        try:
            set_setting("agent.runtimeVersion", runtime)
        except Exception:  # noqa: BLE001 设置写入失败沿用既有值
            pass

    injector = None
    restore_gateway = None
    if fault:
        from lawbuddy.agent import model_gateway
        from .harness import FaultInjector

        injector = FaultInjector(fault, fault_at_call)
        original = model_gateway.chat_with_tools
        model_gateway.chat_with_tools = injector.wrap(original)

        def restore_gateway() -> None:  # noqa: F811 — 收尾恢复原实现
            model_gateway.chat_with_tools = original

    started = time.monotonic()
    if kind == "contract":
        case = cases.create_case(
            title=f"WFA LAW 回归 {time.strftime('%Y-%m-%d %H:%M')}",
            case_type="commercial",
            client_name="回归评测",
            default_mode="legal_review",
        )
        contract = _build_contract(contract_root)
    else:
        case = cases.create_case(
            title=f"WFA LAW 刑事回归 {time.strftime('%Y-%m-%d %H:%M')}",
            case_type="criminal",
            client_name="回归评测",
            client_role="neutral",
            stage="judgment",
            default_mode="legal_review",
        )
        contract = _criminal_contract(contract_root)
        request_text = _criminal_request_text(contract_root)
    paths = _task_sources(agreements_dir, kind)
    case_id = case["id"]
    imported = documents.import_documents(case_id, paths)

    task = create_task(
        case_id=case_id,
        mode="legal_review",
        request_text=request_text,
        deliverables=contract,
    )
    # 无人值守评测：审批由本进程按产品正式通道（policy.respond）自动放行，
    # 不绕过权限层，也不放宽案件的 permission_preset；否则任务会停在
    # WAITING_APPROVAL 直到超时，评测结果毫无意义。
    approvals: list[str] = []
    stop_approver = threading.Event()

    def approve_pending() -> None:
        from lawbuddy import policy
        from lawbuddy.storage import get_db

        while not stop_approver.is_set():
            try:
                rows = get_db().query(
                    "SELECT id FROM approval_requests WHERE task_id=? AND status='pending'",
                    (task["id"],),
                )
                for row in rows:
                    policy.respond(row["id"], "allow_task")
                    approvals.append(str(row["id"]))
            except Exception:  # noqa: BLE001 审批通道异常不掩盖主流程错误
                pass
            stop_approver.wait(1.0)

    approver = threading.Thread(target=approve_pending, daemon=True)
    approver.start()
    AgentRunner.instance().start(task["id"])

    # --interrupt-at：按轮数百分比取消任务，验证部分交付在中断点仍完整可解析
    interrupt_plan: dict[str, Any] | None = None
    if interrupt_at:
        from lawbuddy.agent import budget as budget_mod

        max_rounds = int((budget_mod.snapshot(task["id"]).get("limits") or {})
                         .get("maxRounds") or 0)
        stop_round = max(1, int(max_rounds * int(interrupt_at) / 100)) if max_rounds else 1
        interrupt_plan = {"atPercent": int(interrupt_at), "stopRound": stop_round,
                          "maxRounds": max_rounds}

    deadline = started + DEFAULT_TIMEOUT_S
    final: dict[str, Any] = {}
    while time.monotonic() < deadline:
        info = get_task(task["id"])
        final = info
        if info["lifecycle"] in ("COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED", "CANCELLED"):
            break
        if interrupt_plan:
            used = int(((info.get("budgetProgress") or {}).get("used") or {}).get("round") or 0)
            if used >= int(interrupt_plan["stopRound"]):
                AgentRunner.instance().cancel(task["id"])
                interrupt_plan["cancelledAtRound"] = used
                time.sleep(1.0)
                final = get_task(task["id"])
                break
        time.sleep(POLL_INTERVAL_S)
    stop_approver.set()
    if restore_gateway is not None:
        restore_gateway()

    from lawbuddy.sandbox import workspace_root

    submission = workspace_root() / "submission"
    if kind == "contract":
        table, answers = _read_submission(submission)
        from .scorer import score_submission

        fixture = _official_fixture(reference_dir)
        score = score_submission(table, answers, fixture, reference_dir=reference_dir) if fixture else {
            "branch": 0.0, "merge": 0.0, "error": "缺少官方真值，无法评分",
        }
    else:
        score = _criminal_score(reference_dir, submission)
    # 事件必须全量取出：`get_events` 默认只回前 200 条，而 `turn_complete`（效率、
    # 覆盖率、收尾状态）永远在队尾，截断后 C-04 第 5/6 条与 C-06 指标会恒为 null。
    # 长任务事件上千条，分页拉全后再交给指标汇总。
    events = _all_events(task["id"])
    gate = next((e["payload"] for e in events
                 if e["type"] == "deliverable_skeleton"), {})
    # 综合分（官方口径）= (branch + merge) / 2 × 100：C-04/C-05 门槛按综合分判定，
    # 不写进结果就只能人工换算，门槛复核无法复算。
    branch_value = float(score.get("branch") or 0.0)
    merge_value = float(score.get("merge") or 0.0)
    result = {
        "profile": "full",
        "runtime": runtime,
        "task": task_filter,
        "kind": kind,
        "ok": final.get("lifecycle") in ("COMPLETED", "COMPLETED_WITH_WARNINGS")
        and score.get("branch", 0) > 0,
        "caseId": case_id,
        "taskId": task["id"],
        "lifecycle": final["lifecycle"] if final else None,
        "deliveryStatus": (final or {}).get("deliveryStatus"),
        "imported": len([r for r in imported if r.get("status") == "imported"]),
        "skeleton": gate.get("files") or [],
        "durationS": int(time.monotonic() - started),
        "approvals": len(approvals),
        "composite": round((branch_value + merge_value) / 2 * 100, 2),
        **score,
    }
    if injector is not None:
        result["faultInjection"] = injector.report()
    if interrupt_plan is not None:
        result["interrupt"] = interrupt_plan
        # 中断点也必须留下可解析的主交付物（B-03 原子物化）
        result["submissionParseable"] = _submission_parseable(submission, kind)
    if collect_harness_metrics:
        from .harness import collect_metrics

        result["harness"] = collect_metrics(task["id"], events=events)
    if out is not None:
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def _all_events(task_id: str, page: int = 500) -> list[dict[str, Any]]:
    """按 seq 分页取全量事件（含队尾的 turn_complete）。

    `get_events` 的 limit 是单页上限而不是总量上限；长任务（上千条事件）用默认
    200 条会把效率指标、阅读覆盖和收尾门禁全部截掉，指标恒为 null。
    """
    from lawbuddy.agent.tasks import get_events

    out: list[dict[str, Any]] = []
    cursor = 0
    while True:
        batch = get_events(task_id, after_seq=cursor, limit=page)
        if not batch:
            return out
        out.extend(batch)
        cursor = int(batch[-1]["seq"])
        if len(batch) < page:
            return out


def _criminal_score(reference_dir: Path, submission: Path) -> dict[str, Any]:
    """刑事任务评分：逐字调用官方 `verify.grade`，不复制、不改写口径。

    官方 verify.py 只在 `reference/` 目录下，按模块名加载会与合同任务的
    verify.py 冲突（两个同名文件），所以这里用 importlib 按文件路径加载，
    每次重新执行模块代码，避免两次运行之间共享模块状态。
    """
    import importlib.util

    # 官方 verify.py 有两种存放位置：与 reference/ 并列（仓库布局），
    # 或就在 reference/ 内（快照布局）；两处都找，找不到如实报错。
    verify_path = None
    for candidate in (reference_dir.parent / "verify.py", reference_dir / "verify.py"):
        if candidate.is_file():
            verify_path = candidate
            break
    gold_path = reference_dir / "gold.json"
    sub_path = submission / "judgment.json"
    if verify_path is None:
        return {"branch": 0.0, "merge": 0.0, "error": "缺少官方 verify.py"}
    try:
        spec = importlib.util.spec_from_file_location("wfa_criminal_verify", verify_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except Exception as exc:  # noqa: BLE001 官方评分器无法加载时如实报错
        return {"branch": 0.0, "merge": 0.0, "error": f"官方评分器加载失败：{type(exc).__name__}"}
    try:
        gold = json.loads(gold_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"branch": 0.0, "merge": 0.0, "error": "缺少官方 gold.json"}
    try:
        sub = json.loads(sub_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # 官方口径：提交不可读按空提交计 0 分，不抛异常
        sub = {}
    report = module.grade(gold, sub)
    branch = report.get("branch") or {}
    merge = report.get("merge") or {}
    return {
        "branch": float(branch.get("score") or 0.0),
        "merge": float(merge.get("score") or 0.0),
        "score": round((float(branch.get("score") or 0.0)
                        + float(merge.get("score") or 0.0)) / 2 * 100, 2),
        "full_success": bool(report.get("full_success")),
        "case_all_correct": bool(report.get("case_all_correct")),
        "n_defendants": report.get("n_defendants"),
        "role_accuracy": merge.get("role_accuracy"),
        "scorer": "official",
        "submissionExists": sub_path.is_file(),
        "report": report,
    }


def _submission_parseable(directory: Path, kind: str = "contract") -> dict[str, Any]:
    """中断点检查：主/派生交付物是否都存在且可解析（只判结构，不判内容对错）。"""
    import csv

    names = ("deal_points.csv", "answers.json") if kind == "contract" \
        else ("judgment.json", "reasoning.md")
    out: dict[str, Any] = {"files": {}, "allParseable": False}
    for name in names:
        path = directory / name
        entry = {"exists": path.is_file(), "parseable": False, "rows": 0}
        if entry["exists"]:
            try:
                if name.endswith(".csv"):
                    with path.open(encoding="utf-8-sig", newline="") as fh:
                        entry["rows"] = sum(1 for _ in csv.DictReader(fh))
                    entry["parseable"] = True
                elif name.endswith(".json"):
                    data = json.loads(path.read_text(encoding="utf-8"))
                    entry["rows"] = len(data) if isinstance(data, (dict, list)) else 0
                    entry["parseable"] = True
                else:
                    # Markdown 只判"非空且不是围栏代码块外泄"，不做内容判断
                    entry["rows"] = len(path.read_text(encoding="utf-8").strip())
                    entry["parseable"] = entry["rows"] > 0
            except (OSError, csv.Error, json.JSONDecodeError, UnicodeDecodeError):
                entry["parseable"] = False
        out["files"][name] = entry
    out["allParseable"] = all(v["parseable"] for v in out["files"].values())
    return out


def _read_submission(directory: Path) -> tuple[dict[tuple[str, str], str], dict[str, Any]]:
    """读取 submission/ 下的交付物（缺失时返回空，由评分器判 0 分）。"""
    import csv

    table: dict[tuple[str, str], str] = {}
    answers: dict[str, Any] = {}
    csv_path = directory / "deal_points.csv"
    if csv_path.is_file():
        try:
            with csv_path.open(encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    norm = {(k or "").strip().lower(): v for k, v in row.items()}
                    a = str(norm.get("agreement") or "").strip().upper()
                    d = str(norm.get("deal_point") or "").strip()
                    if a and d:
                        table[(a, d)] = str(norm.get("answer") or "")
        except (OSError, csv.Error):
            pass
    json_path = directory / "answers.json"
    if json_path.is_file():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                answers = data
        except (OSError, json.JSONDecodeError):
            pass
    return table, answers


def _official_fixture(reference_dir: Path) -> dict[str, Any] | None:
    """用官方真值构造 fixture（评分口径与官方完全一致）。"""
    try:
        meta = json.loads((reference_dir / "task_meta.json").read_text(encoding="utf-8"))
        spec = json.loads((reference_dir / "merge_spec.json").read_text(encoding="utf-8"))
        gold_answers = json.loads((reference_dir / "answers.json").read_text(encoding="utf-8"))
        table: dict[tuple[str, str], str] = {}
        with (reference_dir / "deal_points.csv").open(encoding="utf-8-sig", newline="") as fh:
            import csv

            for row in csv.DictReader(fh):
                table[(row["agreement"].strip().upper(), row["deal_point"].strip())] = row["answer"]
    except (OSError, KeyError, json.JSONDecodeError):
        return None
    dps = meta.get("deal_points") or {}
    return {
        "name": "wfa-law-official",
        "agreements": meta.get("agreements") or [],
        "dealPoints": list(dps),
        "options": {dp: dpm.get("options") or [] for dp, dpm in dps.items()},
        "multi": {dp: bool(dpm.get("multi")) for dp, dpm in dps.items()},
        "table": table,
        "mergeSpec": spec.get("questions") or [],
        "goldAnswers": gold_answers,
    }
