"""加密文档解密 Worker（REQ-DOC-004/011/013 / 技术方案 §2.2、§11.2）。

独立子进程，职责单一：接收密码 → 解密 → 输出到指定路径。

安全约束：
- 密码只经 stdin 的受控内存通道传入，不出现在命令行、环境变量与日志（REQ-DOC-011）；
- Worker 不接触数据库、不联网、不持有长期密钥；
- 证书加密、IRM、DRM、未知算法与错误密码一律返回明确失败，绝不尝试绕过或破解（REQ-DOC-013）；
- 单次请求即退出，进程生命周期与一次解密绑定。

协议（stdin/stdout 换行分隔 JSON）：
    请求 {"blobPath": str, "outPath": str, "password": str, "kind": "pdf"|"ooxml"}
    响应 {"ok": bool, "error": str | None, "unsupported": bool}
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


class UnsupportedEncryption(RuntimeError):
    """证书加密 / IRM / DRM / 未知算法：明确失败，不尝试绕过（REQ-DOC-013）。"""


def _decrypt_pdf(blob: Path, out: Path, password: str) -> None:
    from pypdf import PdfReader, PdfWriter

    try:
        reader = PdfReader(str(blob))
    except Exception as exc:
        raise UnsupportedEncryption(f"PDF 无法解析：{type(exc).__name__}") from exc

    if not reader.is_encrypted:
        raise UnsupportedEncryption("PDF 未加密")

    try:
        result = reader.decrypt(password)
    except Exception as exc:
        raise UnsupportedEncryption(
            f"不支持的 PDF 加密算法或损坏的加密字典：{type(exc).__name__}"
        ) from exc
    if not result:
        raise ValueError("密码错误")

    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    with out.open("wb") as f:
        writer.write(f)


def _decrypt_ooxml(blob: Path, out: Path, password: str) -> None:
    import msoffcrypto

    try:
        office = msoffcrypto.OfficeFile(blob.open("rb"))
    except Exception as exc:
        raise UnsupportedEncryption(f"Office 加密容器无法解析：{type(exc).__name__}") from exc

    if not office.is_encrypted():
        raise UnsupportedEncryption("Office 文档未加密")

    try:
        office.load_key(password=password)
    except Exception as exc:
        # msoffcrypto 对错误密码抛 InvalidKeyError；其余为算法/容器不支持
        name = type(exc).__name__
        if "InvalidKey" in name or "Password" in name:
            raise ValueError("密码错误") from exc
        raise UnsupportedEncryption(f"不支持的 Office 加密方式：{name}") from exc

    try:
        with out.open("wb") as f:
            office.decrypt(f)
    except Exception as exc:
        name = type(exc).__name__
        if "InvalidKey" in name or "Password" in name:
            raise ValueError("密码错误") from exc
        raise UnsupportedEncryption(f"Office 解密失败：{name}") from exc


def handle(req: dict) -> dict:
    blob = Path(str(req.get("blobPath") or ""))
    out = Path(str(req.get("outPath") or ""))
    password = str(req.get("password") or "")
    kind = str(req.get("kind") or "")

    if not blob.is_file() or not out.parent.is_dir():
        return {"ok": False, "error": "输入或输出路径无效", "unsupported": False}

    try:
        if kind == "pdf":
            _decrypt_pdf(blob, out, password)
        elif kind == "ooxml":
            _decrypt_ooxml(blob, out, password)
        else:
            return {"ok": False, "error": f"不支持的文档类型：{kind}", "unsupported": True}
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "unsupported": False}
    except UnsupportedEncryption as exc:
        return {"ok": False, "error": str(exc), "unsupported": True}
    except Exception as exc:  # 兜底：任何异常都不向调用方泄漏内部细节
        return {"ok": False, "error": f"解密失败：{type(exc).__name__}", "unsupported": False}

    return {"ok": True, "error": None, "unsupported": False}


def main() -> int:
    line = sys.stdin.readline()
    try:
        req = json.loads(line) if line.strip() else {}
    except json.JSONDecodeError:
        req = {}
    result = handle(req)
    # ensure_ascii=True：纯 ASCII 输出，冻结模式标准流编码异常时协议不损坏
    sys.stdout.write(json.dumps(result, ensure_ascii=True) + "\n")
    sys.stdout.flush()
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
