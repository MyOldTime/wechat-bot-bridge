"""安全日志上下文格式测试。"""

from __future__ import annotations

import logging

from wechat_bot_bridge.core.logging import SafeContextFormatter


def test_safe_context_formatter_outputs_allowlisted_fields_only() -> None:
    record = logging.LogRecord(
        name="wechat_bot_bridge.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="测试日志",
        args=(),
        exc_info=None,
    )
    record.event_id = "event-1"
    record.message_type = "text"
    # 非白名单字段模拟高敏上下文，即使调用方误传也不能进入最终文本。
    record.context_token = "must-not-leak"
    record.message_text = "private-message"

    rendered = SafeContextFormatter("%(levelname)s %(message)s").format(record)

    assert rendered.startswith("INFO 测试日志 context=")
    assert '"event_id":"event-1"' in rendered
    assert '"message_type":"text"' in rendered
    assert "must-not-leak" not in rendered
    assert "private-message" not in rendered
