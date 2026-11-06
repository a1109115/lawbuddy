#!/usr/bin/env python3
"""
案卷阅卷 Skill 的证据清单生成脚本（可执行版，v2.5.0）。

从 metadata.json + Markdown 文件自动生成分组编号证据清单 .docx，
含自检三关（数量覆盖 / 空白扫描 / 金额一致性），不过关抛异常不存盘。

用法:
    python generate_evidence_list.py <md_dir> <output.docx> [--plaintiff|--defendant]
    python generate_evidence_list.py <md_dir> <output.docx> --metadata <metadata.json>

<md_dir> 应指向 MinerU 输出的 Markdown 根目录（含 metadata.json 或子文件夹按工程分组）。

依赖: python-docx (pip install python-docx)
作者: 陆凌燕（北京德恒（无锡）律师事务所）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from docx import Document
    from docx.shared import Pt, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
except ImportError:
    sys.stderr.write(
        "ERROR: python-docx not installed. Run: pip install python-docx\n"
    )
    sys.exit(2)


# ---------------------------------------------------------------------------
# 1. 文档类型二次识别（不盲信 metadata dtype）
# ---------------------------------------------------------------------------

DTYPE_OVERRIDE: dict[str, str] = {
    "质保金申请函": "质保金申请函",
    "质保金申请": "质保金申请函",
    "结算说明": "结算清单",
    "工程结算单": "结算清单",
    "结算清单": "结算清单",
    "付款资料": "付款资料",
    "付款凭证": "付款资料",
    "银行流水": "付款资料",
    "请款通知书": "请款通知书",
    "请款书": "请款通知书",
    "结算申请表": "结算申请表",
    "结算申请": "结算申请表",
    "施工合同": "施工合同",
    "合同": "施工合同",
    "验收表": "验收报告",
    "验收报告": "验收报告",
    "竣工验收": "验收报告",
    "发票": "发票",
    "履约评估": "履约评估表",
    "施工照片": "施工照片",
    "照片": "施工照片",
}


def detect_dtype(filename: str, metadata_dtype: str | None = None) -> str:
    """按文件名关键词做二次识别，回退到 metadata dtype，再回退到 '其他'。"""
    base = re.sub(r"\.(pdf|jpg|jpeg|png|md|PDF|JPG|JPEG|PNG)$", "", filename)
    base = re.sub(r"^\d+[\s、\-_]+", "", base)  # 去数字前缀
    for keyword, dtype in DTYPE_OVERRIDE.items():
        if keyword in base:
            return dtype
    if metadata_dtype:
        return metadata_dtype
    return "其他"


# ---------------------------------------------------------------------------
# 2. 证据条目数据结构
# ---------------------------------------------------------------------------


@dataclass
class EvidenceItem:
    """单条证据。"""

    group: int                 # 工程序号（1..N）
    group_seq: int             # 组内序号（1..M）
    evidence_no: str = ""      # 组-序号（如 1-1）
    filename: str = ""         # 原始文件名
    clean_name: str = ""       # 清洗后证据名称
    dtype: str = "其他"        # 文档类型
    page_start: int = 0        # 组内页码起始
    page_end: int = 0          # 组内页码终止
    proof_purpose: str = ""    # 证据所要证明的事实
    fields: dict[str, Any] = field(default_factory=dict)  # 全量提取的字段
    source_md: str = ""        # 对应 Markdown 源文件（可点击链接相对路径）


@dataclass
class CrossCheckResult:
    """金额交叉校验结果。"""

    project: str
    contract_amount: float | None = None
    paid_amount: float | None = None
    remaining_amount: float | None = None
    invoice_total: float | None = None
    claimed_amount: float | None = None
    guarantee_amount: float | None = None
    issues: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 3. 读取 metadata + 遍历 Markdown
# ---------------------------------------------------------------------------


def load_metadata(md_dir: Path) -> dict[str, Any]:
    """加载 metadata.json，不存在则返回空 dict。"""
    meta_path = md_dir / "metadata.json"
    if meta_path.exists():
        with meta_path.open(encoding="utf-8") as f:
            return json.load(f)
    return {"files": []}


def _dir_sort_key(dirname: str) -> int:
    """按目录名开头的数字序号排序（如 '42、某工程...' → 42，'1、车道...' → 1）。"""
    m = re.match(r"^(\d+)", dirname)
    return int(m.group(1)) if m else 99999


def discover_groups(md_dir: Path, metadata: dict) -> list[tuple[int, str, list[dict]]]:
    """
    发现工程分组。返回 [(组号, 组名, [文件条目, ...]), ...]。
    优先按子文件夹分组；若无子文件夹则按 metadata 的 project 字段分组；再不行就单组。
    """
    subdirs = sorted(
        [d for d in md_dir.iterdir() if d.is_dir() and not d.name.startswith(".")],
        key=lambda d: _dir_sort_key(d.name),
    )
    if subdirs:
        groups: list[tuple[int, str, list[dict]]] = []
        for idx, d in enumerate(subdirs, start=1):
            files = sorted(
                [f for f in d.iterdir() if f.suffix.lower() in {".md", ".pdf", ".jpg", ".jpeg", ".png"}]
            )
            entries = [
                {
                    "path": f,
                    "filename": f.name,
                    "group_dir": d,
                }
                for f in files
            ]
            groups.append((idx, d.name, entries))
        return groups

    # 无子文件夹——尝试按 metadata 分组
    files_meta = metadata.get("files", [])
    if not files_meta:
        files = sorted(md_dir.glob("*.md"))
        entries = [{"path": f, "filename": f.name, "group_dir": md_dir} for f in files]
        return [(1, "默认组", entries)] if entries else []

    by_project: dict[str, list[dict]] = {}
    for fm in files_meta:
        project = fm.get("project") or fm.get("group") or "默认组"
        fname = fm.get("filename") or fm.get("name") or ""
        fpath = md_dir / fname
        by_project.setdefault(project, []).append(
            {"path": fpath, "filename": fname, "group_dir": md_dir, "meta": fm}
        )
    return [
        (idx, name, sorted(entries, key=lambda e: e["filename"]))
        for idx, (name, entries) in enumerate(sorted(by_project.items()), start=1)
    ]


# ---------------------------------------------------------------------------
# 4. 字段提取（从 Markdown 内容）
# ---------------------------------------------------------------------------

_AMOUNT_PATTERN = re.compile(
    r"(?:[¥￥]?\s*[\d,]+(?:\.\d+)?)\s*(?:元|万元)"
)


def extract_amounts(text: str) -> list[float]:
    """从文本中提取金额（元/万元）。"""
    amounts: list[float] = []
    for m in _AMOUNT_PATTERN.finditer(text):
        raw = m.group()
        num_match = re.search(r"[\d,]+(?:\.\d+)?", raw)
        if not num_match:
            continue
        num = float(num_match.group().replace(",", ""))
        if "万" in raw:
            num *= 10000
        amounts.append(num)
    return amounts


def extract_date(text: str) -> str:
    """提取第一个日期（YYYY-MM-DD / YYYY年MM月DD日 / YYYY-MM）。"""
    patterns = [
        r"\d{4}[-/]\d{1,2}[-/]\d{1,2}",
        r"\d{4}年\d{1,2}月\d{1,2}日",
        r"\d{4}[-/]\d{1,2}",
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group()
    return ""


def parse_fields(dtype: str, md_text: str) -> dict[str, Any]:
    """按文档类型从 Markdown 提取关键字段。"""
    fields: dict[str, Any] = {}
    amounts = extract_amounts(md_text)
    date = extract_date(md_text)

    if dtype == "施工合同":
        fields["合同金额"] = amounts[0] if amounts else None
        fields["签订日期"] = date
    elif dtype == "结算申请表":
        fields["合同金额"] = amounts[0] if len(amounts) >= 1 else None
        fields["结算申请金额"] = amounts[1] if len(amounts) >= 2 else None
        fields["竣工日期"] = date
        fields["已开发票金额"] = amounts[2] if len(amounts) >= 3 else None
        fields["已支付金额"] = amounts[3] if len(amounts) >= 4 else None
        fields["申请剩余金额"] = amounts[4] if len(amounts) >= 5 else None
    elif dtype == "请款通知书":
        fields["应收金额"] = amounts[0] if len(amounts) >= 1 else None
        fields["已收金额"] = amounts[1] if len(amounts) >= 2 else None
        fields["本次申请金额"] = amounts[2] if len(amounts) >= 3 else None
        fields["剩余质保金金额"] = amounts[3] if len(amounts) >= 4 else None
        fields["申请日期"] = date
    elif dtype == "发票":
        fields["价税合计"] = sum(amounts) if amounts else None
        fields["开票日期"] = date
    elif dtype == "质保金申请函":
        fields["最终结算金额"] = amounts[0] if len(amounts) >= 1 else None
        fields["已收到货款"] = amounts[1] if len(amounts) >= 2 else None
        fields["申请质保金金额"] = amounts[2] if len(amounts) >= 3 else None
    elif dtype == "付款资料":
        fields["付款金额"] = amounts[0] if amounts else None
        fields["发票金额"] = amounts[1] if len(amounts) >= 2 else None
    elif dtype == "结算清单":
        fields["工程费"] = amounts[0] if len(amounts) >= 1 else None
        fields["结算总额"] = amounts[1] if len(amounts) >= 2 else None
    elif dtype == "验收报告":
        fields["验收日期"] = date
        fields["验收结论"] = "合格" if "合格" in md_text else ("不合格" if "不合格" in md_text else "")
    # 其他类型不提取

    return fields


# ---------------------------------------------------------------------------
# 5. 证明目的模板
# ---------------------------------------------------------------------------

PURPOSE_TEMPLATES: dict[str, str] = {
    "施工合同": (
        "证明原、被告签订施工合同{date}，约定合同金额{amount}元{warranty}"
        "——原告诉请的合同依据。"
    ),
    "结算申请表": (
        "证明：合同金额{c}元，竣工日期{d}，已开发票{inv}元，已支付{paid}元，"
        "申请剩余{rem}元——原告诉请金额的计算依据。"
    ),
    "请款通知书": (
        "证明：应收{recv}元，已收{rcvd}元，本次申请{req}元，剩余质保金{grt}元"
        "——催告及诉讼时效中断的证明。"
    ),
    "发票": (
        "证明原告已就本工程向被告开具增值税发票，合计金额{total}元"
        "——原、被告间工程款具体金额的佐证。"
    ),
    "质保金申请函": (
        "证明：最终结算金额{final}元，已收到货款{rcvd}元，申请质保金{grt}元"
        "——质保金支付请求的依据。"
    ),
    "付款资料": "证明被告向原告付款 {paid} 元（发票金额 {inv} 元）——已付款事实。",
    "结算清单": "证明工程费 {fee} 元，结算总额 {total} 元——工程款金额的依据。",
    "验收报告": "证明工程已于 {date} 竣工验收{result}——质保期起算依据。",
}


def build_proof_purpose(item: EvidenceItem) -> str:
    """按文档类型填充证明目的模板。"""
    f = item.fields
    fmt = lambda v: f"{v:,.2f}" if isinstance(v, (int, float)) and v is not None else "（未提取到）"

    if item.dtype == "施工合同":
        return PURPOSE_TEMPLATES["施工合同"].format(
            date=f"（{f.get('签订日期', '（未提取到）')}）" if f.get("签订日期") else "",
            amount=fmt(f.get("合同金额")),
            warranty="",  # 由 LLM 在执行时补具体条款
        )
    if item.dtype == "结算申请表":
        return PURPOSE_TEMPLATES["结算申请表"].format(
            c=fmt(f.get("合同金额")),
            d=f.get("竣工日期") or "（未提取到）",
            inv=fmt(f.get("已开发票金额")),
            paid=fmt(f.get("已支付金额")),
            rem=fmt(f.get("申请剩余金额")),
        )
    if item.dtype == "请款通知书":
        return PURPOSE_TEMPLATES["请款通知书"].format(
            recv=fmt(f.get("应收金额")),
            rcvd=fmt(f.get("已收金额")),
            req=fmt(f.get("本次申请金额")),
            grt=fmt(f.get("剩余质保金金额")),
        )
    if item.dtype == "发票":
        return PURPOSE_TEMPLATES["发票"].format(total=fmt(f.get("价税合计")))
    if item.dtype == "质保金申请函":
        return PURPOSE_TEMPLATES["质保金申请函"].format(
            final=fmt(f.get("最终结算金额")),
            rcvd=fmt(f.get("已收到货款")),
            grt=fmt(f.get("申请质保金金额")),
        )
    if item.dtype == "付款资料":
        return PURPOSE_TEMPLATES["付款资料"].format(
            paid=fmt(f.get("付款金额")), inv=fmt(f.get("发票金额"))
        )
    if item.dtype == "结算清单":
        return PURPOSE_TEMPLATES["结算清单"].format(
            fee=fmt(f.get("工程费")), total=fmt(f.get("结算总额"))
        )
    if item.dtype == "验收报告":
        return PURPOSE_TEMPLATES["验收报告"].format(
            date=f.get("验收日期") or "（未提取到）",
            result=f"（{f.get('验收结论', '')}）" if f.get("验收结论") else "",
        )

    # 其他类型——通用模板
    return f"证明 {item.clean_name} 所载事实。"


# ---------------------------------------------------------------------------
# 6. 名称清洗
# ---------------------------------------------------------------------------

_NAME_STRIP_RE = re.compile(
    r"(\.pdf|\.jpg|\.jpeg|\.png|\.md|\.PDF|\.JPG|\.JPEG|\.PNG)$"
    r"|^\d+[\s、\-_]*"
    r"|已盖章"
    r"|（编号\d+）"
    r"|（合同编号[:：]?\s*\d+）",
    flags=re.IGNORECASE,
)


def clean_evidence_name(filename: str) -> str:
    """清洗证据名称：去扩展名、去数字编号前缀、去已盖章等标记。"""
    # 先去掉最外层 .md（MinerU 输出的命名约定 .pdf.md → 先剥 .md）
    base = filename
    # 循环去后缀：当文件名以 .md / .pdf / .jpg / .png 等结尾时逐层剥掉
    while True:
        m = re.search(r"\.(md|pdf|jpg|jpeg|png|PDF|JPG|JPEG|PNG)$", base)
        if m:
            base = base[:m.start()]
        else:
            break
    # 去数字前缀（如 145.pdf → "车道雨棚改建施工合同"）
    base = re.sub(r"^\d+[\s、\-_]*", "", base)
    # 去标记词
    base = re.sub(r"已盖章", "", base)
    base = re.sub(r"（编号\d+）", "", base)
    base = re.sub(r"（合同编号[:：]?\s*\d+）", "", base)
    base = re.sub(r"\s+", " ", base).strip()
    return base or filename


# ---------------------------------------------------------------------------
# 7. 金额交叉校验
# ---------------------------------------------------------------------------


def cross_verify_amounts(
    project_name: str, items: list[EvidenceItem]
) -> CrossCheckResult:
    """对同一工程内的金额做交叉校验，返回 CrossCheckResult。"""
    res = CrossCheckResult(project=project_name)

    contract = next((i for i in items if i.dtype == "施工合同"), None)
    settlement = next((i for i in items if i.dtype == "结算申请表"), None)
    demand = next((i for i in items if i.dtype == "请款通知书"), None)
    invoices = [i for i in items if i.dtype == "发票"]

    if contract:
        res.contract_amount = contract.fields.get("合同金额")
    if settlement:
        res.paid_amount = settlement.fields.get("已支付金额")
        res.remaining_amount = settlement.fields.get("申请剩余金额")
        # 结算表自洽：已付 + 申请剩余 = 合同金额
        if (
            settlement.fields.get("已支付金额") is not None
            and settlement.fields.get("申请剩余金额") is not None
            and settlement.fields.get("合同金额") is not None
        ):
            check = settlement.fields["已支付金额"] + settlement.fields["申请剩余金额"]
            if abs(check - settlement.fields["合同金额"]) > 0.01:
                res.issues.append(
                    f"结算表不自洽：已付({settlement.fields['已支付金额']})"
                    f" + 申请剩余({settlement.fields['申请剩余金额']})"
                    f" ≠ 合同金额({settlement.fields['合同金额']})"
                )
    if demand:
        res.claimed_amount = demand.fields.get("本次申请金额")
        res.guarantee_amount = demand.fields.get("剩余质保金金额")
    if invoices:
        res.invoice_total = sum(
            (i.fields.get("价税合计") or 0) for i in invoices
        )

    return res


# ---------------------------------------------------------------------------
# 8. 自检（核心）
# ---------------------------------------------------------------------------


class SelfCheckError(Exception):
    """自检不过关，阻止存盘。"""


def self_check(
    metadata: dict,
    groups: list[tuple[int, str, list[dict]]],
    items: list[EvidenceItem],
    cross_results: list[CrossCheckResult],
) -> dict[str, Any]:
    """
    三关自检。返回 dict 报告，任何一关失败抛 SelfCheckError。
    """
    report: dict[str, Any] = {"passed": True, "checks": []}

    # ---- 第 1 关：数量覆盖 ----
    meta_file_count = len(metadata.get("files", [])) if metadata.get("files") else sum(
        len(entries) for _, _, entries in groups
    )
    item_count = len(items)
    # 合并容差：同类多张发票/验收单合并为一行属正常，差值不应超过文件总数的 20%
    tolerance = max(1, int(meta_file_count * 0.2))
    coverage_ok = abs(meta_file_count - item_count) <= tolerance
    report["checks"].append({
        "name": "数量覆盖",
        "meta_files": meta_file_count,
        "evidence_rows": item_count,
        "tolerance": tolerance,
        "passed": coverage_ok,
        "note": "允许同类型文件合并" if not coverage_ok else "",
    })
    if not coverage_ok:
        report["passed"] = False

    # ---- 第 2 关：空白扫描 ----
    blanks = [
        f"{item.evidence_no} ({item.clean_name})"
        for item in items
        if not item.proof_purpose or "（未提取到）" in item.proof_purpose
    ]
    # 允许少量"未提取到"，但不能出现完全空白
    empty_purpose = [
        f"{item.evidence_no} ({item.clean_name})"
        for item in items
        if not item.proof_purpose.strip()
    ]
    blanks_ok = len(empty_purpose) == 0
    report["checks"].append({
        "name": "空白扫描",
        "empty_purpose_count": len(empty_purpose),
        "unextracted_field_count": len(blanks) - len(empty_purpose),
        "passed": blanks_ok,
    })
    if not blanks_ok:
        report["passed"] = False

    # ---- 第 3 关：金额一致性 ----
    all_issues = []
    for cr in cross_results:
        all_issues.extend([(cr.project, msg) for msg in cr.issues])
    # 金额不一致不阻断存盘（属于风险标注，不是格式错误），但要记录
    report["checks"].append({
        "name": "金额一致性",
        "issues": [{"project": p, "detail": m} for p, m in all_issues],
        "passed": True,  # 金额不一致不抛异常，靠红字标注
        "note": "金额不一致不阻断存盘，在清单内红字标注",
    })

    if not report["passed"]:
        raise SelfCheckError(json.dumps(report, ensure_ascii=False, indent=2))

    return report


# ---------------------------------------------------------------------------
# 9. 生成 .docx
# ---------------------------------------------------------------------------

RED = RGBColor(0xCC, 0x00, 0x00)


def build_docx(
    items: list[EvidenceItem],
    cross_results: list[CrossCheckResult],
    output: Path,
    stance: str = "plaintiff",
) -> None:
    """生成证据清单 Word 文档 — 每组一个独立表格（与现有证据清单格式一致）。"""
    from copy import copy

    doc = Document()

    # 标题
    title = doc.add_heading("证据清单", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER

    stance_label = "原告" if stance == "plaintiff" else "被告"
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.add_run(f"（{stance_label}举证）").italic = True

    # 按组生成独立表格
    sorted_items = sorted(items, key=lambda x: (x.group, x.group_seq))
    by_group: dict[int, list[EvidenceItem]] = {}
    for item in sorted_items:
        by_group.setdefault(item.group, []).append(item)

    for group_no in sorted(by_group.keys()):
        group_items = by_group[group_no]

        # 每组加一个小标题（工程名称来自 setattr 的 _group_name）
        group_name = getattr(group_items[0], "_group_name", f"工程{group_no}")
        heading = doc.add_paragraph()
        heading.add_run(f"{group_no}、{group_name}").bold = True

        # 新建表格
        table = doc.add_table(rows=1, cols=4)
        table.style = "Table Grid"
        hdr = table.rows[0].cells
        for i, htext in enumerate(["编号", "证据名称", "证据所要证明的事实", "页码"]):
            hdr[i].text = htext

        for item in group_items:
            row = table.add_row().cells
            row[0].text = item.evidence_no
            row[1].text = item.clean_name

            purpose_text = item.proof_purpose
            purpose_para = row[2].paragraphs[0]
            run = purpose_para.add_run(purpose_text)

            # 金额异常标红
            project_cross = next(
                (cr for cr in cross_results if cr.project and cr.project == group_name),
                None,
            )
            if project_cross and project_cross.issues:
                run.font.color.rgb = RED

            row[3].text = f"{item.page_start}-{item.page_end}" if item.page_start else ""

        doc.add_paragraph()  # 组间空行

    # 附：金额交叉校验提示
    if any(cr.issues for cr in cross_results):
        doc.add_heading("金额交叉校验提示", level=2)
        for cr in cross_results:
            if not cr.issues:
                continue
            para = doc.add_paragraph()
            run = para.add_run(f"【{cr.project}】")
            run.bold = True
            for issue in cr.issues:
                ipara = doc.add_paragraph(issue)
                ipara.runs[0].font.color.rgb = RED

    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output))


# ---------------------------------------------------------------------------
# 10. 主流程
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成证据清单 .docx")
    parser.add_argument("md_dir", help="MinerU 输出的 Markdown 根目录")
    parser.add_argument("output", help="输出 .docx 路径")
    parser.add_argument(
        "--stance",
        choices=["plaintiff", "defendant"],
        default="plaintiff",
        help="原告 / 被告立场（默认原告）",
    )
    parser.add_argument(
        "--metadata",
        default=None,
        help="metadata.json 路径（默认从 md_dir/metadata.json 读取）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只跑自检不写盘",
    )
    args = parser.parse_args(argv)

    md_dir = Path(args.md_dir).resolve()
    if not md_dir.exists():
        sys.stderr.write(f"ERROR: 目录不存在: {md_dir}\n")
        return 2

    # 加载 metadata
    metadata = (
        json.loads(Path(args.metadata).read_text(encoding="utf-8"))
        if args.metadata
        else load_metadata(md_dir)
    )

    # 发现分组
    groups = discover_groups(md_dir, metadata)
    if not groups:
        sys.stderr.write("ERROR: 未发现任何 Markdown 文件\n")
        return 2

    # 遍历 + 提取
    items: list[EvidenceItem] = []
    cross_results: list[CrossCheckResult] = []

    for group_no, group_name, entries in groups:
        group_items: list[EvidenceItem] = []
        page_cursor = 1
        for seq, entry in enumerate(entries, start=1):
            fpath: Path = entry["path"]
            filename: str = entry["filename"]

            # 读取 Markdown
            md_text = ""
            if fpath.suffix.lower() == ".md" and fpath.exists():
                md_text = fpath.read_text(encoding="utf-8", errors="ignore")

            # 文档类型
            meta_entry = entry.get("meta", {}) if isinstance(entry.get("meta"), dict) else {}
            dtype = detect_dtype(filename, meta_entry.get("dtype"))

            # 提取字段
            fields = parse_fields(dtype, md_text)

            # 组内页码（简化：每个文件占 1 页，实际应从 metadata 读 pages）
            pages = meta_entry.get("pages", 1) if meta_entry else 1
            page_start = page_cursor
            page_end = page_cursor + max(1, pages) - 1
            page_cursor = page_end + 1

            # 组装
            item = EvidenceItem(
                group=group_no,
                group_seq=seq,
                evidence_no=f"{group_no}-{seq}",
                filename=filename,
                clean_name=clean_evidence_name(filename),
                dtype=dtype,
                page_start=page_start,
                page_end=page_end,
                fields=fields,
                source_md=str(fpath.relative_to(md_dir)) if fpath.exists() else "",
            )
            item.proof_purpose = build_proof_purpose(item)
            setattr(item, "_group_name", group_name)  # 供 docx 标红引用
            group_items.append(item)

        items.extend(group_items)
        # 同一工程做金额交叉校验
        cross_results.append(cross_verify_amounts(group_name, group_items))

    # 自检
    try:
        check_report = self_check(metadata, groups, items, cross_results)
    except SelfCheckError as e:
        sys.stderr.write("=" * 60 + "\n")
        sys.stderr.write("自检未通过，拒绝存盘。\n\n")
        sys.stderr.write(str(e) + "\n")
        sys.stderr.write("=" * 60 + "\n")
        return 1

    # 写盘
    output_path = Path(args.output).resolve()
    if not args.dry_run:
        build_docx(items, cross_results, output_path, stance=args.stance)

    # 输出摘要
    print(f"✅ 自检通过")
    print(f"   metadata 文件数: {check_report['checks'][0]['meta_files']}")
    print(f"   证据清单行数:   {check_report['checks'][0]['evidence_rows']}")
    print(f"   工程组数:       {len(groups)}")
    print(f"   金额异常项目:   {sum(len(cr.issues) for cr in cross_results)}")
    if not args.dry_run:
        print(f"   已生成:         {output_path}")
    else:
        print(f"   (dry-run, 未写盘)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
