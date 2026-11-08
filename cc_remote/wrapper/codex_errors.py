"""Typed control-plane failures with actionable, credential-free presentation."""
from typing import Any

PROVIDER_MISMATCH_MESSAGE = (
    "当前模型服务商与此会话保存的服务商不同，本次未发送。"
    "历史仍可查看；请切回原服务商继续，或使用分支功能在当前服务商下创建新会话。"
)


def model_provider_mismatch(code: Any, data: Any) -> bool:
    return code == -32600 and isinstance(data, dict) and data.get("type") == "modelProviderMismatch"
