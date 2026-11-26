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

# 原件区（WFA-LAW §4.4 / P1-06）：这些目录内是用户导入的原件与内部密钥材料，
# 只能经文档管线（document_search / document_get_pages）读取，不开放给文件工具
# 与 shell —— 否则模型在检索失效时会绕过分页、引用回查与 OCR 置信度直接读原文，
# 使"每条结论可回溯原文"的校验失去意义。写入侧另由 _guard_writable 兜底。
READONLY_ZONES = ("inputs",)
# 迁移期警告模式（§6 R2）：策略刚上线时，依赖 shell 的既有工作流可能被误拒。
# 开启后只记审计与告警、不阻断执行，给用户 2 周迁移窗口；默认关闭（硬拦截）。
# 仅对"读取"生效——写入原件区在任何模式下都拒绝（原件只读是产品硬约束）。
_READONLY_WARN_ONLY = os.environ.get("LAWBUDDY_READONLY_WARN_ONLY", "").strip().lower() in (
    "1", "true", "yes", "on",
)


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


def _rel_parts(root: Path, path: Path) -> tuple[str, ...]:
    try:
        return path.relative_to(root).parts
    except ValueError:
        return path.parts


def readonly_zone(root: Path, path: Path) -> str | None:
    """命中只读原件区时返回区域名（供拒绝提示与审计），否则返回 None。"""
    parts = _rel_parts(root, path)
    if not parts:
        return None
    head = parts[0]
    if head in READONLY_ZONES:
        return head
    # 内容存储与内部目录（含密钥材料）同样不可直接读取
    from .workspace import BLOB_DIRNAME

    if head in (BLOB_DIRNAME, ".lawbuddy", ".lawbuddy-temp"):
        return head
    return None


def _allow_read_paths(state: dict[str, Any] | None) -> list[str]:
    """任务级只读白名单（tool_policy.allowReadPaths，§4.4 第 2 条）。

    仅供确有需要的调试场景：白名单只放宽"读取"，写入仍受 _guard_writable 限制，
    且密钥材料（.lawbuddy 下的 dek.key 等）永不进入白名单。
    """
    policy = (state or {}).get("tool_policy") or {}
    raw = policy.get("allowReadPaths") or []
    if isinstance(raw, str):
        raw = [raw]
    out: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        text = str(item or "").strip().replace("\\", "/")
        while text.startswith("./"):
            text = text[2:]
        text = text.lstrip("/")
        if not text or ".." in text.split("/"):
            continue
        # 内部目录（密钥材料所在）永不放宽
        if text.split("/")[0] in (".lawbuddy", ".lawbuddy-temp"):
            continue
        out.append(text)
    return out


def _deny_readonly(root: Path, path: Path, state: dict[str, Any] | None, zone: str) -> bool | None:
    """原件区读取拒绝：给出可执行的替代路径，并把拦截事件写入审计。

    迁移期警告模式（LAWBUDDY_READONLY_WARN_ONLY=1）下改为记审计 + 返回告警文本，
    由调用方随结果带回给模型（不阻断执行）；默认硬拦截。
    """
    rel = _rel(root, path)
    allowed = _allow_read_paths(state)
    if any(rel == a or rel.startswith(a.rstrip("/") + "/") for a in allowed):
        return None
    _audit_blocked(state, "read", rel, zone)
    if _READONLY_WARN_ONLY:
        return True  # 调用方据此附加告警，不抛异常
    raise ValueError(
        f"「{rel}」属于只读原件区（{zone}），不能直接用文件工具或 Shell 读取。"
        "请改用 document_search 检索、document_get_pages 分页精读（结果带页码与 OCR 质量，"
        "可直接用于引用回查）；确需调试读取时由任务显式声明 toolPolicy.allowReadPaths。"
    )


def readonly_warning(rel: str, zone: str) -> str:
    """迁移期告警文案：明确"本次放行"与"后续将拒绝"的边界。"""
    return (
        f"⚠ 原件区读取已放行（迁移期警告模式）：{rel}（区域 {zone}）。"
        "正式策略下该读取会被拒绝，请改用 document_search / document_get_pages；"
        "直读原文会绕过分页、引用回查与 OCR 置信度校验。"
    )


def _audit_blocked(state: dict[str, Any] | None, action: str, rel: str, zone: str) -> None:
    """只读区拦截审计（仅路径与动作，不写文件内容；§4.4 第 3 条）。"""
    if not state:
        return
    try:
        from . import audit
        from .storage import get_db

        with get_db().transaction() as conn:
            audit.append(
                conn,
                action="sandbox.readonly_blocked",
                resource_type="task",
                resource_id=str(state.get("task_id") or ""),
                case_id=str(state.get("case_id") or "") or None,
                result="denied",
                metadata={"action": action, "path": _clip(rel, 200), "zone": zone},
            )
    except Exception:
        pass  # 审计失败不改变拦截结论（拒绝已经生效）


def _check_readable(root: Path, path: Path, state: dict[str, Any] | None) -> str | None:
    """只读区读取检查；返回迁移期告警文本（正常模式返回 None 或抛错）。"""
    zone = readonly_zone(root, path)
    if zone is None:
        return None
    allowed = _deny_readonly(root, path, state, zone)
    if allowed:
        return readonly_warning(_rel(root, path), zone)
    return None


def _guard_outside_readonly(root: Path, path: Path) -> None:
    """写操作禁止落在原件区：原件只读是产品硬约束（AGENTS.md），不是提示语。"""
    zone = readonly_zone(root, path)
    if zone is not None:
        _audit_blocked(None, "write", _rel(root, path), zone)
        raise ValueError(f"「{_rel(root, path)}」属于只读原件区（{zone}），禁止写入或修改")


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

def read_file(
    root: Path,
    raw_path: Any,
    offset: int = 1,
    limit: int = 500,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    path = _safe_path(root, raw_path)
    warning = _check_readable(root, path, state)
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
    result = {
        "path": _rel(root, path),
        "totalLines": total,
        "offset": offset,
        "returned": len(chunk),
        "content": body,
        "note": "行号格式为「行号→内容」；文件较长时可用 offset/limit 分批读取",
    }
    if warning:
        result["warning"] = warning
    return result


def write_file(root: Path, raw_path: Any, content: Any) -> dict[str, Any]:
    path = _safe_path(root, raw_path)
    _guard_writable(path)
    _guard_outside_readonly(root, path)
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
    _guard_outside_readonly(root, path)
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


def glob_files(
    root: Path, pattern: Any, base: Any = ".", state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pat = str(pattern or "").strip() or "**/*"
    start = _safe_path(root, base)
    _check_readable(root, start, state)
    if not start.exists():
        raise ValueError(f"目录不存在：{base}")
    protected = _protected_parts()
    hits: list[Path] = []
    for p in start.glob(pat):
        if p.name in protected or any(part in (".lawbuddy", ".lawbuddy-temp") for part in p.parts):
            continue
        # 原件区不参与列举：避免模型据此拼出原件路径绕过文档管线
        if readonly_zone(root, p.resolve()) is not None:
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
    state: dict[str, Any] | None = None,
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
    _check_readable(root, start, state)
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
        if readonly_zone(root, p.resolve()) is not None:
            continue  # 原件区不经文件工具检索（须走 document_search）
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


# 命令里的"疑似路径 token"切分：空白与常见 shell 分隔符都当边界，
# 重定向（>、<）、管道（|）、赋值（=）不必单独解析，切完就已分开。
_BASH_TOKEN_SPLIT_RE = re.compile(r"""[\s"'|&;<>()=,]+""")
# 环境变量展开与家目录简写无法在解析期求出真实路径，按 fail-closed 处理。
_BASH_EXPANSION_RE = re.compile(r"(%[A-Za-z_][A-Za-z0-9_()]*%|^~|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?)")


def _bash_escaped_paths(root: Path, command: str) -> list[str]:
    """找出命令中解析后落在工作空间之外的路径 token。

    bash 是自由文本命令，无法逐次校验实际打开的文件；文件工具已由 `_safe_path`
    限定在工作空间内，shell 若不设边界，模型可用 `..\\其他目录\\x.json` 或绝对
    路径直接读到工作空间外的材料（实测：读到评测 reference/answers.json 与
    verify.py，跨案读取同样可行），使"可回溯原文"与"跨案召回 0"失效。
    """
    escaped: list[str] = []
    for raw in _BASH_TOKEN_SPLIT_RE.split(command or ""):
        token = raw.strip().strip('"').strip("'")
        if not token or token in ("-", "."):
            continue
        # 纯短选项（/T、/F 等 taskkill 风格）不是路径
        if re.fullmatch(r"/[A-Za-z]{1,3}", token):
            continue
        # 既无分隔符也不是 `..` 的普通词（命令名、参数值）不是路径
        if ".." not in token and "\\" not in token and "/" not in token and token != "..":
            continue
        if _BASH_EXPANSION_RE.search(token):
            escaped.append(token)  # 无法解析出真实路径，按越界处理
            continue
        candidate = Path(token)
        resolved = candidate if candidate.is_absolute() else (root / candidate)
        try:
            resolved.resolve().relative_to(root)
        except (ValueError, OSError):
            escaped.append(token)
    return escaped


def _guard_bash_command(root: Path, command: str, state: dict[str, Any] | None) -> str | None:
    """Shell 命令不得触碰只读原件区，也不得越出工作空间（P1-06 §4.4）。

    bash 是自由文本命令，无法像文件工具那样在打开文件时逐次校验，因此在执行前
    做一次路径引用检查：命中原件区/内容存储/内部目录即拒绝，并提示改用文档管线；
    解析后落在工作空间外的路径同样拒绝（`..` 逃逸、绝对路径、环境变量展开）。
    宁可误拒（模型可改用 read_file 于工作区或 document_* 读原件），也不放开
    直读原件的旁路——那会让引用回查与 OCR 置信度校验形同虚设。

    迁移期警告模式下返回告警文本（放行），由 bash 结果带回；默认抛错拒绝。
    """
    if not command:
        return None
    escaped = _bash_escaped_paths(root, command)
    if escaped:
        audit_path = _clip(escaped[0], 200)
        _audit_blocked(state, "bash", audit_path, "outside")
        raise ValueError(
            f"该命令引用了工作空间之外的路径「{audit_path}」，已拒绝执行。"
            "Shell 只能在工作空间目录内读写；工作空间外的案件材料、其他案件与本机"
            "文件不得直接读取，请改用工作空间的相对路径，或经 document_search / "
            "document_get_pages 读取本任务材料。"
        )
    allowed = _allow_read_paths(state)
    normalized = command.replace("\\", "/")
    zones = list(READONLY_ZONES)
    from .workspace import BLOB_DIRNAME

    zones += [BLOB_DIRNAME, ".lawbuddy"]
    warnings: list[str] = []
    for zone in zones:
        # 匹配 `inputs/`、`cd inputs`、`"inputs/x.txt"`、`--file=inputs/...` 等引用
        pattern = re.compile(rf"(?<![\w.-]){re.escape(zone)}(?:/|\b)", re.IGNORECASE)
        for m in pattern.finditer(normalized):
            tail = normalized[m.start():]
            # 白名单按相对路径前缀放宽（仅读取用途）
            if any(tail.startswith(a) for a in allowed):
                continue
            audit_path = normalized[max(0, m.start() - 20): m.start() + 60]
            _audit_blocked(state, "bash", audit_path, zone)
            if _READONLY_WARN_ONLY:
                warnings.append(readonly_warning(audit_path, zone))
                continue
            raise ValueError(
                f"该命令引用了只读原件区「{zone}」，已拒绝执行。"
                "原件只能经 document_search / document_get_pages 读取（带页码与 OCR 质量，"
                "可直接用于引用回查）；请在命令中改用工作区路径，或改用文档工具获取原文。"
            )
    return "\n".join(warnings) if warnings else None


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


def _resume_after_approval(task_id: str) -> None:
    """审批门禁通过后把任务从 WAITING_APPROVAL 复位为 RUNNING。

    审批卡会把任务生命周期置为 WAITING_APPROVAL，但"通过门禁"不等于"用户刚刚
    批准过"——长效授权（allow_task/allow_case）会让后续调用整体跳过审批分支。
    因此复位必须发生在门禁之后、执行之前，且只在确实处于等待态时写库。
    """
    try:
        from .agent import tasks as task_store

        info = task_store.get_task(task_id)
        if (info or {}).get("lifecycle") == "WAITING_APPROVAL":
            task_store.update_lifecycle(task_id, "RUNNING", "shell")
    except Exception:  # noqa: BLE001
        # 状态复位是展示/取消路径的修正，失败不应阻断命令执行本身
        pass


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
        return read_file(
            root, args.get("path"), int(args.get("offset") or 1), int(args.get("limit") or 500), state,
        )
    if name == "write_file":
        return write_file(root, args.get("path"), args.get("content"))
    if name == "edit_file":
        return edit_file(root, args.get("path"), args.get("oldString"), args.get("newString"), bool(args.get("replaceAll")))
    if name == "glob_files":
        return glob_files(root, args.get("pattern"), args.get("path"), state)
    if name == "grep_search":
        return grep_search(
            root, args.get("pattern"), args.get("path"), args.get("glob"),
            bool(args.get("ignoreCase")), state=state,
        )
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
    # 只读原件区检查放在审批之前：被策略拒绝的命令不应先打扰用户审批
    warning = _guard_bash_command(root, command, state)
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

    # 审批等待期间任务被置为 WAITING_APPROVAL。这里必须放在"权限门禁之后、
    # 真正执行之前"：已有长效授权时上面的审批分支整体跳过，若只在批准分支里
    # 复位，任务会一直停在 WAITING_APPROVAL（UI 显示"等待审批"，
    # cancel() 也会走审批分支而非执行中分支）。只在实际处于该状态时写库，
    # 避免每次 bash 都多一次 UPDATE。
    task_id and _resume_after_approval(task_id)

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
    if warning:
        result["warning"] = warning
    return result
