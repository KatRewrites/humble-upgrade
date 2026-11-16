"""Opt-in notifications, daily questions, chat revive and balance title."""
import asyncio
import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config

log = logging.getLogger('friend-bot.community')
NOTIFICATIONS = ['BotUpdates', 'Quoteoftheday', 'Announcements', 'QuestionoftheDay', 'ChatRevive']
QUESTIONS = [
    'What game would you erase from your memory just to play it fresh?',
    'What tiny inconvenience is your personal villain origin story?',
    'What food do you defend even though everyone roasts you for it?',
    'What skill would you download into your brain instantly?',
    'What fictional world would you survive in for exactly ten minutes?',
    'What song are you legally required to turn up?',
    'What is your most harmless unpopular opinion?',
    'What is something good that happened to you this week?',
    'What game has the best soundtrack?',
    'Which superpower sounds great until you think about the downsides?',
    'What hobby would you try if money and time were no problem?',
    'What is the weirdest thing you believed as a kid?',
    'What movie deserves a better ending?',
    'What snack belongs in every gaming session?',
    'What is your most satisfying gaming achievement?',
    'What would you name a pet dragon?',
    'What fictional character would be the worst roommate?',
    'What would your completely useless superpower be?',
    'What is something you changed your mind about recently?',
    'What game mechanic do you wish more games used?',
    'What song instantly reminds you of a specific place?',
    'What is your favorite way to spend a rainy day?',
    'What is the funniest bug you have seen in a game?',
    'What is a small thing you are looking forward to?',
    'Which animal would be the most annoying if it could talk?',
    'What meal could you cook to impress the group?',
    'What is your dream co-op game?',
    'What would your personal loading-screen tip say?',
    'What is one thing you wish people asked you about more?',
    'What should we play together next?',
]


async def ensure_role(guild, name):
    role = discord.utils.get(guild.roles, name=name)
    if role is None:
        role = await guild.create_role(name=name, permissions=discord.Permissions.none(),
            colour=discord.Colour.default(), mentionable=False, reason='Humble Upgrade notification/title role')
    if role.managed or role >= guild.me.top_role or role.permissions.value:
        raise ValueError(f'{name} must be a non-privileged role below the bot.')
    return role


class RoleMenu(discord.ui.View):
    def __init__(self, owner, roles):
        super().__init__(timeout=180)
        self.owner = owner
        self.roles = roles
        self.lock = asyncio.Lock()
        select = discord.ui.Select(placeholder='Choose your notification roles', min_values=0,
            max_values=len(roles), options=[discord.SelectOption(label=r.name, value=str(r.id), default=r in owner.roles) for r in roles])
        async def selected(interaction):
            if interaction.user.id != owner.id:
                await interaction.response.send_message('Open your own `/roles` menu.', ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            async with self.lock:
                wanted = {int(value) for value in select.values}
                try:
                    for role in self.roles:
                        if role.id in wanted:
                            await interaction.user.add_roles(role, reason='Opted into notifications')
                        else:
                            await interaction.user.remove_roles(role, reason='Opted out of notifications')
                    await interaction.followup.send('Notification roles updated.', ephemeral=True)
                except discord.HTTPException:
                    await interaction.followup.send('Could not update every role. Check the bot’s role permissions and try again.', ephemeral=True)
        select.callback = selected
        self.add_item(select)


class Community(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.post_lock = asyncio.Lock()

    async def cog_load(self):
        await self.bot.db.execute('CREATE TABLE IF NOT EXISTS community_posts(guild_id INTEGER, kind TEXT, posted_at TEXT, PRIMARY KEY(guild_id,kind))')
        self.daily_question.start()
        self.balance_title.start()

    async def cog_unload(self):
        self.daily_question.cancel()
        self.balance_title.cancel()

    @app_commands.command(name='roles', description='Choose which bot notifications you want')
    @app_commands.guild_only()
    async def roles(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            roles = [await ensure_role(interaction.guild, name) for name in NOTIFICATIONS]
        except (discord.HTTPException, ValueError) as exc:
            await interaction.followup.send(f'Role setup needs Manage Roles and roles below the bot. {exc}', ephemeral=True)
            return
        await interaction.followup.send('Select all notifications you want; clear the menu to opt out of all five.', view=RoleMenu(interaction.user, roles), ephemeral=True)

    @tasks.loop(time=time(hour=12, tzinfo=ZoneInfo('America/Chicago')))
    async def daily_question(self):
        for guild in self.bot.guilds:
            if config.DISCORD_GUILD_ID and guild.id != config.DISCORD_GUILD_ID:
                continue
            try:
                await self.post_question(guild)
            except Exception:
                log.exception('Daily question failed')

    @daily_question.before_loop
    async def before_question(self):
        await self.bot.wait_until_ready()

    async def post_question(self, guild):
        async with self.post_lock:
            today = datetime.now(ZoneInfo('America/Chicago')).date()
            row = await self.bot.db.fetchone('SELECT posted_at FROM community_posts WHERE guild_id=? AND kind=?', (guild.id, 'question'))
            if row and row['posted_at'] == today.isoformat():
                return False
            channel = guild.get_channel(config.MEME_CHANNEL_ID or 0)
            if channel is None:
                return False
            role = await ensure_role(guild, 'QuestionoftheDay')
            question = QUESTIONS[today.toordinal() % len(QUESTIONS)]
            await channel.send(f'{role.mention} **Question of the Day**\n{question}', allowed_mentions=discord.AllowedMentions(roles=[role], users=False, everyone=False))
            await self.bot.db.execute('INSERT INTO community_posts VALUES (?,?,?) ON CONFLICT(guild_id,kind) DO UPDATE SET posted_at=excluded.posted_at', (guild.id, 'question', today.isoformat()))
            return True

    @app_commands.command(name='questionoftheday', description='Post today’s discussion question (once per day)')
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_messages=True)
    async def question(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        posted = await self.post_question(interaction.guild)
        await interaction.followup.send('Question posted.' if posted else 'Already posted today, or general channel unavailable.', ephemeral=True)

    @app_commands.command(name='chatrevive', description='Invite opted-in members back to chat (6-hour cooldown)')
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_messages=True)
    async def revive(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        async with self.post_lock:
            row = await self.bot.db.fetchone("SELECT 1 FROM community_posts WHERE guild_id=? AND kind='revive' AND datetime(posted_at)>datetime('now','-6 hours')", (interaction.guild_id,))
            if row:
                await interaction.followup.send('ChatRevive is on its six-hour cooldown.', ephemeral=True)
                return
            role = await ensure_role(interaction.guild, 'ChatRevive')
            await interaction.channel.send(f'{role.mention} Roll call. What are we all up to?', allowed_mentions=discord.AllowedMentions(roles=[role], users=False, everyone=False))
            await self.bot.db.execute("INSERT INTO community_posts VALUES (?,'revive',datetime('now')) ON CONFLICT(guild_id,kind) DO UPDATE SET posted_at=excluded.posted_at", (interaction.guild_id,))
            await interaction.followup.send('ChatRevive sent.', ephemeral=True)

    @app_commands.command(name='notify', description='Post an opt-in announcement or bot update')
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.choices(kind=[app_commands.Choice(name='Bot update', value='BotUpdates'), app_commands.Choice(name='Announcement', value='Announcements')])
    async def notify(self, interaction: discord.Interaction, kind: app_commands.Choice[str], text: app_commands.Range[str, 1, 1800]):
        await interaction.response.defer(ephemeral=True)
        channel = interaction.guild.get_channel(config.EVENT_ANNOUNCE_CHANNEL_ID or 0)
        if channel is None:
            await interaction.followup.send('The announcement channel is unavailable.', ephemeral=True)
            return
        role = await ensure_role(interaction.guild, kind.value)
        await channel.send(f'{role.mention}\n{text}', allowed_mentions=discord.AllowedMentions(roles=[role], users=False, everyone=False))
        await interaction.followup.send('Posted.', ephemeral=True)

    @tasks.loop(minutes=5)
    async def balance_title(self):
        for guild in self.bot.guilds:
            if config.DISCORD_GUILD_ID and guild.id != config.DISCORD_GUILD_ID:
                continue
            try:
                for name in NOTIFICATIONS:
                    await ensure_role(guild, name)
                rows = await self.bot.db.fetchall('SELECT user_id,balance FROM users ORDER BY balance DESC')
                eligible = [(guild.get_member(r['user_id']), r['balance']) for r in rows]
                eligible = [(member, balance) for member, balance in eligible if member and not member.bot]
                if not eligible:
                    continue
                high = eligible[0][1]
                winners = [m for m, balance in eligible if balance == high]
                role = await ensure_role(guild, 'KingOfGambling')
                # Grant the new holder(s) before removing the previous title.
                for member in winners:
                    if role not in member.roles:
                        await member.add_roles(role, reason='Highest current ZAMN balance')
                ids = {m.id for m in winners}
                for member in list(role.members):
                    if member.id not in ids:
                        await member.remove_roles(role, reason='Balance leaderboard title transferred')
            except Exception:
                log.exception('KingOfGambling role update failed')

    @balance_title.before_loop
    async def before_balance(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Community(bot))
