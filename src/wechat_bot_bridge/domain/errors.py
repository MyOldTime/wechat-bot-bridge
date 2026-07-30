"""稳定的应用错误。

本模块中的错误信息经过控制，可以安全地通过 API 返回。
上游原始异常可能包含实现细节，只能保留在日志中。
"""


class BridgeError(Exception):
    """桥接服务可预期业务异常的基类。"""

    code = "bridge_error"


class OwnerNotBoundError(BridgeError):
    code = "owner_not_bound"


class MissingContextError(BridgeError):
    code = "interaction_required"


class InvalidCursorError(BridgeError):
    code = "invalid_cursor"


class GatewayNotReadyError(BridgeError):
    code = "wechat_not_ready"


class GatewayTemporaryError(BridgeError):
    code = "wechat_temporary_error"


class GatewayRejectedError(BridgeError):
    code = "wechat_rejected"
