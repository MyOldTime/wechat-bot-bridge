"""微信 CDN AES 编解码测试。"""

from __future__ import annotations

import base64

import pytest

from wechat_bot_bridge.infrastructure.wechat.ilink.crypto import (
    decode_wire_aes_key,
    decrypt_aes_ecb,
    encode_wire_aes_key,
    encrypt_aes_ecb,
    encrypted_size,
)
from wechat_bot_bridge.infrastructure.wechat.ilink.errors import ILinkMediaError


def test_aes_round_trip_and_size_calculation() -> None:
    key = bytes(range(16))
    plaintext = b"wechat-media"
    encrypted = encrypt_aes_ecb(plaintext, key)

    assert len(encrypted) == encrypted_size(len(plaintext))
    assert decrypt_aes_ecb(encrypted, key) == plaintext


def test_wire_key_decoder_supports_all_observed_encodings() -> None:
    key = bytes(range(16))

    assert decode_wire_aes_key(key.hex()) == key
    assert decode_wire_aes_key(base64.b64encode(key).decode()) == key
    assert decode_wire_aes_key(encode_wire_aes_key(key)) == key


def test_invalid_ciphertext_and_key_fail_closed() -> None:
    with pytest.raises(ILinkMediaError):
        decrypt_aes_ecb(b"not-a-block", bytes(range(16)))
    with pytest.raises(ILinkMediaError):
        decode_wire_aes_key("not-base64")
