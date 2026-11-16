"""领域错误类型。"""

from __future__ import annotations


class ServiceError(Exception):
    """业务校验失败：携带 HTTP 状态码与可直接展示的中文信息。"""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class StaleStateError(ServiceError):
    """页面身份状态已过期（乐观并发冲突）。"""

    def __init__(self, current_version: int):
        super().__init__(
            f"页面身份状态已过期：当前版本为 v{current_version}，请刷新页面后重试",
            status_code=409,
        )
        self.current_version = current_version
