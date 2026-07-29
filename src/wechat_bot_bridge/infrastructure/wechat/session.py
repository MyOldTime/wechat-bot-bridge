"""微信授权会话标识工具。"""

from __future__ import annotations

import hashlib

from .ilink.models import Credentials


def credential_fingerprint(credentials: Credentials) -> str:
    """生成只用于本地状态隔离的授权指纹。

    token 参与摘要但不会进入返回值或日志；授权任一关键字段变化都会作废旧
    owner 与 cursor，避免把上一账号的会话上下文用于新连接。
    """

    material = "\0".join(
        (
            credentials.base_url,
            credentials.account_id,
            credentials.user_id,
            credentials.token,
        )
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()
