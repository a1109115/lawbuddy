"""内容存储的文件级加密（技术方案 §14 第 2 条 / REQ-DOC-002 / AC-SEC-007）。

- 原文件、派生文件与缩略图以 AES-256-GCM 加密后落盘；
- 文件密钥由工作空间 DEK 经 HKDF-SHA256 派生（密钥分离：库密钥与文件密钥不复用）；
- 分块加密（默认 1MiB/块），避免大卷宗整份读入内存；每块独立 nonce 与 AAD，
  篡改任一块都会在解密时失败；
- 解密后的明文只允许存在于案件专属受限临时目录，由 `tempstore` 负责清理。

文件格式（大端）：

    magic(8) | version(1) | chunk_size(4) | nonce_base(8) | 块序列
    块 = 密文长度(4) | AESGCM 密文+tag
    第 i 块 nonce = nonce_base(8) || i(4)；AAD = magic || version || i(4)
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# 加密文件格式标识（含早期版本写入的历史密文）。这是二进制格式常量，不是品牌名，
# 改动会让既有加密文件无法解密，禁止随产品改名同步修改。
MAGIC = b"SLVYUE\x01\x00"
VERSION = 1
HEADER_LEN = len(MAGIC) + 1 + 4 + 8
DEFAULT_CHUNK = 1024 * 1024
_FILE_KEY_INFO = b"slvyue/file-content/v1"


class CryptoError(RuntimeError):
    """加解密失败：文件被篡改、密钥不匹配或格式非法。"""


def derive_file_key(dek: bytes) -> bytes:
    """由工作空间 DEK 派生文件内容密钥（与库密钥分离）。"""
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_FILE_KEY_INFO,
    ).derive(dek)


def is_encrypted(path: Path) -> bool:
    """判断文件是否为本模块加密格式。"""
    try:
        with path.open("rb") as f:
            return f.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def _aad(index: int) -> bytes:
    return MAGIC + bytes([VERSION]) + index.to_bytes(4, "big")


def _nonce(nonce_base: bytes, index: int) -> bytes:
    return nonce_base + index.to_bytes(4, "big")


def encrypt_file(src: Path, dst: Path, key: bytes, chunk_size: int = DEFAULT_CHUNK) -> int:
    """加密 src 到 dst，返回写入字节数。"""
    aes = AESGCM(key)
    nonce_base = os.urandom(8)
    written = 0
    with src.open("rb") as fin, dst.open("wb") as fout:
        header = MAGIC + bytes([VERSION]) + chunk_size.to_bytes(4, "big") + nonce_base
        fout.write(header)
        written += len(header)
        index = 0
        while True:
            block = fin.read(chunk_size)
            if not block:
                break
            ct = aes.encrypt(_nonce(nonce_base, index), block, _aad(index))
            fout.write(len(ct).to_bytes(4, "big"))
            fout.write(ct)
            written += 4 + len(ct)
            index += 1
    return written


def _parse_header(head: bytes) -> tuple[int, bytes]:
    if len(head) < HEADER_LEN or head[: len(MAGIC)] != MAGIC:
        raise CryptoError("不是加密内容存储格式")
    if head[len(MAGIC)] != VERSION:
        raise CryptoError(f"不支持的加密格式版本：{head[len(MAGIC)]}")
    off = len(MAGIC) + 1
    chunk_size = int.from_bytes(head[off:off + 4], "big")
    nonce_base = head[off + 4:off + 12]
    if chunk_size <= 0:
        raise CryptoError("加密头非法：chunk_size")
    if len(nonce_base) != 8:
        raise CryptoError("加密头非法：nonce_base")
    return chunk_size, nonce_base


def decrypt_file(src: Path, dst: Path, key: bytes) -> int:
    """解密 src 到 dst，返回写入字节数。"""
    aes = AESGCM(key)
    written = 0
    with src.open("rb") as fin:
        chunk_size, nonce_base = _parse_header(fin.read(HEADER_LEN))
        with dst.open("wb") as fout:
            index = 0
            while True:
                lenb = fin.read(4)
                if not lenb:
                    break
                if len(lenb) < 4:
                    raise CryptoError("加密内容被截断（块长度不完整）")
                size = int.from_bytes(lenb, "big")
                ct = fin.read(size)
                if len(ct) != size:
                    raise CryptoError("加密内容被截断（块数据不完整）")
                try:
                    plain = aes.decrypt(_nonce(nonce_base, index), ct, _aad(index))
                except Exception as exc:
                    raise CryptoError("解密失败：密钥不匹配或内容被篡改") from exc
                fout.write(plain)
                written += len(plain)
                index += 1
    return written


def encrypt_bytes(data: bytes, key: bytes, chunk_size: int = DEFAULT_CHUNK) -> bytes:
    """内存加密（工作副本等小体量派生内容）。"""
    aes = AESGCM(key)
    nonce_base = os.urandom(8)
    out = bytearray(MAGIC + bytes([VERSION]) + chunk_size.to_bytes(4, "big") + nonce_base)
    index = 0
    for off in range(0, len(data), chunk_size):
        ct = aes.encrypt(_nonce(nonce_base, index), data[off:off + chunk_size], _aad(index))
        out += len(ct).to_bytes(4, "big") + ct
        index += 1
    return bytes(out)


def decrypt_bytes(src: Path, key: bytes) -> bytes:
    """解密小文件到内存（仅供缩略图、OCR 单页等受控场景）。"""
    aes = AESGCM(key)
    with src.open("rb") as fin:
        chunk_size, nonce_base = _parse_header(fin.read(HEADER_LEN))
        out = bytearray()
        index = 0
        while True:
            lenb = fin.read(4)
            if not lenb:
                break
            if len(lenb) < 4:
                raise CryptoError("加密内容被截断（块长度不完整）")
            size = int.from_bytes(lenb, "big")
            ct = fin.read(size)
            if len(ct) != size:
                raise CryptoError("加密内容被截断（块数据不完整）")
            try:
                out.extend(aes.decrypt(_nonce(nonce_base, index), ct, _aad(index)))
            except Exception as exc:
                raise CryptoError("解密失败：密钥不匹配或内容被篡改") from exc
            index += 1
    return bytes(out)


def read_maybe_encrypted(path: Path, key: bytes | None) -> bytes:
    """读取内容存储文件：加密则解密，历史明文原样返回（便于增量迁移）。"""
    if not is_encrypted(path):
        return path.read_bytes()
    if key is None:
        raise CryptoError("内容存储为加密格式，但当前没有可用密钥")
    return decrypt_bytes(path, key)
