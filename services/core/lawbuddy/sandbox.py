"""Agent 沙箱：工作空间文件工具与受审批的 Shell 命令执行。

用户显式开启的可执行扩展通道（放开技术方案 §7 的默认拒绝，改为人审闸门）：
- 文件工具（read/write/edit/glob/grep）解析后的绝对路径必须位于当前工作空间
  根目录之内（resolve + relative 校验，防路径逃逸），且拒碰库文件/密钥环/blobs；
- bash 每次执行前必须经 policy 审批（SHELL_EXEC 能力，模型不能自我批准），
  授权范围由用户决定（允许一次 / 本任务 / 本案件），请求与决定均入审计；
- 所有输出统一截断，防止超长回灌撑爆模型上下文。
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

LINE_LIMIT = 2000          # read_file 单行截断（字符）
READ_MAX_LINES = 1000      # read_file 单次最多行数
GLOB_MAX_RESULTS = 200
GREP_MAX_RESULTS = 60
GREP_PER_FILE_CAP = 20
GREP_MAX_FILE_BYTES = 2_000_000   # 跳过 >2MB 的文件
BASH_TIMEOUT_DEFAULT = 30
BASH_TIMEOUT_MAX = 120
OUTPUT_LIMIT = 8000        # bash stdout/stderr 回灌截断（字符）
BASH_READ_CAP = 1_000_000  # bash 输出读取软上限（字符），超限丢弃后续内容防内存膨胀
HTML_MAX_BYTES = 200_000   # render_html 单次渲染上限（约 200KB）


# ---------------------------------------------------------------------------
# 路径安全
# ---------------------------------------------------------------------------

def workspace_root() -> Path:
    """当前工作空间根目录（库文件所在目录）。"""
    from .storage import get_db

    return get_db().path.parent.resolve()


def _protected_parts() -> set[str]:
    from .workspace import BLOB_DIRNAME, DB_FILENAME

    return {DB_FILENAME, f"{DB_FILENAME}-wal", f"{DB_FILENAME}-shm", BLOB_DIRNAME, ".lawbuddy", ".lawbuddy-temp"}


def _safe_path(root: Path, raw: Any) -> Path:
    """把用户/模型给的路径解析为 root 内的绝对路径，越界即拒绝。"""
    text = str(raw or "").strip().strip('"')
    if not text or text == ".":
        return root
    p = Path(text)
    resolved = (p if p.is_absolute() else root / p).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise ValueError(f"路径越界：只允许访问本地工作空间目录内的文件（{text}）")
    return resolved


def _guard_writable(path: Path) -> None:
    """写操作保护：拒碰库文件、密钥环与 blobs（防止经文件工具破坏加密库）。"""
    protected = _protected_parts()
    if path.name in protected:
        raise ValueError(f"受保护路径，禁止写入：{path.name}")
    for part in path.parts:
        if part in (".lawbuddy", ".lawbuddy-temp"):
            raise ValueError("受保护目录，禁止写入")
    from .workspace import BLOB_DIRNAME

    if BLOB_DIRNAME in path.parts:
        raise ValueError("受保护目录，禁止写入（blobs）")


def _is_text_file(path: Path, head: bytes) -> bool:
    return b"\x00" not in head


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"…（截断，共 {len(text)} 字符）"


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# 文件工具
# ---------------------------------------------------------------------------

def read_file(root: Path, raw_path: Any, offset: int = 1, limit: int = 500) -> dict[str, Any]:
    path = _safe_path(root, raw_path)
    if not path.is_file():
        raise ValueError(f"文件不存在：{raw_path}")
    data = path.read_bytes()
    if not _is_text_file(path, data[:4096]):
        raise ValueError(f"二进制文件，无法以文本读取：{raw_path}")
    lines = data.decode("utf-8", errors="replace").splitlines()
    total = len(lines)
    offset = max(1, int(offset or 1))
    limit = max(1, min(READ_MAX_LINES, int(limit or 500)))
    chunk = lines[offset - 1: offset - 1 + limit]
    body = "\n".join(f"{offset + i:>6}\t{_clip(line, LINE_LIMIT)}" for i, line in enumerate(chunk))
    return {
        "path": _rel(root, path),
        "totalLines": total,
        "offset": offset,
        "returned": len(chunk),
        "content": body,
        "note": "行号格式为「行号→内容」；文件较长时可用 offset/limit 分批读取",
    }


def write_file(root: Path, raw_path: Any, content: Any) -> dict[str, Any]:
    path = _safe_path(root, raw_path)
    _guard_writable(path)
    text = content if isinstance(content, str) else str(content or "")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    path.write_bytes(data)
    return {
        "written": True,
        "path": _rel(root, path),
        "bytes": len(data),
        "lines": len(text.splitlines()),
        "note": "已写入（覆盖式）。如需精确修改已有文件请优先用 edit_file",
    }


def edit_file(root: Path, raw_path: Any, old_string: Any, new_string: Any, replace_all: bool = False) -> dict[str, Any]:
    old = str(old_string or "")
    new = str(new_string or "")
    if not old:
        raise ValueError("old_string 不能为空")
    path = _safe_path(root, raw_path)
    _guard_writable(path)
    if not path.is_file():
        raise ValueError(f"文件不存在：{raw_path}")
    data = path.read_bytes()
    if not _is_text_file(path, data[:4096]):
        raise ValueError(f"二进制文件，无法编辑：{raw_path}")
    text = data.decode("utf-8", errors="replace")
    count = text.count(old)
    if count == 0:
        raise ValueError("old_string 未在文件中找到（需与文件内容逐字符一致，含缩进与换行）")
    if count > 1 and not replace_all:
        raise ValueError(f"old_string 出现 {count} 次，不唯一：请扩大上下文使其唯一，或设 replace_all=true 全部替换")
    new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    path.write_bytes(new_text.encode("utf-8"))
    return {
        "edited": True,
        "path": _rel(root, path),
        "replacements": count if replace_all else 1,
        "lines": len(new_text.splitlines()),
    }


def glob_files(root: Path, pattern: Any, base: Any = ".") -> dict[str, Any]:
    pat = str(pattern or "").strip() or "**/*"
    start = _safe_path(root, base)
    if not start.exists():
        raise ValueError(f"目录不存在：{base}")
    protected = _protected_parts()
    hits: list[Path] = []
    for p in start.glob(pat):
        if p.name in protected or any(part in (".lawbuddy", ".lawbuddy-temp") for part in p.parts):
            continue
        hits.append(p)
        if len(hits) >= GLOB_MAX_RESULTS * 2:
            break
    hits.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    entries = [
        {"path": _rel(root, p) + ("/" if p.is_dir() else ""), "type": "dir" if p.is_dir() else "file"}
        for p in hits[:GLOB_MAX_RESULTS]
    ]
    return {
        "pattern": pat,
        "base": _rel(root, start),
        "count": len(entries),
        "truncated": len(hits) > GLOB_MAX_RESULTS,
        "entries": entries,
    }


def grep_search(
    root: Path,
    pattern: Any,
    base: Any = ".",
    file_glob: Any = None,
    ignore_case: bool = False,
    max_results: int = GREP_MAX_RESULTS,
) -> dict[str, Any]:
    text_pattern = str(pattern or "").strip()
    if not text_pattern:
        raise ValueError("pattern 不能为空")
    flags = re.IGNORECASE if ignore_case else 0
    try:
        rx = re.compile(text_pattern, flags)
    except re.error as exc:
        raise ValueError(f"正则表达式无效：{exc}")
    start = _safe_path(root, base)
    if not start.exists():
        raise ValueError(f"目录不存在：{base}")
    glob_pat = str(file_glob or "").strip() or "**/*"
    protected = _protected_parts()
    matches: list[dict[str, Any]] = []
    truncated = False
    for p in start.glob(glob_pat):
        if not p.is_file() or p.name in protected:
            continue
        if any(part in (".lawbuddy", ".lawbuddy-temp") for part in p.parts):
            continue
        try:
            if p.stat().st_size > GREP_MAX_FILE_BYTES:
                continue
            data = p.read_bytes()
        except OSError:
            continue
        if not _is_text_file(p, data[:4096]):
            continue
        per_file = 0
        for line_no, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), start=1):
            if rx.search(line):
                matches.append({"file": _rel(root, p), "line": line_no, "text": _clip(line.strip(), 240)})
                per_file += 1
                if per_file >= GREP_PER_FILE_CAP:
                    break
        if len(matches) >= max_results:
            truncated = True
            break
    matches = matches[:max_results]
    return {
        "pattern": text_pattern,
        "count": len(matches),
        "truncated": truncated,
        "matches": matches,
    }


# ---------------------------------------------------------------------------
# bash（受审批门控）
# ---------------------------------------------------------------------------

def _kill_process_tree(proc: subprocess.Popen) -> None:
    """强制终止整个进程树。

    只杀 shell 直接子进程会留下孙进程持有输出管道写端，导致读取侧永久阻塞
    （Windows shell=True 下的经典卡死）；必须连树强杀让管道关闭。
    """
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
            )
        else:
            import signal

            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.kill()  # 兜底：进程树终止失败时至少杀掉直接子进程
    except Exception:
        pass


def _bash_env() -> dict[str, str]:
    """bash 子进程环境：把 Core 自身的 Python 解释器目录放到 PATH 最前。

    requirements.txt 里的 python-docx / openpyxl / python-pptx 装在 Core 的 venv，
    而系统 PATH 上的 `python` 通常没有这些包；不修正 PATH 时模型写的导出脚本会
    ModuleNotFoundError。冻结打包后 sys.executable 是应用 exe，目录里没有解释器，
    此时保持原环境不变（不猜测）。
    """
    env = dict(os.environ)
    exe = Path(sys.executable)
    names = ("python.exe",) if os.name == "nt" else ("python", "python3")
    if any((exe.parent / name).exists() for name in names):
        env["PATH"] = str(exe.parent) + os.pathsep + env.get("PATH", "")
    return env


def bash_exec(
    root: Path,
    command: str,
    timeout_s: int = BASH_TIMEOUT_DEFAULT,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """在工作空间根目录执行 Shell 命令（Windows 走 cmd.exe /c）。

    - 执行期间以 0.2s 粒度同时检查超时与任务取消，超时/取消即强杀整个进程树；
    - 输出由独立线程读取：孙进程持有管道时也不会卡住轮询循环；
    - 超时不再抛异常，而是返回 timedOut 结构化结果，明确要求模型更换策略。
    """
    command = str(command or "").strip()
    if not command:
        raise ValueError("command 不能为空")
    timeout = max(5, min(BASH_TIMEOUT_MAX, int(timeout_s or BASH_TIMEOUT_DEFAULT)))

    popen_kwargs: dict[str, Any] = {}
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True  # POSIX：独立进程组，超时可整组强杀
    try:
        proc = subprocess.Popen(
            command,
            cwd=str(root),
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            env=_bash_env(),
            **popen_kwargs,
        )
    except Exception as exc:
        raise ValueError(f"命令启动失败：{exc}")

    def _drain(pipe: Any) -> tuple[str, int]:
        chunks: list[str] = []
        total = 0
        try:
            for line in iter(pipe.readline, ""):
                if total < BASH_READ_CAP:
                    remain = BASH_READ_CAP - total
                    chunks.append(line[:remain])  # 超长单行只保留上限内部分，不整行丢弃
                total += len(line)
                # 超过软上限后继续读但丢弃：写端不堵死，命令不会被误判为卡死
        finally:
            try:
                pipe.close()
            except Exception:
                pass
        return "".join(chunks), total

    out_buf: list[tuple[str, int]] = []
    err_buf: list[tuple[str, int]] = []
    t_out = threading.Thread(target=lambda: out_buf.append(_drain(proc.stdout)), daemon=True)
    t_err = threading.Thread(target=lambda: err_buf.append(_drain(proc.stderr)), daemon=True)
    t_out.start()
    t_err.start()

    cancelled = False
    timed_out = False
    deadline = time.monotonic() + timeout
    while proc.poll() is None:
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            _kill_process_tree(proc)
            break
        if time.monotonic() >= deadline:
            timed_out = True
            _kill_process_tree(proc)
            break
        time.sleep(0.2)

    try:
        proc.wait(timeout=5)
    except Exception:
        pass
    t_out.join(timeout=5)
    t_err.join(timeout=5)
    stdout_text = out_buf[0][0] if out_buf else ""
    stderr_text = err_buf[0][0] if err_buf else ""

    if cancelled:
        return {
            "cancelled": True,
            "timedOut": False,
            "cwd": str(root),
            "stdout": _clip(stdout_text, OUTPUT_LIMIT),
            "stderr": _clip(stderr_text, OUTPUT_LIMIT // 2),
            "message": "用户已停止任务，该命令及其全部子进程已被强制终止。",
        }
    if timed_out:
        return {
            "exitCode": proc.returncode,
            "timedOut": True,
            "cwd": str(root),
            "stdout": _clip(stdout_text, OUTPUT_LIMIT),
            "stderr": _clip(stderr_text, OUTPUT_LIMIT // 2),
            "message": (
                f"命令执行超时（{timeout}s），已强制终止整个进程树。"
                "不要原样重试同一命令、也不要只调大 timeout；请更换策略："
                "缩小处理范围、拆分为更小的步骤、改用 read_file/grep_search 等文件工具，"
                "或向用户说明该步骤无法完成。"
            ),
        }
    return {
        "exitCode": proc.returncode,
        "timedOut": False,
        "cwd": str(root),
        "stdout": _clip(stdout_text, OUTPUT_LIMIT),
        "stderr": _clip(stderr_text, OUTPUT_LIMIT // 2),
    }


def command_hash(command: str) -> str:
    return hashlib.sha256(str(command).encode("utf-8")).hexdigest()


def render_html(state: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """render_html：把 HTML 文档交给前端渲染为用户可见的预览卡片。

    完整 HTML 不在本工具结果里回灌（防撑爆上下文），而是随 tool_use_start
    事件的 arguments 通道原样到达前端；这里只做校验 + 审计 + 小结果返回。
    """
    html = str(args.get("html") or "")
    if not html.strip():
        raise ValueError("html 不能为空")
    size = len(html.encode("utf-8"))
    if size > HTML_MAX_BYTES:
        raise ValueError(
            f"HTML 过大（{size} 字节，上限 {HTML_MAX_BYTES}）；请精简内容或拆分为多次渲染"
        )
    task_id = str(state.get("task_id") or "")
    case_id = str(state.get("case_id") or "")
    _audit_html(task_id, case_id, size, str(args.get("title") or ""))
    return {
        "rendered": True,
        "chars": len(html),
        "note": "已在正文预览区渲染预览卡片（自动出现在回答下方，用户可见）；如需修改请再次调用 render_html",
    }


def render_pptx(state: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """render_pptx：把每页一组原生元素（文本框/矩形/线条/表格）打包为可编辑 .pptx。

    每页都是 PowerPoint 原生 shapes（不是图片），用户可直接编辑；落盘工作空间
    根目录，任务收尾自动登记为产物卡片。页面文本不回灌、不入审计（只记页数与字节数）。
    """
    import re
    from datetime import datetime

    from . import slides as slides_mod

    slides = slides_mod.validate_slides(args.get("slides"))
    title = str(args.get("title") or "").strip()
    file_name = str(args.get("fileName") or "").strip()
    if file_name:
        file_name = re.sub(r'[\\/:*?"<>|]+', "_", file_name)
        if file_name.lower().endswith(".pptx"):
            file_name = file_name[:-5]
        file_name = file_name.strip(". ") or ""
    if not file_name:
        file_name = f"PPT-{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    root = workspace_root()
    dest = _safe_path(root, f"{file_name}.pptx")
    _guard_writable(dest)
    result = slides_mod.pages_to_pptx(slides, dest)

    task_id = str(state.get("task_id") or "")
    case_id = str(state.get("case_id") or "")
    _audit_pptx(task_id, case_id, result["slides"], result["bytes"], title or file_name)
    return {
        "written": True,
        "fileName": dest.name,
        "slides": result["slides"],
        "bytes": result["bytes"],
        "note": (
            f"已生成可编辑的 PPTX 文件（{result['slides']} 页，每页原生文本框/形状/表格，非图片），"
            "任务结束时自动登记为产物卡片；可在应用内预览或用 PowerPoint/WPS 打开编辑。"
            "如需修改请整体重新调用 render_pptx（覆盖同名文件）。"
        ),
    }


def _audit_html(task_id: str, case_id: str, size: int, title: str) -> None:
    try:
        from . import audit
        from .storage import get_db

        with get_db().transaction() as conn:
            audit.append(
                conn,
                action="html.render",
                resource_type="task",
                resource_id=task_id,
                case_id=case_id or None,
                metadata={"size": size, "title": _clip(title, 120)},
            )
    except Exception:
        pass  # 审计失败不阻断渲染结果返回


def _audit_pptx(task_id: str, case_id: str, slide_count: int, size: int, title: str) -> None:
    try:
        from . import audit
        from .storage import get_db

        with get_db().transaction() as conn:
            audit.append(
                conn,
                action="pptx.render",
                resource_type="task",
                resource_id=task_id,
                case_id=case_id or None,
                metadata={"slides": slide_count, "size": size, "title": _clip(title, 120)},
            )
    except Exception:
        pass  # 审计失败不阻断文件已生成的结果


def _audit_shell(task_id: str, case_id: str, command: str, result: dict[str, Any]) -> None:
    try:
        from . import audit
        from .storage import get_db

        with get_db().transaction() as conn:
            audit.append(
                conn,
                action="shell.exec",
                resource_type="task",
                resource_id=task_id,
                case_id=case_id or None,
                metadata={"command": _clip(command, 300), "exitCode": result.get("exitCode")},
            )
    except Exception:
        pass  # 审计失败不阻断命令结果返回


def execute_tool(state: dict[str, Any], name: str, args: dict[str, Any]) -> Any:
    """自主循环的沙箱工具入口（read/write/edit/glob/grep/bash）。"""
    root = workspace_root()
    if name == "read_file":
        return read_file(root, args.get("path"), int(args.get("offset") or 1), int(args.get("limit") or 500))
    if name == "write_file":
        return write_file(root, args.get("path"), args.get("content"))
    if name == "edit_file":
        return edit_file(root, args.get("path"), args.get("oldString"), args.get("newString"), bool(args.get("replaceAll")))
    if name == "glob_files":
        return glob_files(root, args.get("pattern"), args.get("path"))
    if name == "grep_search":
        return grep_search(root, args.get("pattern"), args.get("path"), args.get("glob"), bool(args.get("ignoreCase")))
    if name == "bash":
        return _bash_with_approval(state, root, args)
    if name == "render_html":
        return render_html(state, args)
    if name == "render_pptx":
        return render_pptx(state, args)
    raise ValueError(f"未知沙箱工具：{name}")


def _bash_with_approval(state: dict[str, Any], root: Path, args: dict[str, Any]) -> dict[str, Any]:
    """bash 审批门控：无有效授权时创建审批卡并阻塞等待用户决定。"""
    from . import policy

    command = str(args.get("command") or "").strip()
    if not command:
        raise ValueError("command 不能为空")
    task_id = str(state.get("task_id") or "")
    case_id = str(state.get("case_id") or "")
    payload_hash = command_hash(command)
    cancel_event = state.get("cancel_event")
    # 授权作用域（V32 会话隔离）：优先会话 conversation_id——「案件内允许」
    # 只在当前对话窗口内生效，重开新对话窗口后需重新审批
    grant_scope = str(state.get("conversation_id") or "") or case_id

    if not policy.has_grant(policy.CAP_SHELL_EXEC, task_id, grant_scope, payload_hash):
        approval = policy.create_approval(
            task_id=task_id,
            case_id=case_id,
            capability=policy.CAP_SHELL_EXEC,
            target=_clip(command, 300),
            payload_hash=payload_hash,
            data_summary=f"将在本地工作空间目录执行 Shell 命令（cwd={root}）",
            fallback="拒绝后将把拒绝结果告知模型，由其改用其他方案或向你说明",
            scope_id=grant_scope,
        )
        decision = policy.wait_for_decision(
            approval["approvalId"], task_id,
            cancel_check=(lambda: cancel_event is not None and cancel_event.is_set()),
        )
        if decision != "approved":
            return {
                "denied": True,
                "decision": decision,
                "message": "用户未批准该命令（拒绝或审批超时）。不要原样重试同一命令；请改用其他方案，或向用户说明需要该命令的原因。",
            }

    result = bash_exec(
        root, command,
        int(args.get("timeout") or BASH_TIMEOUT_DEFAULT),
        cancel_event=cancel_event,
    )
    if result.get("cancelled"):
        # 用户在命令执行期间点了停止：强杀后向上抛取消，让任务干净进入 CANCELLED
        from .agent import tasks as task_store

        raise task_store.TaskCancelled("用户停止")
    _audit_shell(task_id, case_id, command, result)
    return result
