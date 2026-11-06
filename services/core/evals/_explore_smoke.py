"""_run_case_explore 冒烟：正常路径 / 轮数用尽强制作答 / 取消传播 / 空问题。

monkeypatch model_gateway.chat_with_tools 与 _execute_tool，不触真实模型与 DB。
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lawbuddy.agent import autonomous
from lawbuddy.agent.model_gateway import ToolCall
from lawbuddy.agent.tasks import TaskCancelled
from lawbuddy.workspace import ensure_workspace

ensure_workspace(str(Path(tempfile.mkdtemp(prefix="lawbuddy-explore-")) / "ws"))

orig_chat = autonomous.model_gateway.chat_with_tools
orig_exec = autonomous._execute_tool

FAKE_RESULT = {"content": "", "toolCalls": [], "usage": {"promptTokens": 100, "completionTokens": 50}}


def fake_exec(state, name, args, call_id="", **_kwargs):
    return {"ok": True, "tool": name}


def run(script, question="调查保证金流向", cancel=None):
    """script: 每次模型调用的返回值序列（末尾不够时复用最后一个元素）"""
    state = {
        "case_id": "c1", "mode": "legal_review",
        "model_policy": {"modelId": "fake-model"},
        "_emit": lambda t, p: events.append((t, p)),
        "_usage_total": {"promptTokens": 0, "completionTokens": 0},
    }
    if cancel:
        state["_cancel_check"] = cancel
    calls = {"n": 0}
    events: list = []

    def fake_chat(messages, tools=None, model=None, **_kw):
        if calls["n"] == 0:
            system = messages[0]["content"]
            assert "反向或冲突材料" in system
            assert "逐字原文短引" in system and "检索覆盖与缺口" in system
            assert "单次检索无命中不得断言全案不存在" in system
        i = min(calls["n"], len(script) - 1)
        calls["n"] += 1
        # 断言子代理工具集只含检索工具、强制作答时 tools 为 None
        if tools is not None:
            names = {t["function"]["name"] for t in tools}
            assert names == {"documents_list", "document_search", "document_get_pages", "memory_search"}, names
        r = dict(FAKE_RESULT)
        r.update(script[i])
        return r

    autonomous.model_gateway.chat_with_tools = fake_chat
    autonomous._execute_tool = fake_exec
    try:
        result = autonomous._run_case_explore(state, question, "outer-1")
    finally:
        autonomous.model_gateway.chat_with_tools = orig_chat
        autonomous._execute_tool = orig_exec
    return result, state, events, calls["n"]


# ---- 场景 1：两轮检索后终答 ----
script = [
    {"toolCalls": [ToolCall("s1", "document_search", '{"query":"保证金"}')]},
    {"toolCalls": [ToolCall("g1", "document_get_pages", '{"documentId":"d1"}'),
                    ToolCall("g2", "document_get_pages", '{"documentId":"d1"}')]},
    {"toolCalls": [], "content": "汇总结论：保证金 380 万（《起诉状》第3页）"},
]
r, s, ev, n = run(script)
assert r["conclusion"].startswith("汇总结论"), r
assert r["reads"] == 1 and r["searches"] == 1 and r["rounds"] == 3, r
assert s["_usage_total"] == {"promptTokens": 300, "completionTokens": 150}, s["_usage_total"]
progress = [p for t, p in ev if t == "explore_progress"]
assert len(progress) == 3, progress  # 1 次检索 + 2 次读页
assert all(p["callId"] == "outer-1" for p in progress)
assert progress[-1] == {"callId": "outer-1", "reads": 1, "searches": 1}, progress[-1]
print("场景1 正常路径 OK:", r)

# ---- 场景 2：8 轮全部带工具调用 → 强制作答 ----
script = [{"toolCalls": [ToolCall(f"s{i}", "document_search", '{"query":"x"}')]} for i in range(8)]
script.append({"toolCalls": [], "content": "强制结论（分点、带来源）"})
r, s, ev, n = run(script)
assert n == 9, n  # 8 轮 + 1 次强制作答
assert r["rounds"] == 8 and r["conclusion"] == "强制结论（分点、带来源）", r
assert r["searches"] == 8, r
print("场景2 轮数用尽强制作答 OK:", r)

# ---- 场景 3：取消传播 ----
try:
    run([{"toolCalls": [ToolCall("s1", "document_search", "{}")]}], cancel=lambda: True)
    raise SystemExit("FAIL: cancel not propagated")
except TaskCancelled:
    print("场景3 取消传播 OK")

# ---- 场景 4：空问题 ----
r, _, _, _ = run([{"content": "x"}], question="")
assert "error" in r and "question" in r["error"], r
print("场景4 空问题防御 OK:", r)

print("ALL EXPLORE SMOKE PASSED")
