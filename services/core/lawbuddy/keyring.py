"""工作空间密钥管理（DEV-FND-007 / 技术方案 §14、§13.2；DEC-004）。

- 每个工作空间生成独立数据加密密钥 DEK（32 字节随机）；
- DEK 由系统密钥环的 KEK 包装后落盘（Windows DPAPI / macOS Keychain / Linux Secret Service）；
- **fail-closed**：平台无安全密钥环时拒绝创建或打开工作空间，不落明文密钥
  （技术方案 §14 第 4 条：不能启用的平台必须阻断，或经安全评审采用替代方案）；
- 密钥不进入数据库、日志、命令行、Crash Dump 与遥测（技术方案 §14 第 5 条）。

整库加密（DEC-004）下 DEK 同时是 SQLCipher 的库密钥，经 `sqlcipher_key_pragma`
以 raw key 形式注入，不走口令 KDF（避免 SQLCipher 各版本 KDF 迭代差异导致无法开库）。
"""

from __future__ import annotations

import base64
import os
import secrets
import sys
from pathlib import Path

# 存储包装值的统一前缀（与 security.protect 历史格式保持一致，兼容既有数据）
PREFIX = "enc:v1:"

DEK_BYTES = 32  # AES-256
KEYRING_DIRNAME = ".lawbuddy"
# 改名前的密钥目录：存在旧目录而新目录不存在时就地改名，避免旧工作空间密钥失效
LEGACY_KEYRING_DIRNAME = ".slvyue"
DEK_FILENAME = "dek.key"


class KeyringError(RuntimeError):
    """密钥环不可用或密钥操作失败。调用方必须中断流程，不得降级为明文。"""


# ---------------------------------------------------------------------------
# 平台密钥环探测
# ---------------------------------------------------------------------------

def platform_keyring() -> str | None:
    """返回当前平台可用的密钥环名称；无可用密钥环返回 None。"""
    if sys.platform == "win32":
        return "dpapi"
    if sys.platform == "darwin":
        return "keychain"
    if sys.platform.startswith("linux"):
        return "secret-service"
    return None


def require_keyring() -> str:
    """要求存在可用密钥环，否则 fail-closed。"""
    name = platform_keyring()
    if name is None:
        raise KeyringError(
            f"当前平台（{sys.platform}）没有受支持的系统密钥环，无法安全保存工作空间密钥；"
            "已按安全基线阻断工作空间初始化"
        )
    return name


# ---------------------------------------------------------------------------
# 平台级包装 / 解包（KEK 由系统密钥环持有）
# ---------------------------------------------------------------------------

def _dpapi(data: bytes, protect: bool) -> bytes:
    """Windows DPAPI（CryptProtectData / CryptUnprotectData），绑定当前用户账户。"""
    import ctypes
    import ctypes.wintypes as wt

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _blob_from_bytes(raw: bytes) -> _DATA_BLOB:
        buf = ctypes.create_string_buffer(raw, len(raw))
        return _DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    def _bytes_from_blob(blob: _DATA_BLOB) -> bytes:
        out = ctypes.string_at(blob.pbData, blob.cbData)
        ctypes.windll.kernel32.LocalFree(blob.pbData)
        return out

    in_blob = _blob_from_bytes(data)
    out_blob = _DATA_BLOB()
    func = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not func(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)):
        raise KeyringError("DPAPI 操作失败")
    return _bytes_from_blob(out_blob)


def _keychain(data: bytes, protect: bool) -> bytes:
    """macOS Keychain（security 命令行）。未在 Windows 环境验证，接入 macOS 时补测。"""
    raise KeyringError("macOS Keychain 尚未接入（REQ-PLAT-001 macOS 发布前补齐）")


def _secret_service(data: bytes, protect: bool) -> bytes:
    """Linux Secret Service。未接入，按 fail-closed 处理。"""
    raise KeyringError("Linux Secret Service 尚未接入（REQ-PLAT-002 依赖验证后确定范围）")


def _platform_call(data: bytes, protect: bool) -> bytes:
    name = require_keyring()
    if name == "dpapi":
        return _dpapi(data, protect)
    if name == "keychain":
        return _keychain(data, protect)
    return _secret_service(data, protect)


def wrap_bytes(data: bytes) -> str:
    """用系统密钥环包装字节串，返回带前缀的可存储值。"""
    return PREFIX + base64.b64encode(_platform_call(data, True)).decode("ascii")


def unwrap_bytes(stored: str) -> bytes:
    """解包；非本格式视为历史明文原样返回（仅用于既有 settings 兼容）。"""
    if not stored.startswith(PREFIX):
        return stored.encode("utf-8")
    return _platform_call(base64.b64decode(stored[len(PREFIX):]), False)


def is_wrapped(stored: str) -> bool:
    return stored.startswith(PREFIX)


# ---------------------------------------------------------------------------
# 工作空间 DEK
# ---------------------------------------------------------------------------

def dek_path(root: Path) -> Path:
    return root / KEYRING_DIRNAME / DEK_FILENAME


def migrate_legacy_keyring_dir(root: Path) -> None:
    """把旧版 `.slvyue/` 密钥目录改名为 `.lawbuddy/`（仅当新目录不存在）。"""
    legacy = root / LEGACY_KEYRING_DIRNAME
    current = root / KEYRING_DIRNAME
    if not legacy.is_dir() or current.exists():
        return
    try:
        os.replace(legacy, current)
    except OSError:
        # 改名失败（占位/权限）时保持原状：load_or_create_dek 会走新建流程并 fail-closed 提示
        return


def load_or_create_dek(root: Path) -> bytes:
    """读取工作空间 DEK；不存在则生成并包装落盘。

    fail-closed：平台无密钥环时抛 KeyringError，不写入明文密钥文件。
    """
    require_keyring()
    migrate_legacy_keyring_dir(root)
    path = dek_path(root)
    if path.exists():
        try:
            raw = unwrap_bytes(path.read_text(encoding="ascii").strip())
        except KeyringError:
            raise
        except Exception as exc:
            raise KeyringError(f"工作空间密钥无法解包：{type(exc).__name__}") from exc
        if len(raw) != DEK_BYTES:
            raise KeyringError("工作空间密钥长度非法")
        return raw

    dek = secrets.token_bytes(DEK_BYTES)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(wrap_bytes(dek), encoding="ascii")
    _restrict(tmp)
    os.replace(tmp, path)
    _restrict(path)
    return dek


def _restrict(path: Path) -> None:
    """收紧密钥文件权限（Windows 下为只读，POSIX 下为 0600）。"""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def sqlcipher_key_pragma(dek: bytes) -> str:
    """SQLCipher raw key 字面量：x'<64 hex>'，跳过口令 KDF。"""
    if len(dek) != DEK_BYTES:
        raise KeyringError("DEK 长度必须为 32 字节")
    return f"x'{dek.hex()}'"


# ---------------------------------------------------------------------------
# 进程内活动 DEK（工作空间初始化后全局复用，供文件级加密读取）
# ---------------------------------------------------------------------------

_active_dek: bytes | None = None


def set_active_dek(dek: bytes | None) -> None:
    global _active_dek
    _active_dek = dek


def get_active_dek() -> bytes:
    if _active_dek is None:
        raise KeyringError("工作空间尚未初始化，密钥不可用")
    return _active_dek
