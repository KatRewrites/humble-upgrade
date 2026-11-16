"""Trivia bank + payout math (pure logic)."""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Optional

FREE_REWARD = 100          # no bet: correct answer pays this
FREE_DAILY_CAP = 25        # free-play wins that pay per user per day (anti-farm)
MAX_BET = 10_000           # bet mode: win +bet, lose bet
COOLDOWN_SECONDS = 30
ANSWER_SECONDS = 20
RECENT_MEMORY = 40


@dataclass(frozen=True)
class Question:
    cat: str
    q: str
    choices: tuple[str, str, str, str]
    answer: int  # index into choices


def _Q(cat, q, right, *wrong) -> Question:
    assert len(wrong) == 3
    return Question(cat, q, (right, *wrong), 0)


BANK: list[Question] = [
    # --- IT / tech ---
    _Q("IT", "What does DNS mainly do?", "Turns domain names into IP addresses", "Encrypts web traffic", "Assigns IP addresses automatically", "Blocks malware"),
    _Q("IT", "Which protocol hands out IP addresses automatically?", "DHCP", "DNS", "FTP", "SNMP"),
    _Q("IT", "Default port for HTTPS?", "443", "80", "22", "8080"),
    _Q("IT", "Default port for SSH?", "22", "21", "23", "3389"),
    _Q("IT", "RDP (Remote Desktop) uses which port by default?", "3389", "445", "5900", "1433"),
    _Q("IT", "What does 'phishing' usually try to steal?", "Logins and personal info", "CPU cycles", "Printer ink", "Wi-Fi bandwidth"),
    _Q("IT", "MFA stands for…", "Multi-Factor Authentication", "Main Firewall Access", "Managed File Archive", "Multiple Frame Allocation"),
    _Q("IT", "Which of these is a private IPv4 range?", "192.168.0.0/16", "8.8.8.0/24", "172.64.0.0/16", "100.0.0.0/8"),
    _Q("IT", "How many bits are in an IPv4 address?", "32", "64", "128", "16"),
    _Q("IT", "How many bits are in an IPv6 address?", "128", "64", "32", "256"),
    _Q("IT", "First thing to try for most 'it's broken' tickets?", "Restart it", "Reinstall the OS", "Replace the hardware", "Escalate to the CEO"),
    _Q("IT", "Windows command to see your IP config?", "ipconfig", "ifconfig /all", "netstat -r", "getip"),
    _Q("IT", "Which RAID level is plain mirroring?", "RAID 1", "RAID 0", "RAID 5", "RAID 10"),
    _Q("IT", "What does a firewall mostly do?", "Filters network traffic by rules", "Speeds up downloads", "Backs up files", "Cools the server room"),
    _Q("IT", "Ransomware does what?", "Encrypts your files and demands payment", "Shows pop-up ads", "Mines crypto quietly", "Steals only cookies"),
    _Q("IT", "The OSI model has how many layers?", "7", "4", "5", "9"),
    _Q("IT", "Layer 3 of the OSI model is…", "Network", "Transport", "Data Link", "Session"),
    _Q("IT", "A MAC address belongs to…", "A network interface", "A Mac computer only", "A website", "An email account"),
    _Q("IT", "'Ctrl + Shift + Esc' opens what on Windows?", "Task Manager", "Settings", "File Explorer", "Command Prompt"),
    _Q("IT", "Which Linux command lists files?", "ls", "dir /l", "list", "show"),
    _Q("IT", "SQL injection attacks target…", "Databases behind an app", "Wi-Fi routers", "Printers", "Power supplies"),
    _Q("IT", "What does VPN stand for?", "Virtual Private Network", "Very Protected Node", "Verified Public Network", "Virtual Packet Notifier"),
    _Q("IT", "Least privilege means…", "Give people only the access they need", "Everyone is admin", "No passwords on guest Wi-Fi", "Cheapest hardware first"),
    _Q("IT", "A '404' error means…", "Page not found", "Server crashed", "Access denied", "Too many requests"),
    _Q("IT", "HTTP status 429 means…", "Too many requests", "Not found", "Unauthorized", "Server error"),
    # --- gaming ---
    _Q("Gaming", "In Minecraft, what do you need to mine diamonds?", "Iron pickaxe or better", "Wooden pickaxe", "Stone shovel", "Your fist"),
    _Q("Gaming", "Which company makes the PlayStation?", "Sony", "Microsoft", "Nintendo", "Sega"),
    _Q("Gaming", "Master Chief is from which series?", "Halo", "Destiny", "Gears of War", "Doom"),
    _Q("Gaming", "In Among Us, the bad guys are called…", "Impostors", "Traitors", "Saboteurs", "Mimics"),
    _Q("Gaming", "Link is the hero of which series?", "The Legend of Zelda", "Final Fantasy", "Kingdom Hearts", "Metroid"),
    _Q("Gaming", "Which game has the 'Creeper'?", "Minecraft", "Terraria", "Roblox", "Fortnite"),
    _Q("Gaming", "In Uno, how many cards does everyone start with?", "7", "5", "8", "10"),
    _Q("Gaming", "What color is Pac-Man?", "Yellow", "Red", "Blue", "Green"),
    _Q("Gaming", "Which studio made The Witcher 3?", "CD Projekt Red", "Bethesda", "BioWare", "Ubisoft"),
    _Q("Gaming", "Fortnite's main mode drops players from a…", "Battle Bus", "Plane", "Blimp", "Rocket"),
    _Q("Gaming", "Pikachu is what type?", "Electric", "Fire", "Normal", "Psychic"),
    _Q("Gaming", "In Tetris, clearing 4 lines at once is called a…", "Tetris", "Quad", "Combo", "Stack"),
    _Q("Gaming", "Which game features the city of Los Santos?", "GTA V", "Saints Row", "Watch Dogs", "Mafia"),
    _Q("Gaming", "Kratos is the main character of…", "God of War", "Devil May Cry", "Dark Souls", "Bayonetta"),
    _Q("Gaming", "The Steam Deck is made by…", "Valve", "Nintendo", "Asus", "Razer"),
    _Q("Gaming", "In chess, which piece can only move diagonally?", "Bishop", "Rook", "Knight", "King"),
    _Q("Gaming", "Sonic the Hedgehog is blue and works for…", "Sega", "Nintendo", "Capcom", "Namco"),
    _Q("Gaming", "In blackjack, what's the best possible hand total?", "21", "20", "22", "11"),
    # --- general ---
    _Q("General", "What planet is known as the Red Planet?", "Mars", "Venus", "Jupiter", "Mercury"),
    _Q("General", "How many continents are there?", "7", "5", "6", "8"),
    _Q("General", "Largest ocean on Earth?", "Pacific", "Atlantic", "Indian", "Arctic"),
    _Q("General", "What's the capital of Japan?", "Tokyo", "Kyoto", "Osaka", "Seoul"),
    _Q("General", "Water boils at what °C at sea level?", "100", "90", "120", "212"),
    _Q("General", "Which animal is the largest mammal?", "Blue whale", "Elephant", "Giraffe", "Orca"),
    _Q("General", "How many sides does a hexagon have?", "6", "5", "7", "8"),
    _Q("General", "The Gateway Arch is in which city?", "St. Louis", "Chicago", "Kansas City", "Denver"),
    _Q("General", "Which gas do plants take in for photosynthesis?", "Carbon dioxide", "Oxygen", "Nitrogen", "Helium"),
    _Q("General", "Who painted the Mona Lisa?", "Leonardo da Vinci", "Michelangelo", "Van Gogh", "Picasso"),
    _Q("General", "What's the hardest natural substance?", "Diamond", "Gold", "Iron", "Quartz"),
    _Q("General", "How many minutes are in a day?", "1440", "1240", "1600", "960"),
    _Q("General", "Which country has the most people (2020s)?", "India", "China", "USA", "Indonesia"),
    _Q("General", "Godzilla first stomped onto screens from which country?", "Japan", "USA", "South Korea", "China"),
    _Q("General", "Joe Hisaishi is famous for scoring films from which studio?", "Studio Ghibli", "Pixar", "DreamWorks", "Toei"),
    _Q("General", "What's the chemical symbol for gold?", "Au", "Ag", "Gd", "Go"),
    _Q("General", "Which instrument has 88 keys?", "Piano", "Organ", "Accordion", "Harp"),
    _Q("General", "The Mississippi and Missouri rivers meet near…", "St. Louis", "Memphis", "New Orleans", "Omaha"),
    _Q("General", "How many legs does a spider have?", "8", "6", "10", "12"),
    _Q("General", "Which is the smallest prime number?", "2", "1", "3", "0"),
    _Q("General", "In what year did the first iPhone come out?", "2007", "2005", "2009", "2010"),
    _Q("General", "Which planet has the most famous rings?", "Saturn", "Uranus", "Neptune", "Jupiter"),
]


def shuffled(question: Question, rng: random.Random) -> tuple[list[str], int]:
    """Return choices in a random order and the new index of the right one."""
    order = list(range(4))
    rng.shuffle(order)
    choices = [question.choices[i] for i in order]
    return choices, order.index(question.answer)


def payout(bet: int, correct: bool) -> int:
    """Net change to the player's balance for one question.

    bet == 0 (free play): +FREE_REWARD if right, 0 if wrong.
    bet > 0: +bet if right, -bet if wrong (stake is held up front; on a win
    they get 2x stake back)."""
    if bet <= 0:
        return FREE_REWARD if correct else 0
    return bet if correct else -bet


def pick(rng: random.Random, recent: list[int], category: Optional[str] = None) -> int:
    pool = [i for i, q in enumerate(BANK) if (category is None or q.cat == category)]
    fresh = [i for i in pool if i not in recent]
    return rng.choice(fresh or pool)
