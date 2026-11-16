"""Weekly duel crown and free, accepted tiebreaker fights."""
import asyncio
import json
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from cogs.community import ensure_role
from cogs.duels import DuelFightView
from services import duel_crown as rules

log = logging.getLogger('friend-bot.duel-crown')


class CrownFight(DuelFightView):
    async def _finish(self, interaction, *, winner, loser, tie=False):
        self.done = True
        self.stop()
        self.clear_items()
        try:
            if not tie:
                await self.crown.resolve(self.guild_id, self.week, winner.id, loser.id)
            text = 'Tiebreaker tied — schedule a rematch.' if tie else f'**{winner.display_name}** wins the tiebreaker! Check `/duelcrown status` for the next match.'
            await interaction.edit_original_response(content=text, embed=self.build_embed(), view=self)
        finally:
            self.crown.active.discard(self.match_key)

    async def _timeout(self):
        if self.done:
            return
        self.done = True
        self.stop()
        self.crown.active.discard(self.match_key)
        if self.message:
            try:
                await self.message.edit(content='Tiebreaker timed out. No winner recorded; use `/duelcrown fight` to retry.', embed=None, view=None)
            except discord.HTTPException:
                pass


class CrownChallenge(discord.ui.View):
    def __init__(self, cog, first, second, week, match_key):
        super().__init__(timeout=120)
        self.cog, self.first, self.second = cog, first, second
        self.week, self.match_key = week, match_key
        self.done = False
        self.message = None

    async def on_timeout(self):
        if not self.done:
            self.done = True
            self.cog.active.discard(self.match_key)
            if self.message:
                try:
                    await self.message.edit(content='Crown challenge expired. The match is still pending.', view=None)
                except discord.HTTPException:
                    pass

    @discord.ui.button(label='Accept crown battle', style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.second.id or self.done:
            await interaction.response.send_message('Only the challenged player can accept this pending match.', ephemeral=True)
            return
        self.done = True
        self.stop()
        await interaction.response.defer()
        try:
            fight = await CrownFight.create(self.cog.bot, self.first, self.second, 0, interaction.guild_id)
            fight.crown, fight.week, fight.match_key = self.cog, self.week, self.match_key
            fight.message = interaction.message
            await interaction.edit_original_response(content='**KingOfDuels tiebreaker** · no wager, no weekly-win or JP rewards', embed=fight.build_embed(), view=fight)
        except Exception:
            self.cog.active.discard(self.match_key)
            log.exception('Could not start crown fight')
            await interaction.followup.send('The match could not start. It remains pending; please retry.', ephemeral=True)

    @discord.ui.button(label='Not now', style=discord.ButtonStyle.secondary)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.second.id or self.done:
            await interaction.response.send_message('Only the challenged player can decline.', ephemeral=True)
            return
        self.done = True
        self.stop()
        self.cog.active.discard(self.match_key)
        await interaction.response.edit_message(content='Challenge declined. The bracket match remains pending.', view=None)


class DuelCrown(commands.Cog):
    duelcrown = app_commands.Group(name='duelcrown', description='Weekly duel standings and tiebreaker battles', guild_only=True)

    def __init__(self, bot):
        self.bot = bot
        self.lock = asyncio.Lock()
        self.active = set()

    async def cog_load(self):
        await rules.setup(self.bot.db)
        self.close_weeks.start()

    async def cog_unload(self):
        self.close_weeks.cancel()

    async def settle_weeks(self, guild):
        async with self.lock:
            weeks = await self.bot.db.fetchall('SELECT DISTINCT week FROM duel_weekly WHERE guild_id=? AND week<? ORDER BY week', (guild.id, rules.week_key()))
            for row in weeks:
                week = row['week']
                existing = await self.bot.db.fetchone('SELECT 1 FROM duel_crowns WHERE guild_id=? AND week=?', (guild.id, week))
                if existing:
                    continue
                scores = await self.bot.db.fetchall('SELECT user_id,wins FROM duel_weekly WHERE guild_id=? AND week=? ORDER BY wins DESC,user_id', (guild.id, week))
                leaders = [r['user_id'] for r in scores if r['wins'] == scores[0]['wins']]
                await self.bot.db.execute('INSERT OR IGNORE INTO duel_crowns(guild_id,week,state) VALUES (?,?,?)', (guild.id, week, json.dumps(rules.bracket(leaders))))
        await self.sync_role(guild)

    async def sync_role(self, guild):
        role = await ensure_role(guild, 'KingOfDuels')
        rows = await self.bot.db.fetchall('SELECT week,state FROM duel_crowns WHERE guild_id=? ORDER BY week DESC', (guild.id,))
        # While a new crown is contested, keep the latest completed winner.
        result = next(((r['week'], json.loads(r['state'])['champion']) for r in rows if json.loads(r['state'])['champion']), None)
        if not result:
            return
        week, uid = result
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except discord.NotFound:
                return
        if role not in member.roles:
            await member.add_roles(role, reason=f'KingOfDuels for week {week}')
        for previous in list(role.members):
            if previous.id != uid:
                await previous.remove_roles(role, reason='Weekly duel crown transferred')
        await self.bot.db.execute('UPDATE duel_crowns SET awarded=1 WHERE guild_id=? AND week=?', (guild.id, week))

    async def resolve(self, guild_id, week, winner, loser):
        async with self.lock:
            row = await self.bot.db.fetchone('SELECT state FROM duel_crowns WHERE guild_id=? AND week=?', (guild_id, week))
            state = rules.advance(json.loads(row['state']), winner, loser)
            await self.bot.db.execute('UPDATE duel_crowns SET state=? WHERE guild_id=? AND week=?', (json.dumps(state), guild_id, week))
        try:
            await self.sync_role(self.bot.get_guild(guild_id))
        except Exception:
            log.exception('Crown saved; role assignment will retry')

    @tasks.loop(minutes=5)
    async def close_weeks(self):
        for guild in self.bot.guilds:
            if config.DISCORD_GUILD_ID and guild.id != config.DISCORD_GUILD_ID:
                continue
            try:
                await self.settle_weeks(guild)
            except Exception:
                log.exception('Weekly crown check failed')

    @close_weeks.before_loop
    async def before_close(self):
        await self.bot.wait_until_ready()

    @duelcrown.command(name='status', description='Weekly wins and pending crown battles')
    async def status(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.settle_weeks(interaction.guild)
        scores = await self.bot.db.fetchall('SELECT user_id,wins FROM duel_weekly WHERE guild_id=? AND week=? ORDER BY wins DESC,user_id LIMIT 10', (interaction.guild_id, rules.week_key()))
        lines = [f'Week of **{rules.week_key()}** · Monday–Sunday Central', 'Completed wins tracked from this update onward.']
        lines += [f'{i}. <@{r["user_id"]}> — {r["wins"]} wins' for i, r in enumerate(scores, 1)]
        rows = await self.bot.db.fetchall('SELECT week,state FROM duel_crowns WHERE guild_id=? ORDER BY week DESC LIMIT 4', (interaction.guild_id,))
        for row in rows:
            state = json.loads(row['state'])
            if state['champion']:
                lines.append(f'**{row["week"]} winner:** <@{state["champion"]}>')
            else:
                lines.append(f'**{row["week"]} tiebreakers:**')
                lines += [f'<@{a}> vs <@{b}>' for a, b in state['pairs']]
                if state['winners']:
                    lines.append('Advanced / bye: ' + ', '.join(f'<@{uid}>' for uid in state['winners']))
        lines.append('Tied players: `/duelcrown fight` sends your pending opponent an acceptance request.')
        await interaction.followup.send(embed=discord.Embed(title='👑 KingOfDuels', description='\n'.join(lines)[:4000]), allowed_mentions=discord.AllowedMentions.none())

    @duelcrown.command(name='fight', description='Challenge your assigned tiebreaker opponent — no wager')
    async def fight(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.settle_weeks(interaction.guild)
        async with self.lock:
            rows = await self.bot.db.fetchall('SELECT week,state FROM duel_crowns WHERE guild_id=? ORDER BY week DESC', (interaction.guild_id,))
            match = next(((r['week'], pair) for r in rows for pair in json.loads(r['state'])['pairs'] if interaction.user.id in pair), None)
            if not match:
                await interaction.followup.send('You have no pending tiebreaker match.', ephemeral=True)
                return
            week, pair = match
            key = (interaction.guild_id, week, *sorted(pair))
            if key in self.active:
                await interaction.followup.send('That match already has an active challenge or fight.', ephemeral=True)
                return
            self.active.add(key)
        try:
            uid = next(uid for uid in pair if uid != interaction.user.id)
            opponent = interaction.guild.get_member(uid) or await interaction.guild.fetch_member(uid)
            view = CrownChallenge(self, interaction.user, opponent, week, key)
            view.message = await interaction.followup.send(f'👑 {opponent.mention}: {interaction.user.mention} challenges you for the weekly crown. No wager. Your build and loadout lock on acceptance.', view=view, wait=True,
                allowed_mentions=discord.AllowedMentions(users=[opponent], roles=False, everyone=False))
        except Exception:
            self.active.discard(key)
            raise


async def setup(bot):
    await bot.add_cog(DuelCrown(bot))
