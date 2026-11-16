"""Tactical duel rules shared by normal and crown fights."""
import random

BUILDS = {
    'balanced': ('Focus Strike', 'Steady damage; special adds 10 damage.'),
    'bruiser': ('Breaker', '+2 damage, -8 HP; special pierces Guard.'),
    'guardian': ('Second Wind', '+12 HP, -2 damage; special restores up to 22 HP.'),
    'trickster': ('Feint', '+5% crit (15% cap), -4 HP; special steals enemy focus and pierces Guard.'),
    'ranger': ('Aimed Shot', '+3 ranged damage, -18 HP; special adds 14 damage but can be blocked by Guard.'),
}
CLASS_NAMES = {'balanced': 'Duelist', 'bruiser': 'Bruiser', 'guardian': 'Guardian', 'trickster': 'Trickster', 'ranger': 'Ranger'}


def build_profile(profile, build):
    out = dict(profile)
    out['build'] = build if build in BUILDS else 'balanced'
    hp, damage = {'balanced': (0, 0), 'bruiser': (-8, 2), 'guardian': (12, -2), 'trickster': (-4, 0), 'ranger': (-18, 3)}[out['build']]
    out['max_hp'] += hp
    out['hit_min'] += damage
    out['hit_max'] += damage
    if out['build'] == 'trickster':
        out['crit_chance'] = min(.15, out.get('crit_chance', 0) + .05)
    return out


def initial():
    return {'guard': False, 'focus': 0, 'cooldown': 0, 'last': None}


def act(profile, attacker, defender, action, hp, max_hp, miss=0, rng=random):
    """Mutate tactical state, returning damage, healing and a display note."""
    if action == 'special' and attacker['cooldown']:
        raise ValueError('Special is cooling down. Use Attack or Guard.')
    if action == 'guard' and attacker['last'] == 'guard':
        raise ValueError('You cannot Guard twice in a row.')
    if action not in ('attack', 'guard', 'special'):
        raise ValueError('Unknown move.')
    attacker['guard'] = False
    attacker['cooldown'] = max(0, attacker['cooldown'] - 1)
    attacker['last'] = action
    if action == 'guard':
        attacker['guard'] = True
        attacker['focus'] = 5
        return 0, 0, 'guards: 60% damage reduction until their next turn; next strike +5'
    build = profile.get('build', 'balanced')
    if action == 'special':
        attacker['cooldown'] = 3
        if build == 'guardian':
            healing = min(22, max_hp - hp)
            return 0, healing, f'uses Second Wind: +{healing} HP'
    if miss and rng.random() < miss:
        attacker['focus'] = 0
        defender['guard'] = False
        return 0, 0, 'misses in the smoke'
    # Narrow damage variation so decisions matter more than a lucky roll.
    middle = (profile['hit_min'] + profile['hit_max']) // 2
    damage = rng.randint(middle - 1, middle + 1) + attacker['focus']
    attacker['focus'] = 0
    pierce = False
    if action == 'special':
        if build == 'balanced':
            damage += 10
        elif build == 'ranger':
            damage += 14
        elif build == 'bruiser':
            damage += 4
            pierce = True
        elif build == 'trickster':
            damage += defender['focus'] + 2
            defender['focus'] = 0
            pierce = True
    crit = rng.random() < min(.15, profile.get('crit_chance', 0))
    if crit:
        damage = int(damage * 1.35)
    blocked = defender['guard'] and not pierce
    if blocked:
        damage = max(1, int(damage * .4))
    defender['guard'] = False
    name = BUILDS[build][0] if action == 'special' else ('Ranged Shot' if build == 'ranger' else 'Attack')
    return damage, 0, f'uses {name}: {damage} damage' + (' (critical)' if crit else '') + (' (guarded)' if blocked else '')
