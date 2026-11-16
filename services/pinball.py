"""Turn-based pinball rules; no Discord or live database access."""
from dataclasses import dataclass
import random


@dataclass
class Table:
    score: int = 0
    ball: int = 1
    hits: int = 0
    combo: int = 0
    nudges: int = 0
    lane: str = 'launch'
    power: str = 'medium'
    done: bool = False
    note: str = 'Three balls. Pick launch power. Match the flipper to the lit lane.'

    def drain(self, reason):
        self.ball += 1
        self.combo = self.hits = self.nudges = 0
        self.done = self.ball > 3
        self.lane = 'done' if self.done else 'launch'
        self.note = reason + (' Game over!' if self.done else ' Launch your next ball.')

    def play(self, action, rng=random):
        if self.done:
            return False
        if self.lane == 'launch':
            if action not in ('soft', 'medium', 'full'):
                return False
            self.power = action
            self.lane = rng.choice(('left', 'right'))
            self.note = f'{action.title()} launch! Use the {self.lane} flipper.'
            return True
        if action not in ('left', 'right', 'nudge'):
            return False
        if action == 'nudge':
            self.nudges += 1
            if self.nudges >= 3:
                self.drain('TILT! Three nudges cost you the ball.')
            elif self.lane == 'outlane':
                self.lane = rng.choice(('left', 'right'))
                self.note = f'Saved! Use the {self.lane} flipper. Nudge {self.nudges}/2.'
            else:
                self.note = f'Unnecessary nudge! Still heading {self.lane}. Nudge {self.nudges}/2.'
            return True
        if action != self.lane:
            self.drain('Wrong flipper — the ball drained!')
            return True
        self.hits += 1
        self.combo += 1
        base = {'soft': 80, 'medium': 120, 'full': 180}[self.power]
        points = base * min(self.combo, 4)
        self.score += points
        if self.hits >= 6:
            self.score += 500
            self.drain('Six-hit combo! Ramp bonus +500, then the ball drained.')
        else:
            risk = {'soft': .10, 'medium': .20, 'full': .35}[self.power]
            self.lane = 'outlane' if rng.random() < risk else rng.choice(('left', 'right'))
            self.note = f'Bumper +{points}! Combo ×{min(self.combo, 4)}. '
            self.note += 'OUTLANE — nudge to save!' if self.lane == 'outlane' else f'Use the {self.lane} flipper.'
        return True
