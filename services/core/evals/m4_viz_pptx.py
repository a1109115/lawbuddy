# -*- coding: utf-8 -*-
"""M4 评测器：可视化产物 PPTX 原生形状导出（P3.2）。

用法（services/core 目录下）：
    python -m evals.m4_viz_pptx

覆盖：
1. 时间线 / 人物关系图 / 流程图导出到 PPTX 使用原生形状（roundRect / ellipse / diamond）
   与连接线，而不是把图退化成表格；
2. 流程方向支持 TB / LR，虚线边写成 dash、连接线带箭头；
3. needs_human / pending 节点在文字上追加「（待核实）」，不只靠颜色；
4. 节点/边的 cite 写入演讲者备注；warnings 单独成页，不静默丢失；
5. export_artifact 对含 viz 的产物走原生形状渲染分支。

门槛：全部断言通过。
"""

from __future__ import annotations

import tempfile
from contextlib import contextmanager
from pathlib import Path


def _slide_text(slide) -> str:
    parts = []
    for shape in slide.shapes:
        if getattr(shape, "has_text_frame", False):
            parts.append(shape.text_frame.text)
        if getattr(shape, "has_table", False):
            parts.extend(cell.text for row in shape.table.rows for cell in row.cells)
    return "\n".join(parts)


def _slide_xml(slide) -> str:
    return slide.shapes._spTree.xml  # noqa: SLF001


def _find_slide(prs, title_part: str):
    for slide in prs.slides:
        if slide.shapes.title is not None and title_part in (slide.shapes.title.text or ""):
            return slide
    return None


def _build_viz() -> dict:
    return {
        "timeline": {
            "nodes": [
                {"date": "2026-01-05", "event": "签订合同", "actors": ["张三"],
                 "cite": {"label": "《购销合同》 · 第1页"}, "status": "supported"},
                {"date": "2026-03-01", "event": "逾期付款", "actors": ["李四"],
                 "cite": {"label": "《对账单》 · 第2页"}, "status": "needs_human"},
                {"date": "2026-04-10", "event": "发出催告", "actors": ["张三"], "status": "pending"},
            ]
        },
        "relationGraph": {
            "nodes": [
                {"name": "张三", "kind": "person", "role": "原告",
                 "cite": {"label": "《起诉状》 · 第1页"}},
                {"name": "李四", "kind": "person", "role": "被告", "status": "needs_human"},
                {"name": "甲公司", "kind": "org", "role": "担保人",
                 "cite": {"label": "《担保函》 · 第1页"}},
            ],
            "edges": [
                {"from": "张三", "to": "李四", "label": "买卖合同",
                 "cite": {"label": "《购销合同》 · 第3页"}},
                {"from": "甲公司", "to": "李四", "label": "担保", "status": "pending"},
            ],
        },
        "flows": [
            {
                "title": "证据流转", "direction": "TB",
                "nodes": [
                    {"id": "s", "label": "立案", "shape": "stadium"},
                    {"id": "a", "label": "提交证据", "shape": "rect"},
                    {"id": "d", "label": "证据充分？", "shape": "diamond", "status": "needs_human"},
                    {"id": "y", "label": "开庭", "shape": "round",
                     "cite": {"label": "《开庭通知》 · 第1页"}},
                    {"id": "n", "label": "补充材料", "shape": "round"},
                ],
                "edges": [
                    {"from": "s", "to": "a"},
                    {"from": "a", "to": "d"},
                    {"from": "d", "to": "y", "label": "是"},
                    {"from": "d", "to": "n", "label": "否", "dashed": True},
                ],
            },
            {
                "title": "横向流程", "direction": "LR",
                "nodes": [{"id": "1", "label": "收案"}, {"id": "2", "label": "归档"}],
                "edges": [{"from": "1", "to": "2"}],
            },
        ],
        "warnings": ["部分时间点缺少原文页码，需人工核实"],
    }


def main() -> int:
    from lawbuddy import artifacts, render

    viz = _build_viz()
    with tempfile.TemporaryDirectory(prefix="lawbuddy_viz_pptx_") as temp:
        root = Path(temp)
        path = root / "可视化.pptx"
        render.viz_to_pptx(viz, str(path), "案件可视化")
        assert path.exists() and path.stat().st_size > 0

        from pptx import Presentation

        prs = Presentation(str(path))
        # 封面 + warnings + 时间线 + 关系图 + 2 个流程
        assert len(prs.slides) == 6, len(prs.slides)

        # ---- 原生形状 / 连接线 ----
        shapes_total = 0
        connectors_total = 0
        for slide in prs.slides:
            for shape in slide.shapes:
                tag = shape._element.tag  # noqa: SLF001
                if tag.endswith("}cxnSp"):
                    connectors_total += 1
                elif tag.endswith("}sp"):
                    shapes_total += 1
        assert shapes_total >= 12, shapes_total
        assert connectors_total >= 10, connectors_total
        print(f"[PASS] 原生形状 {shapes_total} 个 / 连接线 {connectors_total} 条（未退化为表格）")

        all_xml = "\n".join(_slide_xml(slide) for slide in prs.slides)
        assert 'prst="diamond"' in all_xml, "判断节点未写成菱形"
        assert 'prst="ellipse"' in all_xml, "人物节点未写成椭圆"
        assert 'prst="roundRect"' in all_xml, "时间线/起止节点未写成圆角矩形"
        assert 'prstDash val="dash"' in all_xml, "虚线边未写成 dash"
        assert "tailEnd" in all_xml, "连接线缺少箭头"
        print("[PASS] 菱形/椭圆/圆角矩形、虚线边与箭头均为原生 DrawingML")

        # ---- 文字状态与说明 ----
        timeline = _find_slide(prs, "时间线")
        assert timeline is not None and "签订合同" in _slide_text(timeline)
        assert "（待核实）" in _slide_text(timeline), _slide_text(timeline)
        relation = _find_slide(prs, "人物关系图")
        assert relation is not None and "张三" in _slide_text(relation) and "甲公司" in _slide_text(relation)
        assert "（待核实）" in _slide_text(relation)
        flow = _find_slide(prs, "证据流转")
        assert flow is not None and "证据充分？" in _slide_text(flow) and "是" in _slide_text(flow)
        lr = _find_slide(prs, "横向流程")
        assert lr is not None and "收案" in _slide_text(lr)
        print("[PASS] 节点文字、待核实标识与 LR 流程均可见")

        # ---- 引证写入备注、warnings 单独成页 ----
        assert "《购销合同》 · 第1页" in timeline.notes_slide.notes_text_frame.text
        assert "《购销合同》 · 第3页" in relation.notes_slide.notes_text_frame.text
        assert "《开庭通知》 · 第1页" in flow.notes_slide.notes_text_frame.text
        warnings = _find_slide(prs, "待核实提示")
        assert warnings is not None and "缺少原文页码" in _slide_text(warnings)
        print("[PASS] 引证写入演讲者备注，warnings 单独成页")

        # ---- export_artifact 路由到 viz_to_pptx ----
        original_get_artifact = artifacts.get_artifact
        original_get_db = artifacts.get_db
        from lawbuddy import audit

        original_audit_append = audit.append

        class _FakeDB:
            @contextmanager
            def transaction(self):
                yield None

        try:
            artifacts.get_artifact = lambda _artifact_id: {
                "id": "art-viz", "caseId": "case-1", "kind": "viz",
                "title": "案件可视化", "body": {"viz": viz}, "versionNo": 1,
            }
            artifacts.get_db = lambda: _FakeDB()
            audit.append = lambda *args, **kwargs: None
            routed = root / "路由导出.pptx"
            result = artifacts.export_artifact("art-viz", "pptx", str(routed))
            assert result.get("ok") is True and routed.exists()
            routed_prs = Presentation(str(routed))
            assert len(routed_prs.slides) == 6, len(routed_prs.slides)
            assert 'prst="diamond"' in _slide_xml(_find_slide(routed_prs, "证据流转") or routed_prs.slides[0])
        finally:
            artifacts.get_artifact = original_get_artifact
            artifacts.get_db = original_get_db
            audit.append = original_audit_append
        print("[PASS] export_artifact 对 viz 产物走原生形状渲染")

    print("M4 可视化产物 PPTX 原生形状：PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
