"""Quotes: context menu + slash commands + reply trigger + image cards."""
from __future__ import annotations

import re
from datetime import time
from zoneinfo import ZoneInfo

import io
import logging
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from services.quote_card import build_quote_card

import config

log = logging.getLogger("friend-bot.quotes")


class Quotes(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._session: Optional[aiohttp.ClientSession] = None
        self.save_menu = app_commands.ContextMenu(
            name="Save Quote",
            callback=self.save_quote_ctx,
        )
        self.quote_menu = app_commands.ContextMenu(
            name="Quote",
            callback=self.quote_image_ctx,
        )
        self.bot.tree.add_command(self.save_menu)
        self.bot.tree.add_command(self.quote_menu)

    async def cog_load(self) -> None:
        self._session = aiohttp.ClientSession()
        if not self.quote_of_the_day.is_running():
            self.quote_of_the_day.start()

    async def cog_unload(self) -> None:
        if self.quote_of_the_day.is_running():
            self.quote_of_the_day.cancel()
        self.bot.tree.remove_command(self.save_menu.name, type=self.save_menu.type)
        self.bot.tree.remove_command(self.quote_menu.name, type=self.quote_menu.type)
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    @staticmethod
    def _quote_content(message: discord.Message) -> Optional[str]:
        content = (message.clean_content or "").strip()
        if content:
            return content[:1900]
        if message.attachments:
            return message.attachments[0].url
        return None

    @staticmethod
    def _display_name(user: discord.abc.User) -> str:
        if isinstance(user, discord.Member):
            return user.display_name
        return user.global_name or user.name

    @staticmethod
    def _username_handle(user: discord.abc.User) -> str:
        # Prefer modern username; fall back to name
        return user.name

    async def _resolve_member(
        self,
        user: discord.abc.User,
        guild: Optional[discord.Guild] = None,
    ) -> discord.abc.User:
        """Prefer live guild Member so guild/server PFPs are used."""
        if isinstance(user, discord.Member):
            return user
        if guild is None:
            return user
        member = guild.get_member(user.id)
        if member:
            return member
        try:
            return await guild.fetch_member(user.id)
        except (discord.HTTPException, discord.NotFound):
            log.debug(
                "Could not fetch member %s in guild %s; using user avatar",
                user.id,
                guild.id,
            )
            return user

    async def _fetch_avatar_bytes(self, user: discord.abc.User) -> Optional[bytes]:
        # display_avatar: guild avatar when Member, else global/user avatar
        asset = user.display_avatar.replace(size=1024, format="png")
        url = asset.url
        try:
            session = await self._get_session()
            async with session.get(url) as resp:
                if resp.status != 200:
                    log.warning(
                        "Avatar download HTTP %s for user %s (%s)",
                        resp.status,
                        user.id,
                        url,
                    )
                    return None
                data = await resp.read()
                if not data:
                    log.warning("Avatar download empty body for user %s", user.id)
                    return None
                return data
        except Exception:
            log.exception("Failed to download avatar for %s", user.id)
            return None

    async def _save_quote(
        self,
        *,
        guild_id: int,
        author_id: int,
        quoter_id: int,
        content: str,
        message_id: Optional[int] = None,
        channel_id: Optional[int] = None,
    ) -> int:
        cur = await self.bot.db.execute(
            "INSERT INTO quotes (guild_id, author_id, quoter_id, content, message_id, channel_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, author_id, quoter_id, content[:1900], message_id, channel_id),
        )
        return cur.lastrowid

    async def _make_card_file(
        self,
        *,
        content: str,
        author: discord.abc.User,
        quote_id: Optional[int] = None,
        guild: Optional[discord.Guild] = None,
    ) -> discord.File:
        # Prefer guild Member so current server PFP is used when available
        author = await self._resolve_member(author, guild)
        avatar = await self._fetch_avatar_bytes(author)
        if avatar is None:
            log.warning(
                "No avatar bytes for %s (%s); card will use initials placeholder",
                author.id,
                self._display_name(author),
            )
        png = build_quote_card(
            quote_text=content,
            display_name=self._display_name(author),
            username=self._username_handle(author),
            avatar_bytes=avatar,
        )
        name = f"quote-{quote_id}.png" if quote_id else "quote.png"
        return discord.File(io.BytesIO(png), filename=name)

    async def _author_for_row(self, guild: discord.Guild, author_id: int) -> discord.abc.User:
        member = guild.get_member(author_id)
        if member:
            return member
        try:
            return await guild.fetch_member(author_id)
        except (discord.HTTPException, discord.NotFound):
            pass
        try:
            return await self.bot.fetch_user(author_id)
        except discord.HTTPException:
            return self.bot.user  # type: ignore[return-value]

    # --- Context menus ---

    async def save_quote_ctx(
        self, interaction: discord.Interaction, message: discord.Message
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        content = self._quote_content(message)
        if not content:
            await interaction.response.send_message("Nothing to quote.", ephemeral=True)
            return
        qid = await self._save_quote(
            guild_id=interaction.guild.id,
            author_id=message.author.id,
            quoter_id=interaction.user.id,
            content=content,
            message_id=message.id,
            channel_id=message.channel.id,
        )
        try:
            file = await self._make_card_file(
                content=content,
                author=message.author,
                quote_id=qid,
                guild=interaction.guild,
            )
            await interaction.response.send_message(
                content=f"Quote **#{qid}** saved.",
                file=file,
            )
        except Exception:
            log.exception("Quote card failed; falling back to embed")
            embed = discord.Embed(
                title=f"Quote #{qid} saved",
                description=content[:1900],
                color=discord.Color.orange(),
            )
            embed.set_author(
                name=str(message.author),
                icon_url=message.author.display_avatar.url,
            )
            await interaction.response.send_message(embed=embed)

    async def quote_image_ctx(
        self, interaction: discord.Interaction, message: discord.Message
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        content = self._quote_content(message)
        if not content:
            await interaction.response.send_message("Nothing to quote.", ephemeral=True)
            return
        await interaction.response.defer()
        qid = await self._save_quote(
            guild_id=interaction.guild.id,
            author_id=message.author.id,
            quoter_id=interaction.user.id,
            content=content,
            message_id=message.id,
            channel_id=message.channel.id,
        )
        try:
            file = await self._make_card_file(
                content=content,
                author=message.author,
                quote_id=qid,
                guild=interaction.guild,
            )
            await interaction.followup.send(content=f"Quote **#{qid}**", file=file)
        except Exception:
            log.exception("Quote image context failed")
            await interaction.followup.send(
                f"Saved quote **#{qid}**, but image generation failed.",
                ephemeral=True,
            )

    # --- Reply trigger: "quote" / "q" ---

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        if not message.reference or not message.reference.message_id:
            return
        raw = message.content or ""
        if self.bot.user:
            raw = re.sub(rf"<@!?{self.bot.user.id}>", " ", raw)
        text = " ".join(raw.strip().lower().split())
        # Reply with quote / /quote — no links needed
        if text not in {"quote", "q", "/quote", "quote this", "/quote make"}:
            return

        try:
            ref = message.reference.resolved
            if not isinstance(ref, discord.Message):
                channel = message.channel
                ref = await channel.fetch_message(message.reference.message_id)
        except (discord.HTTPException, discord.NotFound):
            return

        content = self._quote_content(ref)
        if not content:
            await message.reply("Nothing to quote.", mention_author=False)
            return

        qid = await self._save_quote(
            guild_id=message.guild.id,
            author_id=ref.author.id,
            quoter_id=message.author.id,
            content=content,
            message_id=ref.id,
            channel_id=ref.channel.id,
        )
        try:
            file = await self._make_card_file(
                content=content,
                author=ref.author,
                quote_id=qid,
                guild=message.guild,
            )
            await message.reply(content=f"Quote **#{qid}**", file=file, mention_author=False)
        except Exception:
            log.exception("Reply quote card failed")
            await message.reply(
                f"Saved quote **#{qid}**, but image generation failed.",
                mention_author=False,
            )


    async def _resolve_qotd_channel(
        self, guild: discord.Guild
    ) -> Optional[discord.TextChannel]:
        candidates: list = []
        if config.QUOTE_OF_DAY_CHANNEL_ID:
            ch = guild.get_channel(config.QUOTE_OF_DAY_CHANNEL_ID)
            if ch:
                candidates.append(ch)
            else:
                try:
                    fetched = await guild.fetch_channel(config.QUOTE_OF_DAY_CHANNEL_ID)
                    if isinstance(fetched, discord.abc.GuildChannel):
                        candidates.append(fetched)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
        if config.PINBOARD_CHANNEL_ID:
            ch = guild.get_channel(config.PINBOARD_CHANNEL_ID)
            if ch:
                candidates.append(ch)
        if guild.system_channel:
            candidates.append(guild.system_channel)
        for name in ("general", "pinboard", "quotes", "bot-and-commands"):
            for ch in guild.text_channels:
                if ch.name.lower() == name:
                    candidates.append(ch)
        me = guild.me
        seen: set[int] = set()
        for ch in candidates:
            if not isinstance(ch, discord.TextChannel) or ch.id in seen:
                continue
            seen.add(ch.id)
            if me and ch.permissions_for(me).send_messages:
                return ch
        return None

    async def _pick_qotd_row(self, guild_id: int):
        state = await self.bot.db.fetchone(
            "SELECT last_qotd_id FROM qotd_state WHERE guild_id = ?",
            (guild_id,),
        )
        last_id = int(state["last_qotd_id"]) if state and state["last_qotd_id"] else None
        if last_id is not None:
            row = await self.bot.db.fetchone(
                "SELECT * FROM quotes WHERE guild_id = ? AND id != ? "
                "ORDER BY RANDOM() LIMIT 1",
                (guild_id, last_id),
            )
            if row:
                return row
        return await self.bot.db.fetchone(
            "SELECT * FROM quotes WHERE guild_id = ? ORDER BY RANDOM() LIMIT 1",
            (guild_id,),
        )

    async def _post_qotd(self, guild: discord.Guild, *, force: bool = False) -> bool:
        channel = await self._resolve_qotd_channel(guild)
        if channel is None:
            log.warning("QOTD: no channel for guild %s", guild.id)
            return False
        row = await self._pick_qotd_row(guild.id)
        if not row:
            log.info("QOTD: no quotes in guild %s", guild.id)
            return False
        author = await self._author_for_row(guild, row["author_id"])
        ping_role = discord.utils.get(guild.roles, name="Quoteoftheday")
        qotd_content = f"☀️ **Quote of the Day** — #{row['id']}" + (f" {ping_role.mention}" if ping_role else "")
        qotd_mentions = discord.AllowedMentions(roles=[ping_role] if ping_role else False, users=False, everyone=False)
        try:
            file = await self._make_card_file(
                content=row["content"],
                author=author,
                quote_id=row["id"],
                guild=guild,
            )
            await channel.send(
                content=qotd_content,
                allowed_mentions=qotd_mentions,
                file=file,
            )
        except Exception:
            log.exception("QOTD card failed; falling back to embed")
            await channel.send(
                content=qotd_content,
                allowed_mentions=qotd_mentions,
                embed=self._embed(row, guild),
            )
        await self.bot.db.execute(
            "INSERT INTO qotd_state (guild_id, last_qotd_id, last_posted_at) "
            "VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(guild_id) DO UPDATE SET "
            "last_qotd_id = excluded.last_qotd_id, "
            "last_posted_at = excluded.last_posted_at",
            (guild.id, int(row["id"])),
        )
        return True

    @tasks.loop(time=time(hour=9, minute=0, tzinfo=ZoneInfo("America/Chicago")))
    async def quote_of_the_day(self) -> None:
        if config.DISCORD_GUILD_ID:
            guild = self.bot.get_guild(config.DISCORD_GUILD_ID)
            if guild:
                try:
                    await self._post_qotd(guild)
                except Exception:
                    log.exception("QOTD schedule failed")
            return
        for guild in self.bot.guilds:
            try:
                await self._post_qotd(guild)
            except Exception:
                log.exception("QOTD failed for %s", guild.id)

    @quote_of_the_day.before_loop
    async def before_quote_of_the_day(self) -> None:
        await self.bot.wait_until_ready()

    # --- Slash commands ---

    quote = app_commands.Group(name="quote", description="Quote commands")

    @quote.command(
        name="this",
        description="How to quote: reply with quote (no link needed)",
    )
    async def quote_this(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "No links needed.\n\n"
            "**Easiest:** reply to their message with `quote` (or `/quote` as text)\n"
            "**Or:** right-click the message → **Apps** → **Quote**\n\n"
            "Optional fallback: `/quote make` with a message ID if you must.",
            ephemeral=True,
        )

    @quote.command(
        name="make",
        description="Optional: quote by message ID/link (prefer reply with quote)",
    )
    @app_commands.describe(
        message_id="Optional message ID or Discord message link",
        channel="Channel containing the message (defaults to current)",
    )
    async def quote_make(
        self,
        interaction: discord.Interaction,
        message_id: Optional[str] = None,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return

        if not message_id or not str(message_id).strip():
            await interaction.response.send_message(
                "Skip the link — **reply** to the message with `quote`, "
                "or right-click it → **Apps** → **Quote**.",
                ephemeral=True,
            )
            return

        raw = str(message_id).strip()
        link = re.search(
            r"discord(?:app)?\.com/channels/\d+/(\d+)/(\d+)",
            raw,
        )
        chan: Optional[discord.abc.Messageable] = channel or interaction.channel
        mid: Optional[int] = None
        if link:
            chan_id = int(link.group(1))
            mid = int(link.group(2))
            if interaction.guild:
                got = interaction.guild.get_channel(chan_id)
                if got is None:
                    try:
                        got = await interaction.guild.fetch_channel(chan_id)
                    except discord.HTTPException:
                        got = None
                if isinstance(got, discord.TextChannel):
                    chan = got
        elif raw.isdigit():
            mid = int(raw)
        else:
            await interaction.response.send_message(
                "That does not look like a message ID or link.\n"
                "Easier: reply with `quote` or Apps → Quote.",
                ephemeral=True,
            )
            return

        if not isinstance(chan, discord.TextChannel) or mid is None:
            await interaction.response.send_message(
                "Could not resolve that message.", ephemeral=True
            )
            return

        await interaction.response.defer()
        try:
            msg = await chan.fetch_message(mid)
        except (discord.NotFound, discord.HTTPException):
            await interaction.followup.send("Message not found.", ephemeral=True)
            return

        content = self._quote_content(msg)
        if not content:
            await interaction.followup.send("Nothing to quote.", ephemeral=True)
            return

        qid = await self._save_quote(
            guild_id=interaction.guild.id,
            author_id=msg.author.id,
            quoter_id=interaction.user.id,
            content=content,
            message_id=msg.id,
            channel_id=msg.channel.id,
        )
        try:
            file = await self._make_card_file(
                content=content,
                author=msg.author,
                quote_id=qid,
                guild=interaction.guild,
            )
            await interaction.followup.send(content=f"Quote **#{qid}**", file=file)
        except Exception:
            log.exception("quote make card failed")
            await interaction.followup.send(
                f"Saved quote **#{qid}**, but image generation failed.",
                ephemeral=True,
            )

    @quote.command(name="random", description="Random quote from this server")
    @app_commands.describe(user="Optional: only quotes by this user")
    async def quote_random(
        self, interaction: discord.Interaction, user: discord.Member | None = None
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        if user:
            row = await self.bot.db.fetchone(
                "SELECT * FROM quotes WHERE guild_id = ? AND author_id = ? "
                "ORDER BY RANDOM() LIMIT 1",
                (interaction.guild.id, user.id),
            )
        else:
            row = await self.bot.db.fetchone(
                "SELECT * FROM quotes WHERE guild_id = ? ORDER BY RANDOM() LIMIT 1",
                (interaction.guild.id,),
            )
        if not row:
            await interaction.response.send_message("No quotes yet.", ephemeral=True)
            return
        await interaction.response.defer()
        await self._send_quote_row(interaction, row)

    @quote.command(name="get", description="Get a quote by ID")
    @app_commands.describe(quote_id="Quote ID")
    async def quote_get(
        self, interaction: discord.Interaction, quote_id: int
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        row = await self.bot.db.fetchone(
            "SELECT * FROM quotes WHERE id = ? AND guild_id = ?",
            (quote_id, interaction.guild.id),
        )
        if not row:
            await interaction.response.send_message("Quote not found.", ephemeral=True)
            return
        await interaction.response.defer()
        await self._send_quote_row(interaction, row)

    async def _send_quote_row(
        self, interaction: discord.Interaction, row
    ) -> None:
        assert interaction.guild is not None
        author = await self._author_for_row(interaction.guild, row["author_id"])
        try:
            file = await self._make_card_file(
                content=row["content"],
                author=author,
                quote_id=row["id"],
                guild=interaction.guild,
            )
            await interaction.followup.send(
                content=f"Quote **#{row['id']}**",
                file=file,
            )
        except Exception:
            log.exception("Failed card for quote #%s", row["id"])
            await interaction.followup.send(embed=self._embed(row, interaction.guild))

    @quote.command(name="list", description="List recent quotes")
    async def quote_list(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        rows = await self.bot.db.fetchall(
            "SELECT id, author_id, content FROM quotes WHERE guild_id = ? "
            "ORDER BY id DESC LIMIT 10",
            (interaction.guild.id,),
        )
        if not rows:
            await interaction.response.send_message("No quotes yet.", ephemeral=True)
            return
        lines = []
        for r in rows:
            snippet = (r["content"][:60] + "…") if len(r["content"]) > 60 else r["content"]
            lines.append(f"`#{r['id']}` <@{r['author_id']}> — {snippet}")
        embed = discord.Embed(
            title="Recent Quotes",
            description="\n".join(lines),
            color=discord.Color.orange(),
        )
        await interaction.response.send_message(embed=embed)


    @quote.command(
        name="daily",
        description="Force-post Quote of the Day (Manage Server)",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def quote_daily(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        ok = await self._post_qotd(interaction.guild, force=True)
        if ok:
            await interaction.followup.send("Quote of the Day posted.", ephemeral=True)
        else:
            await interaction.followup.send(
                "Couldn't post — need quotes saved and a writable channel "
                "(QUOTE_OF_DAY_CHANNEL_ID / pinboard / general).",
                ephemeral=True,
            )

    @quote.command(name="delete", description="Delete a quote (author/quoter/mod)")
    @app_commands.describe(quote_id="Quote ID")
    async def quote_delete(
        self, interaction: discord.Interaction, quote_id: int
    ) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        row = await self.bot.db.fetchone(
            "SELECT * FROM quotes WHERE id = ? AND guild_id = ?",
            (quote_id, interaction.guild.id),
        )
        if not row:
            await interaction.response.send_message("Quote not found.", ephemeral=True)
            return
        is_mod = interaction.user.guild_permissions.manage_messages
        if (
            interaction.user.id not in (row["author_id"], row["quoter_id"])
            and not is_mod
        ):
            await interaction.response.send_message("Not allowed.", ephemeral=True)
            return
        await self.bot.db.execute("DELETE FROM quotes WHERE id = ?", (quote_id,))
        await interaction.response.send_message(f"Deleted quote #{quote_id}.", ephemeral=True)

    def _embed(self, row, guild: discord.Guild) -> discord.Embed:
        author = guild.get_member(row["author_id"])
        name = author.display_name if author else f"User {row['author_id']}"
        embed = discord.Embed(
            title=f"Quote #{row['id']}",
            description=row["content"],
            color=discord.Color.orange(),
        )
        embed.set_footer(text=f"— {name}")
        return embed


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Quotes(bot))
