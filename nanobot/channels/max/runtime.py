"""MAX messenger channel implementation (Bot API: long polling or webhook).

API docs: https://dev.max.ru/docs-api. The Bot API lives on
``platform-api2.max.ru`` and its TLS chain is rooted in the Russian
Ministry of Digital Development ("Russian Trusted Root CA"), which is not
part of most public CA bundles. The root certificate ships with this
package (``ca/``) and is added to the SSL context at runtime so outbound
requests verify without touching the system trust store.

Two transports are supported:

* ``mode="polling"`` — ``GET /updates`` long polling (default, no public URL).
  MAX drops the payload of native voice messages on this transport.
* ``mode="webhook"`` — the channel runs a local aiohttp listener and
  registers it with ``POST /subscriptions``. MAX requires an HTTPS endpoint on
  port 443 with a CA-issued certificate, so the listener normally sits behind
  a reverse proxy (Caddy) on ``127.0.0.1``. Use this transport to receive
  native voice messages (голосовые) intact.
"""

from __future__ import annotations

import asyncio
import json
import re
import ssl
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from pydantic import Field

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.paths import get_data_dir, get_media_dir
from nanobot.config_base import Base
from nanobot.pairing import get_approved
from nanobot.security.network import validate_url_target

DEFAULT_API_BASE_URL = "https://platform-api2.max.ru"
# NewMessageBody.text is capped at 4000 characters.
MAX_TEXT_LENGTH = 4000
_UPDATES_PAGE_LIMIT = 100
_DEFAULT_UPDATE_TYPES = ("message_created", "bot_started")
_UPDATES_TYPES = ",".join(_DEFAULT_UPDATE_TYPES)
# MAX delivers each webhook event with up to 10 retries; acknowledge fast and
# process out of band so a slow LLM turn never causes redelivery.
_WEBHOOK_SECRET_HEADER = "X-Max-Bot-Api-Secret"
# Chat actions (`POST /chats/{chatId}/actions`) are the only feedback primitive
# MAX gives a bot besides messages themselves: no message editing, no streaming.
_ACTION_TYPING_ON = "typing_on"
_ACTION_MARK_SEEN = "mark_seen"
# A typing indicator has to be refreshed; also cap it so a stuck turn cannot
# keep the bot "typing" forever.
_TYPING_MAX_DURATION_S = 600.0
_MARKER_STATE_FILE = "max_channel_state.json"
_SEEN_MIDS_MAX = 512
_POLL_ERROR_BACKOFF_S = 5.0
_RATE_LIMIT_BACKOFF_S = 1.5
# Poll transport budget on top of the server-side long polling window.
_POLL_GRACE_S = 30.0
_BUNDLE_CA_FILE = Path(__file__).resolve().parent / "ca" / "russian-trusted-root-ca.crt"
# Audio attachment suffixes we are willing to store on disk.
_AUDIO_SUFFIXES = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".amr", ".flac"}
# `POST /uploads?type=` accepts exactly these four types (photo was renamed to
# image); WEBP is not in MAX's image list but is tried as image and falls back
# to a plain file on rejection.
_NATIVE_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".bmp", ".heic"}
_IMAGE_SUFFIXES = _NATIVE_IMAGE_SUFFIXES  # WEBP goes as a file (not in MAX's image list)
_VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm"}
_MEDIA_ATTACHMENT_TYPES = {"image", "video", "file", "sticker"}
_DEFAULT_SUFFIX_BY_LABEL = {"image": ".jpg", "video": ".mp4", "audio": ".mp3", "file": ".bin"}
# Platform upload caps per type (both size AND pixel/duration criteria apply on
# the MAX side; the byte cap is what we can check locally).
_MAX_UPLOAD_BYTES = {
    "image": 50 * 1024 * 1024,
    "video": 250 * 1024 * 1024,
    "audio": 256 * 1024 * 1024,
    "file": 4 * 1024 * 1024 * 1024,
}
# Images and videos may share one message (up to 12 total); file and audio
# attachments must be the only attachment in their message.
_MAX_ATTACHMENTS_PER_MESSAGE = 12
# MAX processes an uploaded file asynchronously; `attachment.not.ready` is
# answered until processing finishes. Back off and retry a few times.
_ATTACHMENT_READY_DELAYS = (2.0, 4.0, 8.0)
_UPLOAD_TIMEOUT_S = 600.0
_SAFE_FILENAME_RE = re.compile(r"[^\w.\- ()\u0400-\u04FF]", re.UNICODE)
_URL_SUFFIX_RE = re.compile(r"\.[a-z0-9]{1,5}$")
# Whisper-class providers reject uploads above ~25 MB; skip transcription beyond that.
_TRANSCRIBE_MAX_BYTES = 24 * 1024 * 1024
_DOWNLOAD_TIMEOUT_S = 120.0
# MAX long polling drops the payload of native voice messages / video notes
# (upstream bug: empty `message_created` without `message`, also missing from
# GET /messages). Tell the user at most once per interval.
_VOICE_NOTICE_INTERVAL_S = 300.0
_VOICE_NOTICE_TEXT = (
    "Голосовое не дошло: MAX не передаёт нативные голосовые сообщения ботам через "
    "long polling (известный баг платформы — апдейт приходит пустым). "
    "Отправьте аудио файлом-вложением, оно расшифруется."
)


class MaxConfig(Base):
    """MAX messenger channel configuration."""

    enabled: bool = False
    token: str = ""
    allow_from: list[str] = Field(default_factory=list)
    welcome_message: str = ""
    api_base_url: str = DEFAULT_API_BASE_URL
    mode: Literal["polling", "webhook"] = "polling"
    poll_timeout_seconds: int = Field(default=60, ge=0, le=90)
    text_format: Literal["", "markdown", "html"] = "markdown"
    # Webhook transport. ``webhook_url`` is the public HTTPS endpoint MAX calls
    # (port 443 only, no port in the URL); ``webhook_listen_*`` is the local
    # listener a reverse proxy forwards to.
    webhook_url: str = ""
    webhook_listen_host: str = "127.0.0.1"
    webhook_listen_port: int = Field(default=8090, ge=1, le=65535)
    webhook_path: str = "/webhook"
    webhook_secret: str = ""
    webhook_update_types: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_UPDATE_TYPES)
    )
    # Feedback: MAX shows the message as read and the bot as typing, which is
    # the only "accepted / working" signal available to a bot.
    mark_seen: bool = True
    typing_indicator: bool = True
    typing_interval_seconds: float = Field(default=4.0, gt=0, le=60)
    typing_max_seconds: float = Field(default=300.0, gt=0, le=3600)
    # Inbound attachment download cap in megabytes (0 disables the limit).
    download_max_mb: int = Field(default=100, ge=0, le=4096)


def chunk_text(text: str, limit: int = MAX_TEXT_LENGTH) -> list[str]:
    """Split *text* into API-sized chunks, preferring line boundaries."""
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    return chunks


def _is_audio_file_attachment(item: dict[str, Any]) -> bool:
    """True for `file` attachments that are actually audio (dictaphone uploads)."""
    if str(item.get("type") or "").lower() != "file":
        return False
    payload = item.get("payload") or {}
    names = (
        payload.get("filename"),
        item.get("filename"),
        payload.get("url"),
    )
    for name in names:
        if not isinstance(name, str):
            continue
        suffix = Path(name.split("?")[0]).suffix.lower()
        if suffix in _AUDIO_SUFFIXES:
            return True
    return False


def _audio_attachments(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the audio attachments of a message body.

    Native voice messages arrive as `audio`; audio sent as a document
    (dictaphone recordings) arrives as `file` with an audio filename.
    """
    attachments = body.get("attachments") or []
    if not isinstance(attachments, list):
        return []
    return [
        item
        for item in attachments
        if isinstance(item, dict)
        and (
            str(item.get("type") or "").lower() == "audio"
            or _is_audio_file_attachment(item)
        )
    ]


def _media_attachments(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Image/video/file/sticker attachments (audio is handled separately)."""
    attachments = body.get("attachments") or []
    if not isinstance(attachments, list):
        return []
    return [
        item
        for item in attachments
        if isinstance(item, dict)
        and str(item.get("type") or "").lower() in _MEDIA_ATTACHMENT_TYPES
        and not _is_audio_file_attachment(item)
    ]


def _sanitize_filename(name: str) -> str:
    """Basename without traversal, capped to a sane length (may return "")."""
    name = Path(str(name).replace("\\", "/")).name.strip()
    name = _SAFE_FILENAME_RE.sub("_", name)[:80].strip(" .")
    return name or ""


def _classify_media(name: str) -> str:
    """Map a filename to a MAX upload type (image/video/audio/file)."""
    suffix = Path(name.split("?")[0]).suffix.lower()
    if suffix in _IMAGE_SUFFIXES:
        return "image"
    if suffix in _VIDEO_SUFFIXES:
        return "video"
    if suffix in _AUDIO_SUFFIXES:
        return "audio"
    return "file"


def _extract_upload_token(payload: Any) -> str | None:
    """Find the attachment token in an upload response body.

    Shapes differ per host: files answer ``{"fileId": .., "token": ..}``,
    images answer ``{"photos": {"<id>": {"token": ..}}}``, audio/video send
    the token in the ``POST /uploads`` ticket instead.
    """
    if isinstance(payload, dict):
        token = payload.get("token")
        if isinstance(token, str) and token:
            return token
        for value in payload.values():
            found = _extract_upload_token(value)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _extract_upload_token(item)
            if found:
                return found
    return None


def _batch_media(items: list[tuple[Path, str]]) -> list[list[tuple[Path, str]]]:
    """Split prepared media into legal ``POST /messages`` batches.

    Images and videos may share a message (at most 12 together); audio and
    file attachments must be the only attachment in their message.
    """
    batches: list[list[tuple[Path, str]]] = []
    gallery: list[tuple[Path, str]] = []
    for item in items:
        path, upload_type = item
        if upload_type in ("image", "video"):
            gallery.append(item)
            if len(gallery) >= _MAX_ATTACHMENTS_PER_MESSAGE:
                batches.append(gallery)
                gallery = []
        else:
            if gallery:
                batches.append(gallery)
                gallery = []
            batches.append([item])
    if gallery:
        batches.append(gallery)
    return batches


class _AttachmentTooLarge(Exception):
    """Inbound attachment exceeded the configured download cap."""


class MaxChannel(BaseChannel):
    """MAX messenger channel over the Bot API (long polling or webhook)."""

    name = "max"
    display_name = "MAX"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return MaxConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = MaxConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: MaxConfig = config
        self._client: httpx.AsyncClient | None = None
        self._download_client: httpx.AsyncClient | None = None
        self._marker: int | None = None
        self._skip_backlog = False
        # chat session id -> (query parameter name, target id) for replies.
        self._routes: dict[str, tuple[str, str]] = {}
        self._seen_mids: OrderedDict[str, None] = OrderedDict()
        self._voice_notice_at = 0.0
        self._stop_event: asyncio.Event | None = None
        self._webhook_tasks: set[asyncio.Task[None]] = set()
        # chat id -> running "bot is typing" task
        self._typing_tasks: dict[str, asyncio.Task[None]] = {}
        # session chat id -> MAX chat id used by POST /chats/{chatId}/actions
        self._action_chat_ids: dict[str, str] = {}

    def progress_transport_defaults(self) -> tuple[bool, bool] | None:
        # MAX has no message editing: progress/tool hints would arrive as
        # separate messages and hit the 2 msgs/sec chat limit.
        return False, False

    def should_retry_send_error(self, error: Exception) -> bool:
        # 4xx business errors (bad target, rejected payload) cannot succeed
        # until external state changes; do not burn retries on them.
        if isinstance(error, httpx.HTTPStatusError):
            status = error.response.status_code
            return status >= 500 or status == 429
        return True

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _ssl_context(self) -> ssl.SSLContext:
        """System trust store plus the bundled Mintsifry root certificate."""
        ctx = ssl.create_default_context()
        if _BUNDLE_CA_FILE.is_file():
            ctx.load_verify_locations(cafile=str(_BUNDLE_CA_FILE))
        else:
            self.logger.warning(
                "Bundled MAX CA file missing at {}; relying on system trust store",
                _BUNDLE_CA_FILE,
            )
        return ctx

    def _marker_path(self) -> Path:
        return get_data_dir() / _MARKER_STATE_FILE

    def _load_marker(self) -> int | None:
        try:
            data = json.loads(self._marker_path().read_text())
            marker = data.get("marker")
            return int(marker) if marker is not None else None
        except (OSError, ValueError, TypeError):
            return None

    def _save_marker(self) -> None:
        try:
            self._marker_path().write_text(json.dumps({"marker": self._marker}))
        except OSError:
            self.logger.warning("Could not persist MAX polling marker")

    async def start(self) -> None:
        if not self.config.token:
            self.logger.error("MAX token is not configured")
            return

        timeout = httpx.Timeout(30.0, read=self.config.poll_timeout_seconds + _POLL_GRACE_S)
        async with httpx.AsyncClient(
            base_url=self.config.api_base_url.rstrip("/"),
            headers={"Authorization": self.config.token},
            verify=self._ssl_context(),
            timeout=timeout,
        ) as client:
            self._client = client
            try:
                if self.config.mode == "webhook":
                    await self._run_webhook()
                else:
                    await self._run_polling()
            finally:
                self._client = None
        self._running = False
        self.logger.info("MAX channel stopped")

    async def stop(self) -> None:
        self._running = False
        stop_event = self._stop_event
        if stop_event is not None:
            stop_event.set()
        for chat_id in list(self._typing_tasks):
            self._stop_typing(chat_id)
        client = self._client
        if client is not None:
            await client.aclose()
        download_client = self._download_client
        self._download_client = None
        if download_client is not None:
            await download_client.aclose()

    async def _run_polling(self) -> None:
        """Long polling transport (default)."""
        # An active webhook subscription silently disables long polling, so
        # drop the endpoint we know about before falling back here.
        if self.config.webhook_url:
            await self._drop_subscription(self.config.webhook_url, reason="polling mode")

        self._marker = self._load_marker()
        # Fresh install: skip the backlog so restarts do not replay stale chats.
        self._skip_backlog = self._marker is None

        self._running = True
        self.logger.info(
            "MAX long polling started (marker={}, backlog skipped={})",
            self._marker,
            self._skip_backlog,
        )
        while self._running:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 401:
                    self.logger.error(
                        "MAX token rejected (401); stopping channel. "
                        "Refresh the token on business.max.ru and update config.json."
                    )
                    break
                self.logger.warning(
                    "MAX poll failed with {}; retrying in {}s",
                    exc,
                    _POLL_ERROR_BACKOFF_S,
                )
                await asyncio.sleep(_POLL_ERROR_BACKOFF_S)
            except httpx.HTTPError as exc:
                self.logger.warning(
                    "MAX poll error: {}; retrying in {}s", exc, _POLL_ERROR_BACKOFF_S
                )
                await asyncio.sleep(_POLL_ERROR_BACKOFF_S)
            except Exception:
                self.logger.exception(
                    "Unexpected MAX poll failure; retrying in {}s", _POLL_ERROR_BACKOFF_S
                )
                await asyncio.sleep(_POLL_ERROR_BACKOFF_S)

    async def _run_webhook(self) -> None:
        """Webhook transport: local listener + MAX subscription."""
        try:
            from aiohttp import web
        except ImportError:
            self.logger.error(
                "aiohttp is unavailable; MAX webhook mode needs it. Falling back to long polling."
            )
            await self._run_polling()
            return

        if not self.config.webhook_url:
            self.logger.error(
                "channels.max.webhookUrl is empty; falling back to long polling."
            )
            await self._run_polling()
            return

        app = web.Application()
        app.router.add_post(self.config.webhook_path, self._handle_webhook_request)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(
            runner,
            self.config.webhook_listen_host,
            self.config.webhook_listen_port,
        )
        try:
            await site.start()
        except OSError as exc:
            self.logger.error(
                "MAX webhook listener could not bind {}:{} ({}); falling back to long polling.",
                self.config.webhook_listen_host,
                self.config.webhook_listen_port,
                exc,
            )
            await runner.cleanup()
            await self._run_polling()
            return

        self._stop_event = asyncio.Event()
        self._running = True
        self.logger.info(
            "MAX webhook listener on {}:{}{} (public {})",
            self.config.webhook_listen_host,
            self.config.webhook_listen_port,
            self.config.webhook_path,
            self.config.webhook_url,
        )

        if not await self._sync_subscription():
            self.logger.warning(
                "MAX webhook subscription is not active; falling back to long polling."
            )
            self._running = False
            await site.stop()
            await runner.cleanup()
            self._stop_event = None
            await self._run_polling()
            return

        try:
            await self._stop_event.wait()
        finally:
            self._running = False
            tasks = list(self._webhook_tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            # Keep the MAX subscription alive: while the gateway restarts MAX
            # retries delivery (up to 8h), so events are not lost.
            await runner.cleanup()
            self._stop_event = None

    async def _poll_once(self) -> None:
        client = self._require_client()
        params: dict[str, Any] = {
            "limit": _UPDATES_PAGE_LIMIT,
            "timeout": self.config.poll_timeout_seconds,
            "types": _UPDATES_TYPES,
        }
        if self._marker is not None:
            params["marker"] = self._marker
        response = await client.get("/updates", params=params)
        response.raise_for_status()
        data = response.json()
        updates = data.get("updates") or []
        marker = data.get("marker")

        if self._skip_backlog:
            self._skip_backlog = False
            if updates:
                self.logger.info("Skipped {} backlogged MAX update(s)", len(updates))
        else:
            for update in updates:
                await self._process_update_guarded(update)

        if marker is not None:
            self._marker = int(marker)
            self._save_marker()

    # ------------------------------------------------------------------
    # Webhook transport
    # ------------------------------------------------------------------

    async def _drop_subscription(self, url: str, *, reason: str) -> None:
        """Unregister one webhook endpoint; errors are logged, never raised."""
        if not url:
            return
        try:
            response = await self._require_client().delete(
                "/subscriptions", params={"url": url}
            )
            payload: Any
            try:
                payload = response.json()
            except ValueError:
                payload = response.text[:200]
            self.logger.info(
                "MAX subscription delete ({}): {} -> {} {}",
                reason,
                url,
                response.status_code,
                payload,
            )
        except httpx.HTTPError as exc:
            self.logger.warning("Could not delete MAX subscription {}: {}", url, exc)

    async def _sync_subscription(self) -> bool:
        """Register ``webhook_url`` with MAX. Returns True when active."""
        client = self._require_client()
        url = self.config.webhook_url
        try:
            listing = await client.get("/subscriptions")
            if listing.status_code == 401:
                self.logger.error("MAX token rejected (401) while listing subscriptions")
                return False
            listing.raise_for_status()
            existing = [
                item.get("url")
                for item in (listing.json() or {}).get("subscriptions") or []
                if isinstance(item, dict)
            ]
        except httpx.HTTPError as exc:
            self.logger.warning("Could not list MAX subscriptions: {}", exc)
            existing = []

        # A subscription to another URL competes with ours; ours is re-created
        # so secret/update types always match the current config.
        for stale in existing:
            if isinstance(stale, str) and stale and stale != url:
                await self._drop_subscription(stale, reason="stale endpoint")
        for known in existing:
            if known == url:
                await self._drop_subscription(url, reason="refresh")
                break

        body: dict[str, Any] = {
            "url": url,
            "update_types": list(self.config.webhook_update_types),
        }
        if self.config.webhook_secret:
            body["secret"] = self.config.webhook_secret
        else:
            self.logger.warning(
                "MAX webhook secret is empty: requests are not authenticated"
            )

        try:
            response = await client.post("/subscriptions", json=body)
            if response.status_code == 401:
                self.logger.error("MAX token rejected (401) while subscribing")
                return False
            response.raise_for_status()
        except httpx.HTTPError as exc:
            detail = ""
            if isinstance(exc, httpx.HTTPStatusError):
                detail = f" body={exc.response.text[:300]}"
            self.logger.warning("MAX subscription failed: {}{}", exc, detail)
            return False

        self.logger.info(
            "MAX webhook subscription active: {} ({} events, secret={})",
            url,
            ", ".join(self.config.webhook_update_types),
            "set" if self.config.webhook_secret else "none",
        )
        return True

    async def _handle_webhook_request(self, request: Any) -> Any:
        """aiohttp handler: authenticate, acknowledge fast, process in background."""
        from aiohttp import web

        expected = self.config.webhook_secret
        if expected and request.headers.get(_WEBHOOK_SECRET_HEADER) != expected:
            self.logger.warning(
                "MAX webhook rejected: bad {} header from {}",
                _WEBHOOK_SECRET_HEADER,
                request.remote,
            )
            return web.Response(status=403)

        try:
            update = await request.json()
        except ValueError:
            self.logger.warning("MAX webhook received non-JSON body")
            return web.Response(status=400)
        if not isinstance(update, dict):
            return web.Response(status=400)

        task = asyncio.create_task(self._process_update_guarded(update))
        self._webhook_tasks.add(task)
        task.add_done_callback(self._webhook_tasks.discard)
        return web.json_response({"ok": True})

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    async def _process_update_guarded(self, update: dict[str, Any]) -> None:
        """`_process_update` that never propagates errors to the transport."""
        try:
            await self._process_update(update)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception(
                "Failed to process MAX update: {}",
                json.dumps(update, ensure_ascii=False)[:800],
            )

    async def _process_update(self, update: dict[str, Any]) -> None:
        update_type = update.get("update_type")

        if update_type == "bot_started":
            user = update.get("user") or {}
            user_id = user.get("user_id")
            if user_id is not None and self.config.welcome_message:
                await self._send_text(("user_id", str(user_id)), self.config.welcome_message)
            return

        if update_type != "message_created":
            self.logger.info(
                "MAX update type {} skipped: {}",
                update_type,
                json.dumps(update, ensure_ascii=False)[:1200],
            )
            return

        message = update.get("message") or {}
        body = message.get("body") or {}
        mid = body.get("mid")
        if mid is not None:
            if mid in self._seen_mids:
                return
            self._seen_mids[mid] = None
            while len(self._seen_mids) > _SEEN_MIDS_MAX:
                self._seen_mids.popitem(last=False)

        text = (body.get("text") or "").strip()
        audio_items = _audio_attachments(body)
        media_items = _media_attachments(body)
        if not text and not audio_items and not media_items:
            if not message or not body:
                # Native voice / video note: MAX sends `message_created` with no
                # message at all (upstream bug, see _VOICE_NOTICE_TEXT).
                self.logger.warning(
                    "MAX dropped a message payload (native voice?); raw update: {}",
                    json.dumps(update, ensure_ascii=False)[:600],
                )
                await self._notify_dropped_voice()
                return
            self.logger.info(
                "MAX message without text or audio (mid={}); attachments={}",
                mid,
                [
                    str(item.get("type"))
                    for item in (body.get("attachments") or [])
                    if isinstance(item, dict)
                ],
            )
            return

        sender = message.get("sender") or {}
        recipient = message.get("recipient") or {}
        sender_id = sender.get("user_id")
        if sender_id is None:
            self.logger.debug("Ignoring MAX message without sender (mid={})", mid)
            return

        chat_type = recipient.get("chat_type") or "dialog"
        is_dm = chat_type == "dialog"
        chat_ref = recipient.get("chat_id")
        if chat_ref is not None:
            route = ("chat_id", str(chat_ref))
        else:
            route = ("user_id", str(sender_id))
        chat_id = route[1]
        self._routes[chat_id] = route
        if chat_ref is not None:
            self._action_chat_ids[chat_id] = str(chat_ref)
            # Immediate feedback: read receipt, then "bot is typing" until reply.
            if self.config.mark_seen:
                await self._send_action(str(chat_ref), _ACTION_MARK_SEEN)
            if self.config.typing_indicator:
                self._start_typing(chat_id)

        attachment_types = [
            str(item.get("type"))
            for item in (body.get("attachments") or [])
            if isinstance(item, dict)
        ]
        self.logger.info(
            "MAX {} message from {} in {} (mid={}, audio={}, attachments={})",
            chat_type,
            sender_id,
            chat_id,
            mid,
            len(audio_items),
            attachment_types,
        )
        content_parts = [text] if text else []
        media: list[str] = []
        # Only fetch/transcribe attachments for senders that already passed the
        # allowFrom/pairing gate; unauthorised traffic must not cost CPU or disk.
        if self.is_allowed(str(sender_id)):
            if audio_items:
                audio_paths, audio_parts = await self._collect_audio(mid, sender_id, audio_items)
                media.extend(audio_paths)
                content_parts.extend(audio_parts)
            if media_items:
                media_paths, media_parts = await self._collect_media(mid, sender_id, media_items)
                media.extend(media_paths)
                content_parts.extend(media_parts)
        content = "\n".join(part for part in content_parts if part).strip()
        await self._handle_message(
            sender_id=str(sender_id),
            chat_id=chat_id,
            content=content,
            media=media,
            metadata={
                "message_id": mid,
                "seq": body.get("seq"),
                "chat_type": chat_type,
                "sender_name": sender.get("first_name"),
            },
            is_dm=is_dm,
        )

    # ------------------------------------------------------------------
    # Inbound attachments (voice messages arrive as `audio` attachments)
    # ------------------------------------------------------------------

    async def _notify_dropped_voice(self) -> None:
        """Tell paired users once in a while that MAX swallowed the message."""
        now = time.monotonic()
        if now - self._voice_notice_at < _VOICE_NOTICE_INTERVAL_S:
            return
        self._voice_notice_at = now
        for user_id in get_approved(self.name):
            try:
                await self._send_text(("user_id", str(user_id)), _VOICE_NOTICE_TEXT)
            except Exception as exc:  # noqa: BLE001 - notice must never break polling
                self.logger.warning("Could not deliver voice notice to {}: {}", user_id, exc)

    def _require_download_client(self) -> httpx.AsyncClient:
        """Client for attachment URLs: no default Authorization header."""
        if self._download_client is None:
            self._download_client = httpx.AsyncClient(
                verify=self._ssl_context(),
                timeout=_DOWNLOAD_TIMEOUT_S,
                follow_redirects=True,
            )
        return self._download_client

    async def _download_attachment(
        self, url: str, filename: str, *, max_bytes: int | None = None
    ) -> Path | None:
        """Download one attachment. Returns the stored path or None."""
        client = self._require_download_client()
        target = get_media_dir("max") / filename
        # MAX media hosts (iu.oneme.ru, omu.okcdn.ru) sign the URL; the bot
        # token is accepted on some hosts and rejected on others.
        for headers in ({"Authorization": self.config.token}, {}):
            try:
                async with client.stream("GET", url, headers=headers) as response:
                    if response.status_code in (401, 403) and headers:
                        continue
                    response.raise_for_status()
                    written = 0
                    with target.open("wb") as fh:
                        async for chunk in response.aiter_bytes():
                            written += len(chunk)
                            if max_bytes is not None and written > max_bytes:
                                raise _AttachmentTooLarge(filename)
                            fh.write(chunk)
                    return target
            except _AttachmentTooLarge:
                target.unlink(missing_ok=True)
                self.logger.warning(
                    "MAX attachment {} exceeds the {} byte cap; skipped",
                    filename,
                    max_bytes,
                )
                return None
            except httpx.HTTPError as exc:
                self.logger.warning("MAX attachment download failed ({}): {}", filename, exc)
                return None
        return None

    async def _collect_audio(
        self,
        mid: Any,
        sender_id: Any,
        items: list[dict[str, Any]],
    ) -> tuple[list[str], list[str]]:
        """Store and transcribe audio attachments -> (media paths, content parts)."""
        paths: list[str] = []
        parts: list[str] = []
        for index, item in enumerate(items):
            payload = item.get("payload") or {}
            url = payload.get("url")
            filename_hint = str(payload.get("filename") or item.get("filename") or "")
            suffix = Path(filename_hint).suffix.lower()
            if suffix not in _AUDIO_SUFFIXES:
                suffix = ".mp3"
            local_name = f"{mid or 'max'}-{sender_id}-{index}{suffix}"

            path: Path | None = None
            if isinstance(url, str) and url:
                path = await self._download_attachment(url, local_name)
            if path is not None:
                paths.append(str(path))

            transcription = str(item.get("transcription") or "").strip()
            if transcription:
                # MAX occasionally ships its own speech-to-text result.
                parts.append(f"[transcription: {transcription}]")
                continue
            if path is None:
                parts.append("[audio: download failed]")
                continue
            try:
                if path.stat().st_size > _TRANSCRIBE_MAX_BYTES:
                    self.logger.warning("MAX audio {} too large to transcribe", path.name)
                    parts.append(f"[audio: {path}]")
                    continue
            except OSError:
                pass
            transcribed = (await self.transcribe_audio(path)).strip()
            if transcribed:
                self.logger.info("Transcribed MAX audio: {}...", transcribed[:50])
                parts.append(f"[transcription: {transcribed}]")
            else:
                parts.append(f"[audio: {path}]")
        return paths, parts

    @staticmethod
    def _inbound_filename(
        mid: Any,
        sender_id: Any,
        index: int,
        filename_hint: str,
        label: str,
        url: str,
    ) -> str:
        """Local name for an inbound attachment, keeping the original when sane."""
        candidate = _sanitize_filename(filename_hint)
        if not candidate:
            suffix = Path(urlparse(url).path).suffix.lower()
            if not _URL_SUFFIX_RE.fullmatch(suffix):
                suffix = _DEFAULT_SUFFIX_BY_LABEL.get(label, ".bin")
            candidate = f"{mid or 'max'}-{sender_id}-{index}{suffix}"
        try:
            if (get_media_dir("max") / candidate).exists():
                return f"{mid or 'max'}-{index}-{candidate}"
        except OSError:
            pass
        return candidate

    async def _collect_media(
        self,
        mid: Any,
        sender_id: Any,
        items: list[dict[str, Any]],
    ) -> tuple[list[str], list[str]]:
        """Store image/video/file attachments -> (media paths, content parts)."""
        paths: list[str] = []
        parts: list[str] = []
        max_bytes = (
            self.config.download_max_mb * 1024 * 1024
            if self.config.download_max_mb
            else None
        )
        for index, item in enumerate(items):
            atype = str(item.get("type") or "").lower()
            label = "image" if atype == "sticker" else (atype or "file")
            payload = item.get("payload") or {}
            url = payload.get("url")
            if not isinstance(url, str) or not url:
                parts.append(f"[{label}: нет ссылки для скачивания]")
                continue
            size_hint = item.get("size") or payload.get("size")
            if (
                isinstance(size_hint, int)
                and max_bytes is not None
                and size_hint > max_bytes
            ):
                parts.append(
                    f"[{label}: пропущено, {size_hint} байт больше лимита {self.config.download_max_mb} МБ]"
                )
                continue
            filename_hint = str(item.get("filename") or payload.get("filename") or "")
            local_name = self._inbound_filename(
                mid, sender_id, index, filename_hint, label, url
            )
            path = await self._download_attachment(url, local_name, max_bytes=max_bytes)
            if path is None:
                parts.append(f"[{label}: скачивание не удалось]")
                continue
            paths.append(str(path))
            parts.append(f"[{label}: {path}]")
        return paths, parts

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    def _resolve_route(self, chat_id: str) -> tuple[str, str]:
        route = self._routes.get(str(chat_id))
        if route is not None:
            return route
        # Proactive send to an unknown target: assume a chat id and let the
        # caller fall back to user_id if the API rejects it.
        return ("chat_id", str(chat_id))

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("MAX client is not initialized")
        return self._client

    # ------------------------------------------------------------------
    # Chat actions (typing / read receipt)
    # ------------------------------------------------------------------

    async def _send_action(self, chat_id: str, action: str) -> bool:
        """Send one bot action to a chat. Never raises."""
        client = self._client
        if client is None:
            return False
        try:
            response = await client.post(
                f"/chats/{chat_id}/actions", json={"action": action}
            )
            payload: Any
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if response.status_code == 200 and isinstance(payload, dict):
                if payload.get("success", True):
                    return True
                self.logger.info(
                    "MAX action {} to {} refused: {}",
                    action,
                    chat_id,
                    payload.get("message"),
                )
                return False
            self.logger.debug(
                "MAX action {} to {} -> {}", action, chat_id, response.status_code
            )
        except httpx.HTTPError as exc:
            self.logger.debug("MAX action {} to {} failed: {}", action, chat_id, exc)
        return False

    def _start_typing(self, chat_id: str) -> None:
        """Keep the "bot is typing" indicator alive until the reply is sent."""
        target = self._action_chat_ids.get(str(chat_id))
        if target is None or str(chat_id) in self._typing_tasks:
            return
        task = asyncio.create_task(self._typing_loop(str(chat_id), target))
        self._typing_tasks[str(chat_id)] = task
        task.add_done_callback(
            lambda _task, key=str(chat_id): self._typing_tasks.pop(key, None)
        )

    def _stop_typing(self, chat_id: str) -> None:
        task = self._typing_tasks.pop(str(chat_id), None)
        if task is not None and not task.done():
            task.cancel()

    async def _typing_loop(self, chat_id: str, action_chat_id: str) -> None:
        # MAX shows the indicator only briefly, so refresh it; stop when the
        # reply is delivered (`send`) or when the safety cap is reached.
        deadline = time.monotonic() + min(
            self.config.typing_max_seconds, _TYPING_MAX_DURATION_S
        )
        try:
            while time.monotonic() < deadline and self._running:
                if not await self._send_action(action_chat_id, _ACTION_TYPING_ON):
                    return
                await asyncio.sleep(self.config.typing_interval_seconds)
        except asyncio.CancelledError:
            return

    async def send(self, msg: OutboundMessage) -> None:
        # First chunk means "stop typing"; the answer is on its way.
        self._stop_typing(str(msg.chat_id))
        content = (msg.content or "").strip()
        route = self._resolve_route(msg.chat_id)
        chat_id = str(msg.chat_id)
        media = [m for m in (msg.media or []) if isinstance(m, str) and m.strip()]
        if not media:
            if not content:
                return
            for chunk in chunk_text(content):
                await self._send_text(route, chunk, chat_id=chat_id)
            return
        # Media delivery must never raise: a manager-level retry would upload
        # and send every attachment a second time.
        try:
            failures = await self._send_media_message(route, chat_id, content, media)
            for note in failures:
                for chunk in chunk_text(note):
                    await self._send_text(route, chunk, chat_id=chat_id)
        except Exception:
            self.logger.exception("MAX media delivery failed for {}", media)

    async def _send_media_message(
        self,
        route: tuple[str, str],
        chat_id: str,
        content: str,
        media: list[str],
    ) -> list[str]:
        """Upload and send media; the text rides with the first batch.

        Returns failure notes for attachments that could not be delivered.
        """
        items, failures = await self._prepare_media(media)
        batches = _batch_media(items)
        if not batches:
            # Nothing to attach: deliver the text as usual.
            if content:
                for chunk in chunk_text(content):
                    await self._send_text(route, chunk, chat_id=chat_id)
            return failures
        text_chunks = chunk_text(content) if content else []
        head = text_chunks[0] if text_chunks else ""
        for index, batch in enumerate(batches):
            failures.extend(
                await self._send_media_batch(
                    route, chat_id, head if index == 0 else "", batch
                )
            )
        for chunk in text_chunks[1:]:
            await self._send_text(route, chunk, chat_id=chat_id)
        return failures

    async def _prepare_media(
        self, media: list[str]
    ) -> tuple[list[tuple[Path, str]], list[str]]:
        """Resolve media entries to local files + upload types; collect failures."""
        items: list[tuple[Path, str]] = []
        failures: list[str] = []
        for raw in media:
            raw = raw.strip()
            if raw.startswith("file://"):
                raw = raw[len("file://") :]
            path: Path | None = None
            if raw.startswith(("http://", "https://")):
                ok, error = validate_url_target(raw)
                if not ok:
                    failures.append(f"[вложение пропущено: небезопасный URL ({error})]")
                    continue
                name = _sanitize_filename(Path(urlparse(raw).path).name) or "download"
                path = await self._download_attachment(raw, name)
                if path is None:
                    failures.append(f"[вложение не доставлено: {name}]")
                    continue
            else:
                candidate = Path(raw).expanduser()
                if not candidate.is_file():
                    failures.append(f"[вложение не доставлено: {candidate.name}]")
                    continue
                path = candidate
            upload_type = _classify_media(path.name)
            limit = _MAX_UPLOAD_BYTES.get(upload_type)
            try:
                size = path.stat().st_size
            except OSError:
                failures.append(f"[вложение не доставлено: {path.name}]")
                continue
            if limit is not None and size > limit:
                failures.append(
                    f"[вложение пропущено: {path.name} ({size} байт) больше "
                    f"лимита MAX для {upload_type}]"
                )
                continue
            items.append((path, upload_type))
        return items, failures

    async def _upload_attachment(self, path: Path, upload_type: str) -> str | None:
        """Upload one file to MAX (``POST /uploads`` + multipart). Returns a token."""
        client = self._require_client()
        try:
            response = await client.post("/uploads", params={"type": upload_type})
            response.raise_for_status()
            info: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            self.logger.warning("MAX upload ticket failed for {}: {}", path.name, exc)
            return None
        upload_url = info.get("url") if isinstance(info, dict) else None
        if not isinstance(upload_url, str) or not upload_url:
            self.logger.warning("MAX /uploads returned no url for {}: {}", path.name, info)
            return None
        ticket_token = info.get("token") if isinstance(info, dict) else None
        token = ticket_token if isinstance(ticket_token, str) and ticket_token else None
        try:
            data = path.read_bytes()
        except OSError as exc:
            self.logger.warning("Could not read {} for upload: {}", path, exc)
            return None
        ok, payload = await self._post_upload_bytes(upload_url, path.name, data)
        if not ok:
            return None
        # Image hosts return the token in the upload body; audio/video tickets
        # carry it up front and answer with plain XML.
        return token or _extract_upload_token(payload)

    async def _post_upload_bytes(
        self, upload_url: str, filename: str, data: bytes
    ) -> tuple[bool, Any]:
        """Multipart-upload bytes to a ticket URL. Returns (ok, parsed payload)."""
        client = self._require_download_client()
        # Signed upload URLs (iu.oneme.ru, omu.okcdn.ru, fu.oneme.ru): the bot
        # token is accepted on some hosts and rejected on others.
        for headers in ({"Authorization": self.config.token}, {}):
            try:
                response = await client.post(
                    upload_url,
                    headers=headers,
                    files={"data": (filename, data)},
                    timeout=_UPLOAD_TIMEOUT_S,
                )
                if response.status_code in (401, 403) and headers:
                    continue
                response.raise_for_status()
                try:
                    return True, response.json()
                except ValueError:
                    return True, None
            except httpx.HTTPError as exc:
                self.logger.warning("MAX media upload failed ({}): {}", filename, exc)
                return False, None
        return False, None

    async def _send_media_batch(
        self,
        route: tuple[str, str],
        chat_id: str,
        text: str,
        batch: list[tuple[Path, str]],
    ) -> list[str]:
        """Send one message carrying a media batch. Returns failure notes."""
        attachments: list[tuple[dict[str, Any], str]] = []
        failures: list[str] = []
        for path, upload_type in batch:
            token = await self._upload_attachment(path, upload_type)
            if token is None:
                failures.append(f"[вложение не доставлено: {path.name}]")
                continue
            attachments.append(
                ({"type": upload_type, "payload": {"token": token}}, path.name)
            )
        if not attachments:
            return failures
        if await self._send_attachments_message(
            route, chat_id, text, [item for item, _ in attachments]
        ):
            return failures
        failures.extend(f"[вложение не доставлено: {name}]" for _, name in attachments)
        return failures

    async def _send_attachments_message(
        self,
        route: tuple[str, str],
        chat_id: str,
        text: str,
        attachments: list[dict[str, Any]],
    ) -> bool:
        """POST /messages with attachments, retrying while MAX processes uploads."""
        client = self._require_client()
        params = {route[0]: route[1]}
        body: dict[str, Any] = {"attachments": attachments}
        if text:
            body["text"] = text
            if self.config.text_format:
                body["format"] = self.config.text_format

        delays = _ATTACHMENT_READY_DELAYS
        attempt = 0
        while True:
            try:
                response = await client.post("/messages", params=params, json=body)
            except httpx.HTTPError as exc:
                self.logger.warning("MAX media send error: {}", exc)
                return False
            detail = response.text[:300]
            if response.is_success:
                self.logger.info(
                    "Sent MAX message with {} attachment(s) to {}={}",
                    len(attachments),
                    route[0],
                    route[1],
                )
                return True
            if (
                response.status_code == 400
                and ("attachment.not.ready" in detail or "not.processed" in detail)
                and attempt < len(delays)
            ):
                # Upload is still being processed server-side.
                await asyncio.sleep(delays[attempt])
                attempt += 1
                continue
            if response.status_code == 400 and "format" in body and attempt < len(delays):
                # Markup rejected: retry once as plain text.
                body.pop("format")
                attempt += 1
                continue
            if response.status_code == 429 and attempt < len(delays):
                await asyncio.sleep(_RATE_LIMIT_BACKOFF_S)
                attempt += 1
                continue
            self.logger.warning(
                "MAX media send rejected ({}): {}", response.status_code, detail
            )
            return False

    async def _send_text(
        self,
        route: tuple[str, str],
        text: str,
        *,
        chat_id: str | None = None,
    ) -> None:
        client = self._require_client()
        params = {route[0]: route[1]}
        body: dict[str, Any] = {"text": text}
        if self.config.text_format:
            body["format"] = self.config.text_format

        for attempt in range(3):
            try:
                response = await client.post("/messages", params=params, json=body)
                if (
                    response.status_code == 400
                    and "format" in body
                    and attempt == 0
                ):
                    # Markup rejected: retry once as plain text.
                    body.pop("format")
                    continue
                if response.status_code == 429 and attempt < 2:
                    await asyncio.sleep(_RATE_LIMIT_BACKOFF_S)
                    continue
                if (
                    response.status_code >= 400
                    and route[0] == "chat_id"
                    and chat_id is not None
                ):
                    # Unknown proactive target may be a user id after all.
                    fallback = ("user_id", route[1])
                    self._routes[chat_id] = fallback
                    params = {fallback[0]: fallback[1]}
                    continue
                response.raise_for_status()
                self.logger.debug("Sent MAX message to {}={}", route[0], route[1])
                return
            except httpx.HTTPError as exc:
                if attempt == 2:
                    raise
                self.logger.warning("MAX send error (attempt {}): {}", attempt + 1, exc)
                await asyncio.sleep(_RATE_LIMIT_BACKOFF_S)
        # Unreachable: the loop either returns or raises.
        raise RuntimeError("MAX send failed")
