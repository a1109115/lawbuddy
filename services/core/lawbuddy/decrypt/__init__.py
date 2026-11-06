"""加密文档解锁：检测、隔离解密 Worker 与明文重新加密入库。

技术方案 §2.2 / §11.2；需求 REQ-DOC-004 / 011 / 012 / 013。
"""

from .manager import DecryptError, detect_encryption, unlock_document

__all__ = ["DecryptError", "detect_encryption", "unlock_document"]
