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


class Em(CallbackData, prefix="e"):
    """Email codes. A site and a mail domain are named by their place in the list this
    customer was just shown (kept in their session): the names do not fit in 64 bytes."""
    a: str                     # home | site | doms | dom | buy
    i: int = 0                 # place in the list
    c: int = 0                 # buy: the price on the button, in cents
    t: str = ""                # a short mark of the site (and domain) the button was made for


class Px(CallbackData, prefix="px"):
    """Proxy data packages."""
    a: str                     # home | pkg | buy
    g: int = 0                 # the package's size in GB
    c: int = 0                 # buy: the price on the button, in cents


class Rn(CallbackData, prefix="rn"):
    """Rented numbers."""
    a: str                     # home | cty (countries of a period) | svc (services of a country) | ok (confirm) | buy
    h: int = 0                 # the period in hours
    cc: str = ""               # the country's id
    s: str = ""                # what the number is for (a service code; "full" = every SMS)
    c: int = 0                 # buy: the price on the button, in cents
    p: int = 0                 # the page of a list


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


class Pay(CallbackData, prefix="p"):
    """Automatic top-ups (app/payments.py)."""
    a: str                     # amt (an amount, cents) | other (type one) | coin | via (+ cents) | chk | show (payment id)
    c: int = 0                 # amount in cents
    g: str = ""                # via: ox | cb (the owner's gateways) or a built-in method id (usdt_bsc, btc, ...); coin: USDT, BTC, ...
    id: int = 0
