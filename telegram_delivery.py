"""Telegram-specific delivery policy, isolated from archive business logic."""

from typing import Any, Protocol


class TelegramCaller(Protocol):
    def call(self, method: str, **parameters: Any) -> Any: ...


class TelegramDeliveryService:
    def __init__(self, api: TelegramCaller, mode: str = "copy") -> None:
        if mode not in {"copy", "forward"}:
            raise ValueError("Delivery mode must be 'copy' or 'forward'.")
        self.api = api
        self.mode = mode

    def deliver(
        self,
        destination_chat_id: int,
        source_chat_id: int,
        source_message_id: int,
    ) -> Any:
        method = "copyMessage" if self.mode == "copy" else "forwardMessage"
        parameters: dict[str, int] = {
            "chat_id": destination_chat_id,
            "from_chat_id": source_chat_id,
            "message_id": source_message_id,
        }
        return self.api.call(method, **parameters)
