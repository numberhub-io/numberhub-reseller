"""How services look in the bot: the popular apps first (icon + clean name), then
the next most popular ones, then every service by its first letter, all with tidy
names instead of the raw catalog ones."""
from __future__ import annotations

import re

# code -> (icon, display name). Codes are NumberHub's; the order is the grid order.
POPULAR = [
    ("wa", "💬", "WhatsApp"), ("tg", "✈️", "Telegram"),
    ("ig", "📸", "Instagram"), ("fb", "👍", "Facebook"),
    ("lf", "🎵", "TikTok"), ("go", "🔍", "Google · Gmail"),
    ("dr", "🤖", "ChatGPT"), ("tw", "🐦", "X (Twitter)"),
    ("ds", "🎮", "Discord"), ("fu", "👻", "Snapchat"),
    ("am", "🛒", "Amazon"), ("wx", "🍏", "Apple"),
    ("mm", "🪟", "Microsoft"), ("vi", "💜", "Viber"),
    ("oi", "❤️", "Tinder"), ("ts", "💳", "PayPal"),
    ("mt", "🕹", "Steam"), ("bw", "🔒", "Signal"),
    ("nf", "🎬", "Netflix"), ("ub", "🚗", "Uber"),
]
ANY_OTHER = "ot"
_POP = {code: (icon, name) for code, icon, name in POPULAR}
LETTERS = [chr(c) for c in range(ord("A"), ord("Z") + 1)] + ["#"]
MORE_POPULAR = 30          # the "🔥 More popular apps" page


def nice_name(code: str, api_name: str | None) -> str:
    """'facebook' -> 'Facebook', 'Google,youtube,Gmail' -> 'Google, youtube, Gmail',
    ' Caffe Nero' -> 'Caffe Nero'. Brand casing (eBay, myBCA) and domains (vk.com)
    are kept as they are."""
    if code in _POP:
        return _POP[code][1]
    name = re.sub(r"\s+", " ", (api_name or code)).strip()
    name = re.sub(r",(?=\S)", ", ", name)
    if name and name == name.lower() and "." not in name:
        name = name[:1].upper() + name[1:]
    return name or code


def icon(code: str) -> str:
    return _POP.get(code, ("📱",))[0]


def letter_of(name: str) -> str:
    first = (name or "#").strip()[:1].upper()
    return first if "A" <= first <= "Z" else "#"


def more_popular(items: list[dict]) -> list[dict]:
    """The catalog comes most-popular first: the next apps after the grid's own."""
    return [s for s in items if s["code"] not in _POP and s["code"] != ANY_OTHER][:MORE_POPULAR]


def by_letter(items: list[dict], letter: str) -> list[tuple[str, str]]:
    """(display name, code) of every app starting with `letter`, A to Z."""
    rows = [(nice_name(s["code"], s["name"]), s["code"]) for s in items if s["code"] != ANY_OTHER]
    return sorted(((n, c) for n, c in rows if letter_of(n) == letter), key=lambda x: x[0].casefold())


def letter_counts(items: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for s in items:
        if s["code"] != ANY_OTHER:
            k = letter_of(nice_name(s["code"], s["name"]))
            counts[k] = counts.get(k, 0) + 1
    return counts
