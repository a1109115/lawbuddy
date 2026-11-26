"""工具注册表：工具元数据、副作用声明与中文检索（LB-HERMES-2026-01 WP01/WP06）。

Schema 本身仍由 `autonomous._tool_specs()` 提供（共享 OpenAI function 格式），
本模块只登记**元数据**，供以下三处消费：

1. `tool_runtime`：执行前判断副作用类别、幂等性、是否允许子代理/批处理；
2. `tool_search`：中文别名与关键词检索（渐进披露）；
3. 评测：断言每个下发的 Schema 都有元数据，避免新增工具绕过 Journal 与策略。

硬约束：
- 未登记的工具一律按最保守属性处理（`effect=external`、非幂等、不允许子代理/批处理），
  宁可多审批也不静默放行未知副作用。
- 元数据不包含工具参数默认值或敏感内容，可安全序列化进事件与诊断包。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

# 副作用类别（与 V51 tool_call_journal.effect 的 CHECK 一致）
EFFECT_READ = "read"
EFFECT_WRITE = "write"
EFFECT_DELIVERABLE = "deliverable"
EFFECT_MEMORY = "memory"
EFFECT_EXTERNAL = "external"
EFFECT_SHELL = "shell"

EFFECTS = (
    EFFECT_READ, EFFECT_WRITE, EFFECT_DELIVERABLE, EFFECT_MEMORY, EFFECT_EXTERNAL, EFFECT_SHELL,
)

# 并发作用域：同一作用域内的写入不得并行执行
SCOPE_NONE = "none"          # 无共享资源（只读、无状态）
SCOPE_WORKSPACE = "workspace"  # 工作空间文件系统
SCOPE_TASK = "task"          # 任务级状态（账本、交付物、记忆）
SCOPE_CASE = "case"          # 案件级状态（合同工作副本、风险落库）


@dataclass(frozen=True)
class ToolMeta:
    """单个工具的登记信息（不可变，避免运行时被意外改写）。"""

    name: str
    summary: str
    aliases: tuple[str, ...] = ()
    effect: str = EFFECT_EXTERNAL
    idempotent: bool = False
    concurrency_scope: str = SCOPE_TASK
    eager: bool = True
    subagent_allowed: bool = False
    batch_allowed: bool = False
    approval_capability: str | None = None
    version: str = "1"
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def read_only(self) -> bool:
        return self.effect == EFFECT_READ

    def public(self) -> dict[str, Any]:
        """可外发的元数据（不含参数与内部实现细节）。"""
        return {
            "name": self.name,
            "version": self.version,
            "summary": self.summary,
            "aliases": list(self.aliases),
            "effect": self.effect,
            "readOnly": self.read_only,
            "idempotent": self.idempotent,
            "concurrencyScope": self.concurrency_scope,
            "eager": self.eager,
            "subagentAllowed": self.subagent_allowed,
            "batchAllowed": self.batch_allowed,
            "approvalCapability": self.approval_capability,
            "tags": list(self.tags),
        }


def _m(
    name: str, summary: str, *, aliases: Iterable[str] = (), effect: str = EFFECT_READ,
    idempotent: bool = True, scope: str = SCOPE_NONE, eager: bool = True,
    subagent: bool = False, batch: bool = False, approval: str | None = None,
    tags: Iterable[str] = (),
) -> ToolMeta:
    return ToolMeta(
        name=name, summary=summary, aliases=tuple(aliases), effect=effect,
        idempotent=idempotent, concurrency_scope=scope, eager=eager,
        subagent_allowed=subagent, batch_allowed=batch,
        approval_capability=approval, tags=tuple(tags),
    )


# ---------------------------------------------------------------------------
# 登记表：键必须与 autonomous._tool_specs() 的工具名一一对应
# ---------------------------------------------------------------------------

_TOOLS: tuple[ToolMeta, ...] = (
    # ---- 案件检索与阅读（高频，保持 eager） ----
    _m(
        "documents_list", "列出当前案件全部文档（卷宗材料清单）",
        aliases=("文档列表", "材料清单", "卷宗目录", "有哪些文件"),
        subagent=True, batch=True, tags=("retrieval", "case"),
    ),
    _m(
        "document_search", "在卷宗内按页全文检索，可多路合并并按文档限定",
        aliases=("检索", "搜索原文", "查找关键词", "全文搜索", "查一下"),
        subagent=True, batch=True, tags=("retrieval", "search", "case"),
    ),
    _m(
        "document_get_pages", "读取指定文档指定页码的原文，支持游标续读",
        aliases=("读原文", "读第几页", "查看页", "打开文档", "读第3页"),
        subagent=True, batch=True, tags=("retrieval", "read", "case"),
    ),
    _m(
        "memory_search", "检索本案件记忆（历史结论、人物、时间线、用户更正）",
        aliases=("记忆", "查历史结论", "之前分析过什么", "回忆"),
        subagent=True, batch=True, tags=("retrieval", "memory"),
    ),
    _m(
        "memory_write", "写入案件记忆（需带原文来源）",
        aliases=("记下来", "记住这个", "写入记忆"),
        effect=EFFECT_MEMORY, idempotent=False, scope=SCOPE_TASK, eager=False,
        tags=("memory",),
    ),
    _m(
        "case_ledger", "登记证据与争点到案件账本",
        aliases=("证据账本", "登记证据", "记录争点"),
        effect=EFFECT_MEMORY, idempotent=False, scope=SCOPE_TASK, eager=False,
        tags=("memory", "evidence"),
    ),
    # ---- 子代理与工作单元 ----
    _m(
        "case_explore", "把定向调查问题委托给检索子代理（只读、并行、共享预算）",
        aliases=("子代理", "委托调查", "并行检索", "让子代理查"),
        effect=EFFECT_READ, scope=SCOPE_NONE, tags=("subagent", "retrieval"),
    ),
    _m(
        "work_unit_run", "按工作全集派发工作型子代理，结果写入暂存区与答案格",
        aliases=("批量处理", "工作单元", "逐份处理", "格子任务"),
        effect=EFFECT_WRITE, idempotent=False, scope=SCOPE_TASK, tags=("subagent", "delivery"),
    ),
    # ---- 会话级计划 ----
    _m(
        "task_plan", "维护当前任务的步骤清单",
        aliases=("计划", "任务清单", "步骤", "待办"),
        scope=SCOPE_NONE, tags=("planning",),
    ),
    # ---- 合同 ----
    _m(
        "contract_create_review", "创建合同审查记录并落库风险项",
        aliases=("合同审查", "审合同", "合同风险"),
        effect=EFFECT_WRITE, idempotent=False, scope=SCOPE_CASE, tags=("contract",),
    ),
    _m(
        "contract_split_clauses", "把合同切分为条款并登记",
        aliases=("拆分条款", "条款切分"),
        effect=EFFECT_WRITE, idempotent=False, scope=SCOPE_CASE, tags=("contract",),
    ),
    _m(
        "contract_get_clauses", "读取已切分条款及其原文",
        aliases=("查看条款", "读条款"),
        tags=("contract", "retrieval"),
    ),
    # ---- 文书生成 ----
    _m(
        "docgen_list_templates", "列出可用的文书模板",
        aliases=("文书模板", "模板列表"),
        tags=("docgen",),
    ),
    _m(
        "docgen_create_free_draft", "创建不套模板的自由文书草稿",
        aliases=("自由文书", "新建草稿"),
        effect=EFFECT_WRITE, idempotent=False, scope=SCOPE_TASK, tags=("docgen",),
    ),
    _m(
        "docgen_create_draft", "按模板创建文书草稿",
        aliases=("生成文书", "起草文书", "套模板"),
        effect=EFFECT_WRITE, idempotent=False, scope=SCOPE_TASK, tags=("docgen",),
    ),
    _m(
        "docgen_render_draft", "把文书草稿渲染为文件交付",
        aliases=("导出文书", "渲染文书", "生成文件"),
        effect=EFFECT_DELIVERABLE, idempotent=False, scope=SCOPE_TASK, tags=("docgen", "delivery"),
    ),
    _m(
        "docgen_get_draft", "读取文书草稿当前内容",
        aliases=("查看草稿", "读文书"),
        tags=("docgen", "retrieval"),
    ),
    _m(
        "docgen_revise_draft", "修改指定文书草稿",
        aliases=("改文书", "修订草稿"),
        effect=EFFECT_WRITE, idempotent=False, scope=SCOPE_TASK, tags=("docgen",),
    ),
    # ---- 人工决策 ----
    _m(
        "request_user_decision", "向用户提出必要的决策问题并暂停任务",
        aliases=("问用户", "需要确认", "让用户决定", "人工确认"),
        scope=SCOPE_NONE, tags=("human",),
    ),
    # ---- 技能 ----
    _m(
        "skill_install", "从本地文件安装技能（提示词包）",
        aliases=("安装技能", "导入技能"),
        effect=EFFECT_WRITE, idempotent=False, scope=SCOPE_TASK, tags=("skill",),
    ),
    _m(
        "skill_list", "列出已安装技能",
        aliases=("技能列表", "有哪些技能"),
        tags=("skill",),
    ),
    _m(
        "skill_remove", "删除已安装技能",
        aliases=("删除技能", "卸载技能"),
        effect=EFFECT_WRITE, idempotent=False, scope=SCOPE_TASK, tags=("skill",),
    ),
    # ---- 沙箱工作空间 ----
    _m(
        "read_file", "读取工作空间内的文件",
        aliases=("读文件", "打开文件"),
        subagent=True, batch=True, tags=("workspace",),
    ),
    _m(
        "write_file", "在工作空间写入文件（暂存后原子提交）",
        aliases=("写文件", "保存到文件", "导出文件"),
        effect=EFFECT_WRITE, idempotent=False, scope=SCOPE_WORKSPACE, eager=False,
        tags=("workspace",),
    ),
    _m(
        "edit_file", "修改工作空间内已存在文件的片段",
        aliases=("改文件", "替换内容"),
        effect=EFFECT_WRITE, idempotent=False, scope=SCOPE_WORKSPACE, eager=False,
        tags=("workspace",),
    ),
    _m(
        "glob_files", "按通配符列出工作空间文件",
        aliases=("列文件", "找文件", "文件匹配"),
        subagent=True, batch=True, eager=False, tags=("workspace",),
    ),
    _m(
        "grep_search", "在工作空间文件内容中检索",
        aliases=("内容检索", "搜文件内容"),
        subagent=True, batch=True, eager=False, tags=("workspace",),
    ),
    _m(
        "bash", "在工作空间执行 Shell 命令（需用户审批）",
        aliases=("执行命令", "跑脚本", "终端", "shell"),
        effect=EFFECT_SHELL, idempotent=False, scope=SCOPE_WORKSPACE, eager=False,
        tags=("workspace", "shell"),
    ),
    # ---- 校验与聚合 ----
    _m(
        "merge_fragments_check", "按主键合并多批片段并校验实体齐全",
        aliases=("合并片段", "分批合并", "校验合并"),
        scope=SCOPE_NONE, tags=("validation",),
    ),
    _m(
        "options_check", "多选逐项核对，要求逐项可回查原文",
        aliases=("逐项核对", "多选题核对"),
        scope=SCOPE_NONE, tags=("validation",),
    ),
    _m(
        "clause_role_check", "同族条款消歧校验（引用定义与主体是否同侧）",
        aliases=("条款消歧", "角色核对"),
        scope=SCOPE_NONE, tags=("validation", "contract"),
    ),
    _m(
        "table_aggregate", "跨文档聚合求值（机械计算，禁止模型心算）",
        aliases=("汇总表格", "聚合统计", "求和"),
        tags=("validation", "aggregate"),
    ),
    # ---- 工具发现（WP06：渐进披露；自身不产生副作用） ----
    # 注意：这里不放"怎么生成图表"之类的具体任务别名——否则检索任何具体任务时
    # 发现工具自身都会挤进前列，把真正能完成任务的工具挤下去。
    _m(
        "tool_search", "按中文描述检索可调用工具及其参数（只返回有权调用的工具）",
        aliases=("找工具", "有哪些工具", "工具检索", "工具列表", "能用什么工具"),
        scope=SCOPE_NONE, tags=("discovery",),
    ),
    _m(
        "tool_describe", "查看某个工具的完整参数说明（不执行）",
        aliases=("工具说明", "参数说明", "工具参数", "tool describe"),
        scope=SCOPE_NONE, tags=("discovery",),
    ),
    _m(
        "tool_call", "按名称调用检索到的低频工具（仍走策略、审批、Journal 与预算）",
        aliases=("调用工具", "执行工具", "tool call"),
        effect=EFFECT_EXTERNAL, idempotent=False, scope=SCOPE_TASK,
        tags=("discovery",),
    ),
    _m(
        "tool_batch", "批量并行只读检索（声明式 DSL；写入/交付/Shell 一律拒绝）",
        aliases=("批量检索", "并行查", "一起查", "批量读取"),
        scope=SCOPE_NONE, tags=("discovery", "batch"),
    ),
    # ---- 交付回写 ----
    _m(
        "deliverable_patch", "按主键把内容回写到本次任务的交付物",
        aliases=("回写交付物", "填表", "写入csv"),
        effect=EFFECT_DELIVERABLE, idempotent=True, scope=SCOPE_TASK,
        tags=("delivery",),
    ),
    # ---- 渲染 ----
    _m(
        "render_html", "把分析结果渲染为可视化 HTML 图表",
        aliases=("生成图表", "可视化", "画关系图", "时间轴图"),
        effect=EFFECT_WRITE, idempotent=False, scope=SCOPE_WORKSPACE, tags=("render",),
    ),
    _m(
        "render_pptx", "把分析结果渲染为 PPTX 演示文稿",
        aliases=("生成PPT", "幻灯片", "演示文稿"),
        effect=EFFECT_WRITE, idempotent=False, scope=SCOPE_WORKSPACE, tags=("render",),
    ),
)

REGISTRY: dict[str, ToolMeta] = {m.name: m for m in _TOOLS}

# 未登记工具的保守兜底：视为外部副作用、非幂等、不允许子代理与批处理
_UNKNOWN = ToolMeta(
    name="__unknown__", summary="未登记工具", effect=EFFECT_EXTERNAL,
    idempotent=False, concurrency_scope=SCOPE_TASK, eager=False,
    subagent_allowed=False, batch_allowed=False,
)


class RegistryError(RuntimeError):
    pass


def get(name: str) -> ToolMeta:
    """取工具元数据；未登记时返回保守兜底（不抛错，避免执行路径因登记缺失而中断）。"""
    return REGISTRY.get(str(name or ""), _UNKNOWN)


def is_registered(name: str) -> bool:
    return str(name or "") in REGISTRY


def descriptors(names: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """按名称顺序返回可外发元数据；None 表示全部。"""
    if names is None:
        return [m.public() for m in _TOOLS]
    return [get(n).public() for n in names if is_registered(n)]


def unregistered(specs: Iterable[dict[str, Any]]) -> list[str]:
    """返回 Schema 中存在但未登记元数据的工具名（评测门禁用）。"""
    out: list[str] = []
    for spec in specs:
        fn = spec.get("function") if isinstance(spec, dict) else None
        name = str((fn or {}).get("name") or "")
        if name and name not in REGISTRY:
            out.append(name)
    return out


def effect_of(name: str) -> str:
    return get(name).effect


def is_read_only(name: str) -> bool:
    return get(name).read_only


def concurrent_safe_pair(a: str, b: str) -> bool:
    """两个工具能否并行执行：同为只读、或作用域互不相交且都非案件级写入。"""
    ma, mb = get(a), get(b)
    if ma.read_only and mb.read_only:
        return True
    if ma.concurrency_scope == SCOPE_NONE and mb.concurrency_scope == SCOPE_NONE:
        return True
    return False


# ---------------------------------------------------------------------------
# 中文检索（WP06）：精确名称加权 + 别名/摘要子串 + 单字/bigram/trigram 覆盖
# ---------------------------------------------------------------------------

def _bigrams(text: str) -> set[str]:
    return {text[i:i + 2] for i in range(max(0, len(text) - 1))}


def _trigrams(text: str) -> set[str]:
    return {text[i:i + 3] for i in range(max(0, len(text) - 2))}


def _score(query: str, meta: ToolMeta) -> float:
    q = query.strip().lower()
    if not q:
        return 0.0
    name = meta.name.lower()
    score = 0.0
    # 精确名称与名称前缀优先
    if q == name:
        return 100.0
    if name.startswith(q):
        score += 40.0
    elif q in name:
        score += 20.0
    haystack = " ".join((meta.summary, *meta.aliases, *meta.tags)).lower()
    if q in haystack:
        score += 12.0
    for alias in meta.aliases:
        al = alias.lower()
        if q == al:
            score += 30.0
        elif q in al or al in q:
            score += 10.0
    # 中文单字/二元/三元覆盖：短查询靠字符覆盖率兜底
    q_bi, q_tri = _bigrams(q), _trigrams(q)
    hay_bi, hay_tri = _bigrams(haystack), _trigrams(haystack)
    if q_bi:
        score += 6.0 * len(q_bi & hay_bi) / len(q_bi)
    if q_tri:
        score += 8.0 * len(q_tri & hay_tri) / len(q_tri)
    if set(q) & set(haystack):
        score += 2.0 * len(set(q) & set(haystack)) / max(1, len(set(q)))
    return score


# 相关性下限：低于此分视为噪声（避免把完全不相关的工具当命中返回）
MIN_SCORE = 5.0


def search(
    query: str, *, allowed: set[str] | None = None, limit: int = 8,
    min_score: float = MIN_SCORE,
) -> list[dict[str, Any]]:
    """按中文相关性排序检索工具。

    `allowed` 是策略过滤后的可调用工具集合，**必须先于相关性排序**：
    模型不能搜索到无权调用的工具（WP06 硬约束）。

    命中条件：名称/别名/摘要命中，或存在二元以上字符片段重合。仅靠单字重合
    的候选一律丢弃——中文单字重合几乎必然产生噪声（如"写文件"命中"有哪些文件"）。
    """
    candidates = [
        m for m in _TOOLS
        if allowed is None or m.name in allowed
    ]
    scored = [(_score(query, m), _fragment_match(query, m), m) for m in candidates]
    ranked = sorted(
        (item for item in scored if item[1]),
        key=lambda item: (-item[0], item[2].name),
    )
    hits = [(s, m) for s, _frag, m in ranked if s >= min_score][:limit]
    return [{**m.public(), "score": round(s, 2)} for s, m in hits]


def _fragment_match(query: str, meta: ToolMeta) -> bool:
    """是否存在有意义的片段重合（名称/别名/摘要的 2 字以上重合）。"""
    q = query.strip().lower()
    if not q:
        return False
    name = meta.name.lower()
    if q in name or any(al.lower() in q or q in al.lower() for al in meta.aliases):
        return True
    haystack = " ".join((meta.summary, *meta.aliases, *meta.tags)).lower()
    if q in haystack:
        return True
    return bool(_bigrams(q) & _bigrams(haystack))


def categories(allowed: set[str] | None = None) -> dict[str, list[str]]:
    """按标签归类可用工具，用于检索未命中时给出可理解的降级建议。"""
    out: dict[str, list[str]] = {}
    for m in _TOOLS:
        if allowed is not None and m.name not in allowed:
            continue
        for tag in m.tags or ("other",):
            out.setdefault(tag, []).append(m.name)
    return out
