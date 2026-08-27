"""Minimal Telegram Bot API client (spec §7).

Webhook mode needs sendMessage, editMessageText, answerCallbackQuery and
sendVoice, so this is a thin httpx wrapper rather than a bot framework — one
less event loop to reconcile with FastAPI's.
"""

import logging

import httpx

log = logging.getLogger("vocab.tg")

API = "https://api.telegram.org"


class TelegramError(RuntimeError):
    pass


def keyboard(rows: list[list[tuple[str, str]]]) -> dict:
    """Inline keyboard from rows of (label, callback_data).

    callback_data is capped at 64 bytes by Telegram, so callers pass short
    keys and keep the real payload server-side (R8).
    """
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row] for row in rows
        ]
    }


class Telegram:
    def __init__(self, token: str, client: httpx.AsyncClient | None = None, api: str = API):
        self.token = token
        self.api = api
        self._client = client
        self._owned = client is None

    async def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20)
        return self._client

    async def aclose(self):
        if self._client is not None and self._owned:
            await self._client.aclose()
            self._client = None

    async def call(self, method: str, **params):
        client = await self.client()
        response = await client.post(f"{self.api}/bot{self.token}/{method}", json=params)
        body = response.json()
        if not body.get("ok"):
            raise TelegramError(f"{method} failed: {body.get('description')}")
        return body.get("result")

    # -- the handful of methods the bot actually uses --------------------

    async def send_message(self, chat_id, text, buttons=None, preview=False):
        params = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": not preview,
        }
        if buttons:
            params["reply_markup"] = keyboard(buttons)
        return await self.call("sendMessage", **params)

    async def edit_message(self, chat_id, message_id, text, buttons=None):
        params = {"chat_id": chat_id, "message_id": message_id, "text": text}
        params["reply_markup"] = keyboard(buttons) if buttons else {"inline_keyboard": []}
        return await self.call("editMessageText", **params)

    async def answer_callback(self, callback_id, text=None):
        params = {"callback_query_id": callback_id}
        if text:
            params["text"] = text
        return await self.call("answerCallbackQuery", **params)

    async def send_voice(self, chat_id, audio: bytes, filename: str, caption: str = ""):
        client = await self.client()
        response = await client.post(
            f"{self.api}/bot{self.token}/sendVoice",
            data={"chat_id": str(chat_id), "caption": caption},
            files={"voice": (filename, audio, "audio/mpeg")},
        )
        body = response.json()
        if not body.get("ok"):
            raise TelegramError(f"sendVoice failed: {body.get('description')}")
        return body.get("result")

    async def set_webhook(self, url: str, secret: str):
        return await self.call(
            "setWebhook",
            url=url,
            secret_token=secret,
            allowed_updates=["message", "callback_query"],
        )

    async def delete_webhook(self):
        return await self.call("deleteWebhook")
