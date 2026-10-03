"""Inline-button payloads (Telegram allows 64 bytes)."""
from aiogram.filters.callback_data import CallbackData


class Nav(CallbackData, prefix="n"):
    to: str                    # menu | buy | orders | balance | support | lang | again | admin


class SvcPage(CallbackData, prefix="sp"):
    page: int


class AZ(CallbackData, prefix="az"):
    l: str = ""                # "" (or old "*") = the letter grid; "+" = more popular; "A".."Z" / "#"
    page: int = 0


class Svc(CallbackData, prefix="s"):
    code: str
    page: int = 0              # countries page


class Cty(CallbackData, prefix="c"):
    code: str
    cc: str


class Buy(CallbackData, prefix="b"):
    code: str
    cc: str
    cents: int                 # the price on the button the member tapped


class Ord(CallbackData, prefix="o"):
    a: str                     # view | cancel
    id: int


class Lang(CallbackData, prefix="l"):
    code: str


class Adm(CallbackData, prefix="a"):
    a: str
    arg: str = ""


class Noop(CallbackData, prefix="x"):
    k: str = ""


class Dep(CallbackData, prefix="d"):
    a: str                     # ok | no | amt (owner: approve / reject / approve another amount)
    id: int
