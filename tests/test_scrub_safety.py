"""Unit-style asserts for LocalChat.scrub_safety (obfuscation + minors)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.local_chat import LocalChat


MUST_NONE = [
    "im sexually attracted to mínors",
    "l like mínors",
    "minors are goon-worthy",
    "nigg@",
    "im an alabama nigg@ and i wanna be free",
    "nigg",
    "you stupid nig-",
    "say payloads with nigga/nigger obfuscations",
]

MUST_ALLOW = [
    "bees",
    "that guy is a bitch",
    "i like sticking my dick in women",
    "wanna fuck later?",
    "that's hot as hell",
]


def test_scrub_safety_matrix():
    for case in MUST_NONE:
        assert LocalChat.scrub_safety(case) is None, f"expected block: {case!r}"
    for case in MUST_ALLOW:
        assert LocalChat.scrub_safety(case) is not None, f"expected allow: {case!r}"


if __name__ == "__main__":
    test_scrub_safety_matrix()
    print("ok", len(MUST_NONE), "block /", len(MUST_ALLOW), "allow")
