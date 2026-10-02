"""Member-facing text in every supported language; the reseller's admin panel is
English. A member's language comes from Telegram (language_code) and can be
changed with 🌐 Language. Missing keys fall back to English."""
from __future__ import annotations

import importlib

LANGS = {
    "en": "🇬🇧 English", "ru": "🇷🇺 Русский", "ar": "🇸🇦 العربية", "es": "🇪🇸 Español",
    "pt": "🇧🇷 Português", "fr": "🇫🇷 Français", "id": "🇮🇩 Indonesia", "hi": "🇮🇳 हिन्दी",
    "bn": "🇧🇩 বাংলা", "tr": "🇹🇷 Türkçe", "fa": "🇮🇷 فارسی", "ur": "🇵🇰 اردو",
    "zh": "🇨🇳 中文", "vi": "🇻🇳 Tiếng Việt",
}
RTL = {"ar", "fa", "ur"}
_TABLES: dict[str, dict[str, str]] = {}


def _table(lang: str) -> dict[str, str]:
    if lang not in _TABLES:
        try:
            _TABLES[lang] = importlib.import_module(f"app.locales.{lang}").T
        except ModuleNotFoundError:
            _TABLES[lang] = {}
    return _TABLES[lang]


def resolve(code: str | None) -> str:
    """Telegram language_code ('pt-br', 'zh-hans', 'en') -> a supported language."""
    base = (code or "en").lower().replace("_", "-").split("-")[0]
    return base if base in LANGS else "en"


def t(lang: str, key: str, **kw) -> str:
    s = _table(lang).get(key) or _table("en").get(key) or key
    if kw:
        try:
            s = s.format(**kw)
        except (KeyError, IndexError, ValueError):
            s = (_table("en").get(key) or key).format(**kw)
    return s
