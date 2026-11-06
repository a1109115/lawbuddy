"""敏感配置的本地加密（DEV-FND-007）。

- 密钥环与包装原语统一由 `keyring` 提供（Windows DPAPI / macOS Keychain / Linux Secret Service）；
- 本模块只负责 settings 等"值级"敏感字段的包装，格式为 `enc:v1:<base64>`；
- 非密钥环平台 **fail-closed**：写入抛 KeyringError，不再静默降级为明文
  （技术方案 §14 第 4 条）；读取历史明文值仍兼容。
"""

from __future__ import annotations

from .keyring import PREFIX, KeyringError, is_wrapped, unwrap_bytes, wrap_bytes

__all__ = ["PREFIX", "KeyringError", "protect", "unprotect", "is_wrapped"]


def protect(plaintext: str) -> str:
    """加密为带前缀的可存储值。无可用密钥环时抛 KeyringError。"""
    return wrap_bytes(plaintext.encode("utf-8"))


def unprotect(stored: str) -> str:
    """解密；非本格式的历史明文值原样返回。"""
    return unwrap_bytes(stored).decode("utf-8")
