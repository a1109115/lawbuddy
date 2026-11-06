# -*- coding: utf-8 -*-
"""浏览器交互冒烟（需求文档 12.2 第二层）。

用 Playwright Chromium 加载 renderer 构建产物（out/renderer），验证：
  1. 应用可加载，三模式页签（法律阅卷 / 合同审查 / 文书生成）默认选中阅卷
  2. 模式页签可切换且选中态正确
  3. 新建任务向导：阅卷模式必填校验、合同模式字段联动（合同类型/审查背景）、
     文书生成"点选即建"入口
  4. 浏览器调试模式降级：无 Electron 桥时创建案件被拒绝并给出明确 Toast，
     对话框保留已填内容（创建失败不丢失输入）

使用方式（仓库根目录）：
  python scripts/e2e_browser.py            # 需要 apps/desktop/out/renderer 构建产物
  python scripts/e2e_browser.py --url http://127.0.0.1:5173   # 连接 electron-vite dev server

依赖系统 Python 的 playwright（含 Chromium）。截图与失败信息写入 %TEMP%。
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RENDERER_DIST = REPO / "apps" / "desktop" / "out" / "renderer"
SHOT_DIR = Path(tempfile.gettempdir())

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    return ok


def serve_static(directory: Path, port: int) -> ThreadingHTTPServer:
    handler = partial(SimpleHTTPRequestHandler, directory=str(directory))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main() -> int:
    parser = argparse.ArgumentParser(description="LawBuddy 浏览器交互冒烟")
    parser.add_argument("--url", default="", help="renderer 页面地址；缺省时本地静态服务 out/renderer")
    parser.add_argument("--port", type=int, default=5180)
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("缺少 playwright：请用系统 Python（含 playwright + Chromium）运行", file=sys.stderr)
        return 2

    url = args.url
    httpd = None
    if not url:
        index = RENDERER_DIST / "index.html"
        if not index.is_file():
            print(f"未找到构建产物 {index}；请先在根目录执行 npm run build", file=sys.stderr)
            return 2
        httpd = serve_static(RENDERER_DIST, args.port)
        url = f"http://127.0.0.1:{args.port}/index.html"

    print(f"浏览器冒烟 → {url}")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            try:
                run(page, url)
            except Exception as exc:  # noqa: BLE001 — 冒烟脚本需要完整失败信息
                shot = SHOT_DIR / "lawbuddy-e2e-browser-fail.png"
                try:
                    page.screenshot(path=str(shot), full_page=True)
                    print(f"失败截图：{shot}")
                except Exception:
                    pass
                raise exc
            browser.close()
    finally:
        if httpd:
            httpd.shutdown()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n浏览器冒烟结果：{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


def run(page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded")

    # ---- 1. 应用加载与三模式页签 ----
    page.wait_for_selector(".mode-tabs", timeout=20000)
    check("应用加载（标题）", page.title() == "LawBuddy", page.title())

    tabs = page.locator(".mode-tabs [role='tab']")
    check("三模式页签渲染", tabs.count() == 3, f"got {tabs.count()}")
    sel = page.locator(".mode-tabs [role='tab'][aria-selected='true']")
    check("默认选中法律阅卷", sel.count() == 1 and "法律阅卷" in (sel.first.text_content() or ""),
          sel.first.text_content() if sel.count() else "none")

    # ---- 2. 模式切换 ----
    def click_tab(label: str) -> str:
        page.locator(f".mode-tabs [role='tab']", has_text=label).first.click()
        page.wait_for_timeout(150)
        sel = page.locator(".mode-tabs [role='tab'][aria-selected='true']")
        return sel.first.text_content() or ""

    check("切换到合同审查", "合同审查" in click_tab("合同审查"))
    check("切换到文书生成", "文书生成" in click_tab("文书生成"))
    check("切回法律阅卷", "法律阅卷" in click_tab("法律阅卷"))

    # ---- 3. 新建任务向导 ----
    page.click(".btn-newcase")
    dlg = page.locator("[role='dialog'][aria-label='新建任务']")
    dlg.wait_for(state="visible", timeout=5000)
    check("新建任务向导打开", dlg.is_visible())

    submit = dlg.locator(".btn-primary")
    check("阅卷模式：名称与客户未填时禁用创建", submit.is_disabled())

    dlg.locator("#f-title").fill("浏览器冒烟-阅卷任务")
    check("仅填名称仍禁用（客户必填）", submit.is_disabled())
    dlg.locator("#f-client").fill("冒烟客户")
    check("名称与客户补齐后可创建", submit.is_enabled())

    # 合同模式联动
    dlg.locator("[role='radio']", has_text="合同审查").click()
    page.wait_for_timeout(150)
    check("合同模式：合同类型字段出现", dlg.locator("#f-contract-type").count() == 1)
    check("合同模式：审查背景字段出现", dlg.locator("#f-background").count() == 1)
    check("合同模式：隐藏程序阶段", dlg.locator("#f-stage").count() == 0)
    check("合同模式下客户字段改为选填文案",
          "客户 / 我方代称" in (dlg.locator("label[for='f-client']").text_content() or ""))

    # 阅卷模式：程序阶段与身份字段
    dlg.locator("[role='radio']", has_text="法律阅卷").click()
    page.wait_for_timeout(150)
    check("阅卷模式：程序阶段字段恢复", dlg.locator("#f-stage").count() == 1)
    check("阅卷模式：客户字段为必填", "*" in (dlg.locator("label[for='f-client']").text_content() or ""))

    # ---- 4. 浏览器调试模式降级：创建被拒 + Toast + 表单保留 ----
    dlg.locator("#f-title").fill("浏览器冒烟-降级任务")
    dlg.locator("#f-client").fill("降级客户")
    submit.click()
    toast = page.locator(".toast", has_text="浏览器调试模式")
    toast.first.wait_for(state="visible", timeout=5000)
    check("无桥创建被拒并 Toast 提示", toast.count() >= 1)
    check("创建失败后向导保留（不丢输入）", dlg.is_visible()
          and dlg.locator("#f-title").input_value() == "浏览器冒烟-降级任务")

    # ws 模式点选即建：同样走 createCase → 浏览器降级拒绝
    dlg.locator("[role='radio']", has_text="文书生成").click()
    page.wait_for_timeout(300)
    check("文书生成点选即建入口触发（浏览器降级 Toast）",
          page.locator(".toast", has_text="浏览器调试模式").count() >= 1)

    # ---- 5. Esc 关闭向导 ----
    dlg.locator("[role='radio']", has_text="法律阅卷").click()
    page.keyboard.press("Escape")
    dlg.wait_for(state="hidden", timeout=3000)
    check("Esc 关闭新建任务向导", not dlg.is_visible())


if __name__ == "__main__":
    sys.exit(main())
