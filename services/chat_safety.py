"""Conservative chat-only safety gates; never punish or delete member messages."""
import re
import unicodedata

SAFETY_REPLY = "I'm not doing slurs or sexual jokes about children."
MINOR_REPLY = "I'll keep discussion involving children respectful and non-sexual."
WORDPLAY_REPLY = "I'm not doing baited word games or disguised slurs."


def normalize(text):
    text = unicodedata.normalize('NFKC', text).casefold()
    text = ''.join(c for c in text if unicodedata.category(c) != 'Cf')
    return text.translate(str.maketrans({'0':'o','1':'i','3':'e','4':'a','5':'s','7':'t','@':'a','$':'s',
        '\u0430':'a','\u0435':'e','\u0456':'i','\u043e':'o','\u0441':'c'}))

# Deliberately conservative: the bot should not echo these even as quotes or letter games.
_SLURS = ('nigger','nigga','faggot','fag','kike','chink','tranny','retard')
_SLUR_PATTERNS = [re.compile(r'(?<![a-z])' + r'[\W_]*'.join(word) + r's?(?![a-z])') for word in _SLURS]
_MINOR = re.compile(r'\b(children|child|kids?|minors?|underage|under.?18|preteen|teen(?:ager)?s?|csam|cp|loli|lolicon|shota|shotacon|pedo\w*)\b|\bcheese\s+pizza\b|\b(?:[0-9]|1[0-7])\s*[- ]?(?:years?\s*[- ]?old|y/?o)\b')
_WORDPLAY = re.compile(r'\b(abbreviat\w*|acronyms?|acrostics?|initials|first\s+letters?|every\s+\w+\s+letter|spell\w*|decode\w*|encode\w*|base64|rot13|reverse\w*)\b|\brepeat\s+after\s+me\b|\bjoin\s+(?:these|the)\s+(?:letters|words)\b')


def contains_slur(text):
    text = normalize(text)
    return any(p.search(text) for p in _SLUR_PATTERNS)


_SEXUAL = re.compile(r"\b(sex(?:ual\w*)?|nudes?|naked|porn\w*|fuck\w*|rape\w*|molest\w*|horny|attract\w*|fetish\w*|blowjob|genitals?|penis|vagina)\b")
_ABUSE = re.compile(r"\b(?:rape|molest)\s+(?:me|you|her|him|them|my|a|the)\b|\b(?:sex|fuck\w*|rape\w*)\b.{0,45}\b(?:dog|cat|animal)\b")
_FOLLOWUP = re.compile(r"^(?:and |but |so |what about |for (?:the |a )?right price|yes\b|no\b|why\b|what\??$|do it\b)|\b(?:them|those|that|it)\b", re.I)


def relevant_context(latest, history=()):
    text = latest.get('text', '')
    if re.search(r'rape me\s+by\s+nirvana', text, re.I):
        return text
    # A fresh standalone message does not inherit every sensitive topic in the channel.
    if not (_FOLLOWUP.search(text) or _SEXUAL.search(normalize(text))):
        return text
    parts = [text]
    ref = latest.get('reply_to') or {}
    if ref.get('message'):
        parts.append(ref['message'])
    for row in list(history)[-6:]:
        if row.get('bot') or row.get('text') == text:
            continue
        if row.get('user_id') == latest.get('user_id') or _MINOR.search(normalize(row['text'])):
            parts.append(row['text'])
    return '\n'.join(parts)


def context_reply(text):
    text = normalize(text) + '\n' + unicodedata.normalize('NFKC', text).casefold()
    # Discussing a song is not asking to enact its title.
    text = re.sub(r'rape me\s+by\s+nirvana', '[song title]', text)
    if (_MINOR.search(text) and 'cheese pizza' in text and re.search(r'\b(children|child|kids?)\b', text)) or re.search(r'\b(csam|child porn\w*|lolicon|shotacon)\b', text) or (_MINOR.search(text) and _SEXUAL.search(text)):
        return MINOR_REPLY
    if _ABUSE.search(text):
        return "No. I'm not roleplaying sexual violence or animal abuse."
    if contains_slur(text):
        return SAFETY_REPLY
    if _WORDPLAY.search(text) and re.search(r'\b(slurs?|n.?word|f.?slur|racis\w*)\b', text):
        return WORDPLAY_REPLY
    return None


def output_allowed(text):
    if text in (SAFETY_REPLY, MINOR_REPLY, WORDPLAY_REPLY, "No. I'm not roleplaying sexual violence or animal abuse."):
        return True
    return not contains_slur(text) and context_reply(text) is None and text.strip().upper() not in {'PASS', 'LP'}
