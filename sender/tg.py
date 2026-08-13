"""Telegram delivery via Telethon (StringSession, no bot token).

Message shape: an optional HTML header, then every config in the batch inside a
single ``<pre>`` block — one tap copies the whole batch — then an optional footer.
If the rendered text exceeds ``message.max_chars`` (Telegram's hard limit is
4096) the batch is split across several messages automatically.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from typing import Any

from telethon import TelegramClient
from telethon.errors import (
    ChannelPrivateError,
    ChatWriteForbiddenError,
    FloodWaitError,
    MessageTooLongError,
    RPCError,
    UsernameNotOccupiedError,
)
from telethon.sessions import StringSession

LOG = logging.getLogger(__name__)

TELEGRAM_HARD_LIMIT = 4096


class MissingCredentials(SystemExit):
    """Raised when API credentials are not present in the environment."""


@dataclass
class Credentials:
    api_id: int
    api_hash: str
    session: str

    @classmethod
    def from_env(cls) -> "Credentials":
        api_id = os.environ.get("TELEGRAM_API_ID", "").strip()
        api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()
        session = os.environ.get("TELEGRAM_SESSION", "").strip()
        missing = [
            name
            for name, value in (
                ("TELEGRAM_API_ID", api_id),
                ("TELEGRAM_API_HASH", api_hash),
                ("TELEGRAM_SESSION", session),
            )
            if not value
        ]
        if missing:
            raise MissingCredentials(
                "missing credentials: " + ", ".join(missing) + "\n"
                "Set them as GitHub repository secrets (Settings → Secrets and "
                "variables → Actions), or export them locally."
            )
        try:
            return cls(api_id=int(api_id), api_hash=api_hash, session=session)
        except ValueError as exc:
            raise MissingCredentials(f"TELEGRAM_API_ID must be numeric: {exc}") from None


def render(configs: list[str], channel: str, message_cfg: dict[str, Any], batch_index: int, batch_total: int) -> list[str]:
    """Render a batch into one or more ready-to-send HTML messages."""
    max_chars = min(int(message_cfg.get("max_chars", 4000)), TELEGRAM_HARD_LIMIT)
    use_code = bool(message_cfg.get("code_block", True))
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    def wrap(chunk: list[str], part: int, parts: int) -> str:
        fields = {
            "channel": channel,
            "date": today,
            "count": len(chunk),
            "batch_index": batch_index,
            "batch_total": batch_total,
            "part": part,
            "part_total": parts,
        }
        header = str(message_cfg.get("header") or "").format(**fields)
        footer = str(message_cfg.get("footer") or "").format(**fields)
        if parts > 1 and header:
            header = header.rstrip("\n") + f" (part {part}/{parts})\n"
        body = "\n".join(escape(c) for c in chunk)
        if use_code:
            body = f"<pre>{body}</pre>"
        return f"{header}{body}{footer}"

    single = wrap(configs, 1, 1)
    if len(single) <= max_chars:
        return [single]

    # Split into as few chunks as possible while staying under the limit.
    chunks: list[list[str]] = []
    current: list[str] = []
    # Reserve room for header/footer/markup; measured empirically per chunk below.
    for config in configs:
        candidate = current + [config]
        if current and len(wrap(candidate, 1, 2)) > max_chars:
            chunks.append(current)
            current = [config]
        else:
            current = candidate
    if current:
        chunks.append(current)

    return [wrap(chunk, i + 1, len(chunks)) for i, chunk in enumerate(chunks)]


class Sender:
    """Thin async wrapper around a Telethon client."""

    def __init__(self, creds: Credentials, dry_run: bool = False) -> None:
        self.creds = creds
        self.dry_run = dry_run
        self.client: TelegramClient | None = None
        self._entities: dict[str, Any] = {}

    async def __aenter__(self) -> "Sender":
        if self.dry_run:
            LOG.warning("DRY RUN — no Telegram connection will be made")
            return self
        self.client = TelegramClient(
            StringSession(self.creds.session), self.creds.api_id, self.creds.api_hash
        )
        await self.client.connect()
        if not await self.client.is_user_authorized():
            await self.client.disconnect()
            raise SystemExit(
                "TELEGRAM_SESSION is not authorized. Regenerate it with "
                "`python tools/gen_session.py` and update the secret."
            )
        me = await self.client.get_me()
        LOG.info("signed in as %s (id=%s)", getattr(me, "username", None) or me.first_name, me.id)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self.client is not None:
            await self.client.disconnect()

    async def resolve(self, channel: str) -> Any:
        """Resolve and cache a channel entity, with a clear error when it fails."""
        if self.dry_run:
            return channel
        if channel in self._entities:
            return self._entities[channel]
        assert self.client is not None
        try:
            entity = await self.client.get_entity(channel)
        except (UsernameNotOccupiedError, ValueError) as exc:
            raise SystemExit(
                f"cannot resolve {channel}: {exc}. Check the username, and make sure "
                "the account behind TELEGRAM_SESSION is a member of the channel."
            ) from None
        except ChannelPrivateError:
            raise SystemExit(
                f"{channel} is private or the session account was removed from it."
            ) from None
        self._entities[channel] = entity
        return entity

    async def send(self, channel: str, text: str) -> int | None:
        """Send one message. Returns the message id (None in dry-run)."""
        if self.dry_run:
            LOG.info("[dry-run] → %s (%d chars)\n%s", channel, len(text), text[:400])
            return None

        assert self.client is not None
        entity = await self.resolve(channel)

        for attempt in range(1, 4):
            try:
                message = await self.client.send_message(
                    entity, text, parse_mode="html", link_preview=False
                )
                LOG.info("sent to %s (message id %s, %d chars)", channel, message.id, len(text))
                return message.id
            except FloodWaitError as exc:
                wait = int(getattr(exc, "seconds", 30)) + 3
                if wait > 900:
                    LOG.error("flood wait of %ss is too long for this run — stopping", wait)
                    raise
                LOG.warning("flood wait: sleeping %ss (attempt %d/3)", wait, attempt)
                await asyncio.sleep(wait)
            except ChatWriteForbiddenError:
                raise SystemExit(
                    f"the session account cannot post in {channel} — grant it posting rights."
                ) from None
            except MessageTooLongError:
                LOG.error("message rejected as too long (%d chars) — lower message.max_chars", len(text))
                raise
            except RPCError as exc:
                if attempt == 3:
                    LOG.error("giving up on %s after 3 attempts: %s", channel, exc)
                    raise
                backoff = 5 * attempt
                LOG.warning("send failed (%s) — retrying in %ss", exc, backoff)
                await asyncio.sleep(backoff)
        return None
