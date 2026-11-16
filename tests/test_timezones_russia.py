import asyncio

from cogs.timezones import COMMON_ZONES, normalize_timezone, tz_autocomplete


def _ac(q):
    return asyncio.run(tz_autocomplete(None, q))


def test_vladivostok_alias():
    assert normalize_timezone("Vladivostok")[1] == "Asia/Vladivostok"
    assert normalize_timezone("vladivostok")[1] == "Asia/Vladivostok"


def test_vladivostok_common():
    assert "Asia/Vladivostok" in COMMON_ZONES


def test_autocomplete_surfaces_vladivostok():
    for q in ("vlad", "russia"):
        hits = [c.name + " " + c.value for c in _ac(q)]
        assert any("Asia/Vladivostok" in h for h in hits), (q, hits)
