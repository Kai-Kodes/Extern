"""Native Telegram integration adapter for Todo CLI."""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from todo.integrations.filemeta import FilemetaScanResult, ScannedFile


class TelegramIntegrationError(Exception):
    """Base exception for Telegram integration errors."""
    pass


class TelegramNotConfiguredError(TelegramIntegrationError):
    """Raised when Telegram API credentials or session configuration is missing."""
    pass


class TelegramSessionNotFoundError(TelegramIntegrationError):
    """Raised when the specified Telegram session file does not exist."""
    pass


class TelegramUnauthorizedError(TelegramIntegrationError):
    """Raised when the Telegram session is invalid or not logged in."""
    pass


class TelegramConnectionError(TelegramIntegrationError):
    """Raised when a network or connection error occurs while reaching Telegram."""
    pass


class TelegramChannelNotFoundError(TelegramIntegrationError):
    """Raised when the target channel cannot be found or accessed."""
    pass


@dataclass
class TelegramChannel:
    id: int
    title: str
    username: Optional[str] = None
    is_channel: bool = True
    is_group: bool = False
    item_count: int = 0


def format_duration(seconds: Optional[int | float]) -> str:
    """Format duration in seconds to a human-friendly string like '1h 32m 54s' or '05:30'."""
    if seconds is None:
        return ""
    try:
        total = int(seconds)
    except (ValueError, TypeError):
        return ""

    if total <= 0:
        return ""

    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60

    if hours > 0:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    return f"{minutes:02d}:{secs:02d}"


def clean_video_title(
    filename: Optional[str], caption: Optional[str] = None, msg_id: int = 0
) -> str:
    """Derive a friendly display title for a video message."""
    if filename:
        stem = Path(filename).stem
        if stem.strip():
            return stem.strip()

    if caption:
        first_line = caption.strip().splitlines()[0]
        cleaned = first_line.strip()
        if len(cleaned) > 60:
            cleaned = cleaned[:57] + "..."
        if cleaned:
            return cleaned

    return f"Video #{msg_id}" if msg_id else "Untitled Video"


class TelegramAdapter:
    """
    Adapter for communicating with Telegram via Telethon.
    Manages client lifecycle, session authorization, and video course extraction.
    Safe to import without triggering network connections or login prompts.
    """

    def __init__(
        self,
        session_path: Optional[Path | str] = None,
        api_id: Optional[int] = None,
        api_hash: Optional[str] = None,
    ):
        self.session_path = Path(session_path) if session_path else None
        self.api_id = api_id
        self.api_hash = api_hash
        self._client = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def check_availability(self) -> Tuple[bool, Optional[str]]:
        """
        Check whether Telethon is installed and whether credentials/session are configured.
        Returns: (is_available, error_message_if_not)
        """
        try:
            import telethon  # noqa: F401
        except ImportError:
            return False, "Telethon library is not installed in the current environment."

        if not self.api_id or not self.api_hash:
            return (
                False,
                "Telegram API credentials (api_id / api_hash) are not configured.\n"
                "Set TELEGRAM_API_ID and TELEGRAM_API_HASH or configure ~/.config/todo/config.toml.",
            )

        if not self.session_path:
            return (
                False,
                "Telegram session path is not configured.\n"
                "Set TELEGRAM_SESSION_PATH or configure session_path in ~/.config/todo/config.toml.",
            )

        session_file = self.session_path
        if not str(session_file).endswith(".session"):
            session_file = Path(f"{session_file}.session")

        if not session_file.exists():
            return (
                False,
                f"Telegram session file not found at: {session_file}\n"
                "Please verify your session path or provide a valid session.",
            )

        return True, None

    def _run_async(self, coro):
        """Execute an asynchronous coroutine synchronously using the client's loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, coro).result()
        return asyncio.run(coro)

    def _get_client(self):
        """Lazily initialize the Telethon TelegramClient instance."""
        if self._client is not None:
            return self._client

        from telethon import TelegramClient

        session_str = str(self.session_path)
        if session_str.endswith(".session"):
            session_str = session_str[:-8]

        self._client = TelegramClient(session_str, self.api_id, self.api_hash)
        return self._client

    def connect(self) -> None:
        """
        Connect to Telegram and verify that the session is authorized.
        Raises:
            TelegramNotConfiguredError
            TelegramSessionNotFoundError
            TelegramUnauthorizedError
            TelegramConnectionError
        """
        ok, err = self.check_availability()
        if not ok:
            if "not configured" in err.lower():
                raise TelegramNotConfiguredError(err)
            if "not found at" in err.lower():
                raise TelegramSessionNotFoundError(err)
            raise TelegramIntegrationError(err)

        async def _connect_internal():
            client = self._get_client()
            if not client.is_connected():
                try:
                    await client.connect()
                except Exception as e:
                    raise TelegramConnectionError(f"Failed to connect to Telegram: {e}") from e

            try:
                authorized = await client.is_user_authorized()
            except Exception as e:
                raise TelegramConnectionError(f"Telegram authorization check failed: {e}") from e

            if not authorized:
                raise TelegramUnauthorizedError(
                    f"Telegram session at {self.session_path} is not authorized or has expired.\n"
                    "Re-authorization is required."
                )

        self._run_async(_connect_internal())

    def disconnect(self) -> None:
        """Disconnect client cleanly and release resources."""
        if self._client is not None:
            async def _disconnect_internal():
                if self._client.is_connected():
                    await self._client.disconnect()
            try:
                self._run_async(_disconnect_internal())
            except Exception:
                pass
            self._client = None

    def is_authorized(self) -> bool:
        """Check if the client is currently connected and authorized."""
        try:
            self.connect()
            return True
        except Exception:
            return False

    def discover_channels(self, limit: int = 150) -> List[TelegramChannel]:
        """
        Discover channels and groups available to the Telegram account.
        Filters out 1-on-1 chats and bots.
        """
        self.connect()

        async def _discover():
            client = self._get_client()
            dialogs = await client.get_dialogs(limit=limit)
            channels: List[TelegramChannel] = []

            for d in dialogs:
                if d.is_channel or d.is_group:
                    username = getattr(d.entity, "username", None)
                    channels.append(
                        TelegramChannel(
                            id=d.id,
                            title=d.name or f"Chat #{d.id}",
                            username=username,
                            is_channel=d.is_channel,
                            is_group=d.is_group,
                        )
                    )

            # Sort alphabetically by title
            channels.sort(key=lambda c: c.title.lower())
            return channels

        return self._run_async(_discover())

    def scan_channel(
        self, channel_id: int | str, limit: Optional[int] = None
    ) -> FilemetaScanResult:
        """
        Scan a channel for video messages and extract structured course metadata.
        Returns a FilemetaScanResult compatible with SourceService.
        """
        self.connect()

        async def _scan():
            client = self._get_client()
            try:
                entity = await client.get_entity(channel_id)
            except Exception as e:
                raise TelegramChannelNotFoundError(
                    f"Unable to access Telegram channel {channel_id}: {e}"
                ) from e

            channel_title = getattr(entity, "title", str(channel_id))
            channel_uri = f"telegram://{channel_id}"

            items: List[ScannedFile] = []
            async for msg in client.iter_messages(entity, limit=limit):
                is_video = False
                duration: Optional[int] = None
                raw_filename: Optional[str] = None
                size_bytes: Optional[int] = None

                if msg.video:
                    is_video = True
                    size_bytes = getattr(msg.video, "size", None)
                    for attr in msg.video.attributes:
                        if hasattr(attr, "duration") and attr.duration:
                            duration = int(attr.duration)
                        if hasattr(attr, "file_name") and attr.file_name:
                            raw_filename = attr.file_name

                elif msg.document:
                    mime = getattr(msg.document, "mime_type", "") or ""
                    if mime.startswith("video/"):
                        is_video = True
                    size_bytes = getattr(msg.document, "size", None)

                    for attr in msg.document.attributes:
                        if hasattr(attr, "duration") and attr.duration:
                            is_video = True
                            duration = int(attr.duration)
                        if hasattr(attr, "file_name") and attr.file_name:
                            raw_filename = attr.file_name

                if is_video:
                    if not raw_filename and msg.file:
                        raw_filename = msg.file.name

                    clean_title = clean_video_title(
                        raw_filename, caption=msg.message, msg_id=msg.id
                    )
                    dur_formatted = format_duration(duration)

                    meta: Dict[str, Any] = {
                        "source_type": "telegram",
                        "channel_id": channel_id,
                        "channel_title": channel_title,
                        "message_id": msg.id,
                        "duration": duration,
                        "duration_formatted": dur_formatted,
                        "file_name": raw_filename or clean_title,
                        "size": size_bytes,
                        "date": msg.date.isoformat() if msg.date else None,
                    }

                    # Stable unique URI for the item: telegram://{channel_id}/{msg_id}
                    item_uri = f"telegram://{channel_id}/{msg.id}"

                    items.append(
                        ScannedFile(
                            name=raw_filename or f"msg_{msg.id}.mp4",
                            path=item_uri,
                            title=clean_title,
                            type="video",
                            metadata=meta,
                        )
                    )

            # Sort chronologically by message ID so course lessons progress in natural order
            items.sort(key=lambda it: it.metadata.get("message_id", 0))

            return FilemetaScanResult(
                source_name=channel_title,
                source_path=channel_uri,
                items=items,
            )

        return self._run_async(_scan())
