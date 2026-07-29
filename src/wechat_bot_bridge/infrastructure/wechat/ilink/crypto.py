"""微信 CDN 使用的 AES-128-ECB 与密钥编码工具。"""

from __future__ import annotations

import base64
import binascii
import re

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from .errors import ILinkMediaError

_HEX_KEY = re.compile(r"^[0-9a-fA-F]{32}$")


def encrypted_size(plaintext_size: int) -> int:
    """计算 PKCS#7 填充后的 AES 密文长度。"""

    if plaintext_size < 0:
        raise ValueError("plaintext_size must not be negative")
    return ((plaintext_size + 1 + 15) // 16) * 16


def encrypt_aes_ecb(plaintext: bytes, key: bytes) -> bytes:
    """使用 AES-128-ECB 与 PKCS#7 填充加密媒体。"""

    if len(key) != 16:
        raise ILinkMediaError("AES key must contain exactly 16 bytes")
    padder = PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def decrypt_aes_ecb(ciphertext: bytes, key: bytes) -> bytes:
    """解密微信 CDN 的 AES-128-ECB 媒体。"""

    if len(key) != 16:
        raise ILinkMediaError("AES key must contain exactly 16 bytes")
    if not ciphertext or len(ciphertext) % 16 != 0:
        raise ILinkMediaError("Encrypted media length is invalid")
    try:
        decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()
    except ValueError as exc:
        raise ILinkMediaError("Encrypted media padding is invalid") from exc


def decode_wire_aes_key(encoded: str) -> bytes:
    """兼容协议中出现的三种 AES key 编码。

    微信历史消息中同时存在直接十六进制、Base64(raw key) 和
    Base64(hex string) 三种表示形式，解析层必须全部兼容。
    """

    if _HEX_KEY.fullmatch(encoded):
        return bytes.fromhex(encoded)
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ILinkMediaError("Media AES key is not valid Base64") from exc
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32:
        try:
            hexadecimal = decoded.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ILinkMediaError("Media AES key has an invalid encoding") from exc
        if _HEX_KEY.fullmatch(hexadecimal):
            return bytes.fromhex(hexadecimal)
    raise ILinkMediaError("Media AES key has an invalid length")


def encode_wire_aes_key(key: bytes) -> str:
    """按腾讯当前客户端使用的 Base64(hex string) 形式编码密钥。"""

    if len(key) != 16:
        raise ILinkMediaError("AES key must contain exactly 16 bytes")
    return base64.b64encode(key.hex().encode("ascii")).decode("ascii")
