"""消息协议关联键的领域工具。"""

from __future__ import annotations

import hashlib


def scoped_protocol_item_id(scope: str, item_id: str | None) -> str | None:
    """把上游 item ID 变为账号隔离、不可逆的内部关联键。"""

    if item_id is None:
        return None
    return hashlib.sha256(f"{scope}\0{item_id}".encode()).hexdigest()
