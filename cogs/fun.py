"""Fun utilities: 8ball and help overview."""
from __future__ import annotations

import random

import discord
from discord import app_commands
from discord.ext import commands

import config

EIGHTBALL = [
    "It is certain.",
    "Without a doubt.",
    "You may rely on it.",
    "Yes — definitely.",
    "As I see it, yes.",
    "Most likely.",
    "Outlook good.",
    "Signs point to yes.",
    "Reply hazy, try again.",
    "Ask again later.",
    "Better not tell you now.",
    "Cannot predict now.",
    "Concentrate and ask again.",
    "Don't count on it.",
    "My reply is no.",
    "My sources say no.",
    "Outlook not so good.",
    "Very doubtful.",
]


class Fun(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="8ball", description="Ask the magic 8-ball")
    @app_commands.describe(question="Your question")
    async def eightball(self, interaction: discord.Interaction, question: str) -> None:
        q = (question or "").strip()
        if not q:
            await interaction.response.send_message(
                "Ask an actual question.", ephemeral=True
            )
            return
        embed = discord.Embed(
            title="🎱 Magic 8-Ball",
            color=discord.Color.dark_blue(),
        )
        embed.add_field(name="Question", value=q[:500], inline=False)
        embed.add_field(name="Answer", value=random.choice(EIGHTBALL), inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="help", description="Overview of Humble Upgrade commands")
    async def help_cmd(self, interaction: discord.Interaction) -> None:
        in_dm = interaction.guild is None
        embed = discord.Embed(
            title="Humble Upgrade Help",
            description=(
                f"Chaos dealer: {config.CURRENCY_NAME}, games, social, memes."
                + ("\n*(You're in DMs — most play commands are in the server.)*" if in_dm else "")
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Economy",
            value=(
                "/balance /give /leaderboard /daily /inventory /shop /use-ticket /redeem-role "
                "/profile view|flair|title "
                "/borrow /debt /paydebt /autopay /refinance — credit (50k lifetime earned · 7d term · 2%/day after due). "
                "— +10 per chat message (not in gamba). "
                "Shop duel boosts (adrenaline/bandage/smoke) last ONE fight via /journey loadout. "
                "High-roller shelf: streak shield, chat/payday boosts, gamba insurance, bail voucher, "
                "flair, titles, status roles."
            ),
            inline=False,
        )
        embed.add_field(
            name="Games",
            value=(
                "/guess /slots /rps (rare Shoot) /tictactoe "
                "/blackjack (max 100k · Play again) /poker "
                "/higherlower (100k+ · min 3k) /crash (100k+ · min 5k) "
                "/deathroll (1M+ table · 100k–500k · bust odds climb each roll · no refunds) "
                "/pinball play|leaderboard /duel "
                "(Attack / Guard / Special) /duelclass /duelcrown status|fight "
                "/journey view|upgrade|respec|loadout /quests /quest claim. "
                f"Lost solo wagers refund {config.loss_refund_pct()}% (PvP: winner takes the pot, no refund). "
                f"Wealth tax: {config.WEALTH_TAX_RATE:.0%}/day on the part of a balance above "
                f"{config.WEALTH_TAX_THRESHOLD:,}."
            ),
            inline=False,
        )
        embed.add_field(
            name="Social & Fun",
            value=(
                "/ship /hug /headpat /cuddle /kiss /marry /divorce /relationship /polycule /prefs set "
                "/8ball /factcheck /quote * — reply quote or Apps **Quote** for cards. "
                "Daily QOTD ~9:00 CT · `/quote daily` (Manage Server)."
            ),
            inline=False,
        )
        embed.add_field(
            name="Kids",
            value=(
                "/kid adopt — married couple adopts a kid (spouse confirms) "
                "· /kid status [kid] [user] — kid card (age, meters, piggy bank, milestones) or list "
                "· /kid feed|play|nap|hug — care (1–2h cooldowns) · /kid gift — wallet → piggy bank "
                "· /kid nickname · /kid rename (parents / custody holder, 24h cooldown)."
            ),
            inline=False,
        )
        embed.add_field(
            name="Chaos extras",
            value=(
                "/roast opt-in|opt-out|user|meter — both sides opt-in. "
                "/crew set|show|leave|leaderboard — duo tags (rank: duel wins then $ZAMN). "
                "/deathmessage set|clear|show — personal loss line. "
                "/funroles setup — opt-in reaction roles (Manage Server + Manage Roles)."
            ),
            inline=False,
        )
        embed.add_field(
            name="Court",
            value=(
                "/sue @member amount reason [charge] [sentence] — fee, jury votes 10 min (min 3, parties/lawyers can't vote). "
                "Sentences: jail · restraining order · community service · public apology · double or nothing. "
                "Charges incl. Ghosting, Rizz violation, Stealing my man/woman, Divorce (married), Custody (shared kid). "
                "Case buttons: Settle · Countersue · Hire lawyer / Public defender · Objection! · Bribe (risky). "
                "/appeal /case /docket /record · /court guide|apologize|bail · Server owner: /judge rule|dismiss|pardon|bail|contempt."
            ),
            inline=False,
        )
        embed.add_field(
            name="Weekend events",
            value=(
                "/event status|leaderboard|duel-join|duel-bracket — "
                "King of Gamba (net wager games) + Duel Champ (most duel wins) "
                "Fri 18:00–Sun 23:59 CT · title roles + 500 $ZAMN."
            ),
            inline=False,
        )
        embed.add_field(
            name="Notifications & conversation",
            value=("/roles — opt into BotUpdates, Quoteoftheday, Announcements, QuestionoftheDay, ChatRevive. "
                   "Daily question at noon CT. Staff: /questionoftheday /chatrevive /notify. "
                   "KingOfGambling follows the highest balance. Mention/reply to chat locally; "
                   "Apps → Translate or /translate for words automatic detection misses."),
            inline=False,
        )
        embed.add_field(
            name="Memes",
            value=(
                "/meme — random SFW meme (replies in this channel). "
                "Rare spontaneous SFW memes post in the configured meme channel. Memes flagged NSFW by the API are skipped."
            ),
            inline=False,
        )
        embed.add_field(
            name="Utility",
            value="/status /afk /timezone set /time /compare /pinboard /translate (also auto on foreign-language chat) /log — Manage Server: /backup create|list|restore",
            inline=False,
        )
        embed.set_footer(text="No automod. Refunds DM you. /help works in DMs. /maintenance when updating.")
        await interaction.response.send_message(embed=embed, ephemeral=not in_dm)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Fun(bot))
