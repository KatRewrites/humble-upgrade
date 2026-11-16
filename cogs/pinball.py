"""Free pinball, animated turns, persistent scores and capped rewards."""
import asyncio
import logging
import time
import uuid

import discord
from discord import app_commands
from discord.ext import commands

import config
from services.pinball import Table

log = logging.getLogger('friend-bot.pinball')


class PinballView(discord.ui.View):
    def __init__(self, cog, user, guild_id):
        super().__init__(timeout=90)
        self.cog, self.user, self.guild_id = cog, user, guild_id
        self.table = Table()
        self.session = uuid.uuid4().hex
        self.lock = asyncio.Lock()
        self.message = None
        self.expires = time.monotonic() + 600
        self.reward = None
        self.revision = 0
        self.again = None
        self.controls()

    def controls(self):
        self.clear_items()
        if self.table.done:
            return
        actions = ('soft', 'medium', 'full') if self.table.lane == 'launch' else ('left', 'right', 'nudge')
        for action in actions:
            button = discord.ui.Button(label=action.title(), style=discord.ButtonStyle.primary)
            async def callback(interaction, action=action):
                await self.move(interaction, action)
            button.callback = callback
            self.add_item(button)

    def embed(self, moving=False):
        t = self.table
        left = '●' if t.lane == 'left' else ' '
        right = '●' if t.lane == 'right' else ' '
        board = f'╔═══════════════╗\n║  ◇  RAMP  ◇   ║\n║   ○   ○   ○   ║\n║ {left}           {right} ║\n║    ╱     ╲    ║\n╚═════  ▽  ═════╝'
        description = f'```\n{board}\n```\n**Score: {t.score:,}** · Ball {min(t.ball, 3)}/3\n'
        description += '⚡ Ball bouncing through the bumpers…' if moving else t.note
        if t.done:
            description += f'\nReward: **{self.reward or 0} {config.CURRENCY_TICKER}**'
        else:
            description += '\nSoft: safer / fewer points · Full: riskier / more points.'
        embed = discord.Embed(title='🎯 Pinball', description=description, color=discord.Color.gold())
        embed.set_author(name=self.user.display_name)
        embed.set_footer(text=f'Free play · max {config.PINBALL_REWARD_PER_GAME} $ZAMN/game, {config.PINBALL_REWARD_PER_DAY}/day UTC · 90s idle timeout')
        return embed

    async def finish(self, reward=True):
        self.table.done = True
        self.controls()
        self.stop()
        self.cog.active.pop((self.guild_id, self.user.id), None)
        self.reward = await self.cog.save(self, reward)
        # Play again: re-runs /pinball play itself (active-table limit, daily cap in save).
        from cogs.games import PlayAgainView

        cog = self.cog
        self.again = PlayAgainView(
            cog.bot, self.user.id, lambda i: Pinball.play.callback(cog, i), label="Play again",
        ).bind(message=self.message)

    def final_view(self):
        return self.again if self.table.done and self.again is not None else self

    async def move(self, interaction, action):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message('Start your own table with `/pinball play`.', ephemeral=True)
            return
        if self.lock.locked() or self.table.done:
            await interaction.response.send_message('Wait for the ball to settle.', ephemeral=True)
            return
        revision = self.revision
        await interaction.response.defer()
        async with self.lock:
            if self.table.done or revision != self.revision:
                return
            self.revision += 1
            if time.monotonic() > self.expires:
                self.table.note = 'Table timed out. Score saved; no reward.'
                await self.finish(False)
            else:
                self.table.play(action)
                if self.table.done:
                    await self.finish()
                else:
                    self.controls()
                    try:
                        await interaction.edit_original_response(embed=self.embed(True), view=None)
                        await asyncio.sleep(.6)
                    except discord.HTTPException:
                        pass
            if self.again is not None:
                self.again.bind(interaction=interaction)
            await interaction.edit_original_response(embed=self.embed(), view=self.final_view())

    async def on_timeout(self):
        async with self.lock:
            if self.table.done:
                return
            self.table.note = 'Table timed out. Score saved; no reward.'
            await self.finish(False)
            if self.message:
                try:
                    await self.message.edit(embed=self.embed(), view=self.final_view())
                except discord.HTTPException:
                    pass


class Pinball(commands.Cog):
    pinball = app_commands.Group(name='pinball', description='Arcade pinball: free play and high scores', guild_only=True)

    def __init__(self, bot):
        self.bot = bot
        self.active = {}

    async def cog_load(self):
        await self.bot.db.execute('''CREATE TABLE IF NOT EXISTS pinball_runs (
            session TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            score INTEGER NOT NULL, reward INTEGER NOT NULL,
            played_at TEXT NOT NULL DEFAULT (datetime('now')))''')

    async def save(self, view, reward=True):
        db = self.bot.db
        # Record game and credit reward in one transaction; duplicate saves are harmless.
        async with db._lock:
            try:
                row = await db.fetchone('SELECT reward FROM pinball_runs WHERE session=?', (view.session,))
                if row:
                    return int(row['reward'])
                row = await db.fetchone("SELECT COALESCE(SUM(reward),0) AS total FROM pinball_runs WHERE user_id=? AND date(played_at)=date('now')", (view.user.id,))
                credit = min(
                    config.PINBALL_REWARD_PER_GAME,
                    view.table.score // config.PINBALL_SCORE_DIVISOR,
                    max(0, config.PINBALL_REWARD_PER_DAY - int(row['total'])),
                ) if reward else 0
                await db.conn.execute('INSERT INTO pinball_runs(session,guild_id,user_id,score,reward) VALUES (?,?,?,?,?)',
                    (view.session, view.guild_id, view.user.id, view.table.score, credit))
                if credit:
                    await db.conn.execute('INSERT OR IGNORE INTO users(user_id,balance) VALUES (?,?)', (view.user.id, config.STARTING_BALANCE))
                    await db.conn.execute('UPDATE users SET balance=balance+? WHERE user_id=?', (credit, view.user.id))
                    row = await db.fetchone('SELECT balance FROM users WHERE user_id=?', (view.user.id,))
                    await db.conn.execute('INSERT INTO transactions(user_id,amount,reason,balance_after) VALUES (?,?,?,?)',
                        (view.user.id, credit, 'pinball score reward', row['balance']))
                await db.conn.commit()
                return credit
            except Exception:
                await db.conn.rollback()
                raise

    @pinball.command(name='play', description='Three balls, flippers, nudges and combos — free play')
    async def play(self, interaction: discord.Interaction):
        key = (interaction.guild_id, interaction.user.id)
        if key in self.active:
            await interaction.response.send_message('Finish your current pinball table first.', ephemeral=True)
            return
        view = PinballView(self, interaction.user, interaction.guild_id)
        self.active[key] = view
        try:
            await interaction.response.send_message(embed=view.embed(), view=view)
            view.message = await interaction.original_response()
        except Exception:
            self.active.pop(key, None)
            view.stop()
            raise

    @pinball.command(name='leaderboard', description='Top pinball scores in this server')
    async def leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer()
        rows = await self.bot.db.fetchall('SELECT user_id,MAX(score) AS best FROM pinball_runs WHERE guild_id=? GROUP BY user_id ORDER BY best DESC,user_id LIMIT 10', (interaction.guild_id,))
        lines = [f'**{i}.** <@{r["user_id"]}> — **{r["best"]:,}**' for i, r in enumerate(rows, 1)]
        await interaction.followup.send(embed=discord.Embed(title='🎯 Pinball high scores', description='\n'.join(lines) or 'No scores yet. Try `/pinball play`!'), allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(Pinball(bot))
