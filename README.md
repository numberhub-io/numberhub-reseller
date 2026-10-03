# NumberHub reseller bots

This project lets anyone who already has customers run their own Telegram shop bot that sells virtual phone numbers for SMS verification. The bot has the reseller's name, the reseller's prices and the reseller's customers. NumberHub supplies every number through its public API, billed to the reseller's own NumberHub wallet with the reseller's own API key.

One Python process runs everything: the builder bot where resellers create their shops, every reseller's shop bot, and the background loops that keep orders in step with NumberHub.

```
customer ──▶ @their_shop_bot ──────────────▶ NumberHub API (the reseller's key)
             customer balance, kept here       numbers, SMS codes, and the hold and
             (the reseller tops it up)         charge on the reseller's wallet
```

Contents

1. [How it works](#how-it-works)
2. [What customers see](#what-customers-see)
3. [Guide for resellers](#guide-for-resellers)
4. [Money rules](#money-rules)
5. [Install and run](#install-and-run)
6. [Deploy on a Linux server](#deploy-on-a-linux-server)
7. [Operating the platform](#operating-the-platform)
8. [Tests](#tests)
9. [Project layout](#project-layout)
10. [Troubleshooting](#troubleshooting)

## How it works

There are two kinds of bots and two kinds of money.

The builder bot belongs to the platform (you). A reseller opens it, sends a bot token from @BotFather and a NumberHub API key, picks a commission, and their shop bot starts selling within a minute. The builder bot is optional: without `BUILDER_BOT_TOKEN` the process runs the shop bots that already exist and nobody can create new ones.

There is a shorter way in too, the hosted path: in @TheNumberHubBot a NumberHub customer taps 🤖 Your own bot and sends only the bot token. NumberHub creates the API key on their account itself and hands both to this platform through its provisioning listener (`app/provision.py`, below). The shop that comes out is the same as one made in the builder.

A shop bot belongs to one reseller. Their customers buy numbers in it, and the reseller manages it from an admin panel inside the same bot.

Customer balances live in this project's database. Customers pay the reseller however the reseller likes (cash, bank transfer, crypto, anything), and the reseller adds that amount to the customer in the admin panel. The reseller's NumberHub wallet is separate: NumberHub holds and charges it for every number the shop buys. The reseller's profit is the difference between what customers are charged and what NumberHub charges.

Tech used:

| Part | What it is |
|---|---|
| Language | Python 3.11 or 3.12 |
| Telegram | aiogram 3, long polling (one `Bot` and `Dispatcher` per shop, all in one asyncio loop) |
| Database | SQLite through SQLAlchemy 2 (async, aiosqlite), WAL mode |
| HTTP | httpx, to the NumberHub API at `https://api.numberhub.io/v1` |
| Secrets at rest | Fernet (`cryptography`): bot tokens and API keys are stored encrypted with `SECRET_KEY` |
| Settings | pydantic-settings, read from the environment or `.env` |

Nothing listens on a public port. The process makes outgoing connections to Telegram and NumberHub, so the server needs no open inbound ports. The one exception is optional: with `PROVISION_SECRET` set, the hosted path's listener runs on `127.0.0.1` only (`PROVISION_PORT`, 8097), and every request must carry that secret. It creates, reconnects, lists, pauses and resumes shops with the same checks as the builder.

Background loops in `main.py`:

| Loop | Every | What it does |
|---|---|---|
| sync | 5 s (`SYNC_INTERVAL_SEC`) | For each shop with open orders, 8 shops at a time, even while its Telegram bot is reconnecting: one `GET /orders?limit=100`, plus a few `GET /numbers/{id}` per pass for live orders older than the newest 100. It copies status, number and codes into the local order, settles orders that ended, sends codes to customers, and refreshes the live order card (at most every 20 s unless something changed). A 429 or a key problem backs that shop off instead of retrying every 5 s |
| sweep | 30 s | Settles any ended order still unsettled, finishes purchases whose outcome was unclear, and cancels any number nobody holds money for (see [Money rules](#money-rules)) |
| warm | 120 s | Refreshes the "from" price of the 20 popular apps for each shop, one request every 0.5 s, so the app picker can show `WhatsApp · $0.24+` without waiting |

NumberHub allows 60 requests per 10 seconds per API key. One sync request per shop every 5 seconds plus the warm loop stays well under that.

## What customers see

Everything below is in the customer's Telegram language when it is one of the 14 supported ones (English, Russian, Arabic, Spanish, Portuguese, French, Indonesian, Hindi, Bengali, Turkish, Persian, Urdu, Chinese, Vietnamese). Customers can switch with 🌐 Language. Customers never see the word NumberHub; the shop is the reseller's.

1. Menu. A welcome line (the reseller can write their own), the customer's balance, and buttons for buying, 🔁 buying the same app and country again, 🧾 My orders, 💰 Balance and 🌐 Language.
2. Pick an app. The 20 most used apps are one tap away with their icon and the cheapest price, for example `💬 WhatsApp · $0.24+`. Below them: 🔥 More popular apps (32 more well-known apps such as LinkedIn, WeChat, Shopee and Binance, picked by hand because NumberHub ranks only the top of its catalog), 🔤 All apps A to Z (a letter grid; each letter lists its apps alphabetically, 30 per page) and ❓ Any other app for sites that are not listed. Typing a name on any screen searches, so `tik` finds TikTok.
3. Pick a country. Countries come best first: in stock and delivering well, then weaker ones, then countries with no number free right now (marked ⏳; buying one queues the order until a number appears). Each button shows the customer's price, for example `🇺🇸 USA · $0.39`. Typing a country name filters the list.
4. Confirm. The price, a line that says the customer is charged only if the code arrives, and the customer's balance. If the balance is too low, the screen says how to top up.
5. The order card. The number (tap to copy), a 20 minute countdown, and the code as soon as it arrives. The code is also sent as a separate message. When an app verified by phone call, the "code" is the caller's number, and the card says to type its last 6 (or 4) digits. A cancel button unlocks after about 2 minutes (NumberHub refuses earlier cancels) and shows the seconds left until then. The card updates by itself.
6. No code means no charge. If the time runs out or the customer cancels, the reserved amount goes back and the customer gets a message saying so.

Under 💰 Balance customers see their ID. That is what they send to the reseller when they want to top up. The shop bot only works in private chats; in a group it stays silent, so nobody's number or code is shown to the group.

## Guide for resellers

### What you need

1. A Telegram bot token. In Telegram open @BotFather, send `/newbot`, choose a name and a username, and copy the token (it looks like `123456789:AAH...`).
2. A NumberHub API key with money in the wallet. Sign in at numberhub.io, open Account, then API keys, then Create key, and leave every permission ticked (the shop needs catalog, orders read and write, and wallet). The key starts with `nh_`. One key per bot is a good habit. A daily spend limit on the key is a good safety net: it counts what your numbers really cost, and numbers that got no code don't use it up.

### Create your bot

Open the builder bot and tap ➕ Create my bot. It asks for three things:

1. The bot token. The builder deletes your message as soon as it has read it and checks the token with Telegram. If the bot used a webhook somewhere else, the builder switches it over.
2. The API key. It is deleted from the chat too and checked live against NumberHub, permission by permission, and your wallet balance is shown. After three rejected keys in 10 minutes the builder asks you to wait: a burst of bad keys from one server makes NumberHub block that server, which would stop every shop.
3. Your commission: tap 20%, 30% (marked ⭐, the default), 50% or 100%, or type any number from 0 to 300.

Your own @username becomes the shop's support contact, so customers know who to send their ID to. You can change it in the admin panel.

If you revoke the bot's token in @BotFather, tap ➕ Create my bot again and send the new token of the same bot. The builder recognises the bot and reconnects it with all its customers, balances and orders; nobody else can take over a bot you connected.

Your bot starts selling immediately. 🤖 My bots in the builder lists your bots (up to 3 per person) with sales, profit and status, and lets you pause or resume a bot or replace its API key. A paused bot stops selling but keeps running: customers are told it is paused, and orders bought before the pause still get their codes and can still be cancelled. While the bot has open orders, a new API key must come from the same NumberHub account, because those orders live there; the builder checks this.

### Or: from the NumberHub bot

If you already use NumberHub, open @TheNumberHubBot and tap 🤖 Your own bot, then send your bot token. That is all: NumberHub creates an API key for your shop on your own account (named `Shop bot @yourbot`, with only the permissions a shop needs), and your bot is live a minute later with a 30% commission you can change in `/admin`. The same screen lists your bots with their week and lets you pause or resume them. You need one paid top-up on your NumberHub account first, because your shop sells from that balance.

### Your admin panel

Open your own shop bot and send `/admin`, or tap ⚙️ Admin panel in the menu (only you see that button). The panel shows customers, sales and profit for today, 7 days and 30 days, your NumberHub wallet balance, your share link, and your commission with a worked example. The buttons:

| Button | What it does |
|---|---|
| ➕ Add balance | Send `customer-ID amount`, for example `123456789 5`, or use `@username` instead of the ID |
| ➖ Remove balance | The same format. Money reserved by an open order cannot be removed |
| 👥 Customers | The latest 25 customers with their IDs and balances |
| 📣 Broadcast | Sends your message to every customer of this bot |
| 💲 Commission | Changes your commission. It applies to new purchases only |
| 💳 Deposits | Lets customers ask for balance in the bot (see below). Set your payment details and a minimum here, and approve what waits |
| 🏷 Custom prices | Your own price for one app, or for one app in one country: a fixed price (`0.35`) or its own commission (`10%`). Also a profit cap: the most you earn on one number |
| 📝 Welcome text | Replaces the greeting on the menu |
| 🆘 Support contact | A `@username` or an `https://` link. Customers see it on their balance screen |
| 🚫 Block / unblock | A blocked customer cannot use the bot |

### How the commission works

Your commission is a percentage added on top of NumberHub's price for each country. With a 30% commission, a number NumberHub prices at $0.25 costs your customer $0.33 (rounded up to the cent) and you keep $0.08. The bot uses NumberHub's highest price for that route (`price_max`) as the base, so NumberHub can never charge you more for a sale than the customer paid.

### Deposits

Instead of customers sending you their ID, they can ask for balance in the bot. Turn it on in ⚙️ Admin panel → 💳 Deposits → ✏️ Payment details: write how customers pay you (for example `bKash: 01XXXXXXXXX`, a Binance Pay ID or a USDT address). Then:

1. The customer taps 💳 Add balance, sees your payment details, pays you, and sends the amount and the transaction ID or a screenshot.
2. You get the request in your bot at once (#1, #2, ...), with the screenshot, and three buttons: ✅ Approve, ✏️ Other amount (credit what really arrived) and ❌ Reject.
3. The customer gets a message: approved with their new balance, or not approved with your support contact.

A request is credited exactly once, even if you tap twice or on two devices. A customer has one request waiting at a time and at most 5 a day; you can set a minimum. Requests that wait are also listed under 💳 Deposits. Send `-` as the payment details to turn deposits off.

### Custom prices

One commission for everything can make expensive numbers too dear for your customers. Under ⚙️ Admin panel → 🏷 Custom prices you can:

- give any app its own price for all countries, or for one country: a fixed price like `0.35`, or its own commission like `10%`;
- set a profit cap: the most you earn on one number (for example `0.20`), so a $2.00 number sells for $2.20 instead of $2.60 at 30%.

The most specific setting wins: the app in that country, then the app, then your commission (held to the cap). A fixed price never sells below NumberHub's price: if NumberHub's price goes above it, that number sells at NumberHub's price and you earn nothing on it, but you never pay for a customer's number. The screen shows NumberHub's current price for the app and country while you set it, and warns you when your price is below it.

### Alerts you may get

The shop bot messages you, at most once an hour per problem, when:

- your NumberHub balance is too low for a customer's purchase (top up at numberhub.io; sales continue as soon as there is money),
- your API key reached its daily spend limit,
- NumberHub rejected your API key (it was revoked or rotated). Send a new one with 🤖 My bots, then 🔑 New API key,
- the key is missing a permission, or only works from certain IP addresses,
- your account has as many open orders for one app and country as NumberHub allows (all your shops count together).

While any of these lasts, customers who try to buy are told that buying is paused for a moment, and nothing is held from them.

## Money rules

These rules are covered by `tests/run_tests.py`.

A purchase goes like this:

1. The bot looks up the current price and checks it against the price on the button the customer tapped. If the price went up, nothing is held and the confirm screen is shown again with the new price. A customer without enough balance is told so before any request reaches NumberHub.
2. The customer's price is held on their balance and the order row is created, in one transaction. Nothing is charged yet. A customer can run one purchase at a time, so a double tap buys one number.
3. The bot buys at NumberHub with `POST /numbers`, sending `max_price` (the route's highest price) and an `Idempotency-Key` that is unique to this order, including across databases (it carries the order's creation time).
4. If NumberHub's purchase handler says no (sold out, price changed, wallet empty), the hold goes back at once. Any other trouble keeps the hold: a lost reply, a 5xx, or an answer from the layers in front of the handler (429, 401/403, "still in progress") after a request may already have arrived. The bot retries with the same key, so NumberHub returns the first result instead of buying a second number. If the outcome is still unknown, the order stays `buying` and the sweep asks again every 30 seconds with the same key and a byte for byte identical body. It never touches an order a live purchase is still working on, and gives the hold back after 20 hours (NumberHub forgets keys after 24, and a later replay would be a new purchase).
5. If a purchase that was given up on turns out to exist, the customer's hold is taken again and the order reopens. If the customer no longer has the money, the number is cancelled at NumberHub so no code arrives that nobody pays for.
6. When the NumberHub order ends, the local order is settled exactly once: the hold is charged if a code arrived and released if not. The claim and the balance change happen in one transaction, so the sync loop, the sweep loop and a customer's cancel tap cannot settle the same order twice. Status updates only move forward, so a list fetched before a cancel can't reopen the cancelled order.

Other rules:

- Every change to a customer's balance is a single conditional update with an audit row (`member_tx`).
- A customer can have at most 10 open orders, and at most 3 for the same app and country (`MAX_OPEN_PER_MEMBER`, `MAX_OPEN_PER_ROUTE`).
- If NumberHub refuses a buy because the price is higher than the list said (`price_exceeded`), the bot remembers the real price for that route for 15 minutes. The customer sees the new price once, and the next tap buys.
- Cancelling goes to NumberHub first. The local hold is released only after NumberHub confirms the cancel. If a code arrived in the meantime, the customer is told and charged for the code.

## Install and run

You need Python 3.11 or 3.12 and git.

```bash
git clone https://github.com/numberhub-io/numberhub-reseller.git
cd numberhub-reseller
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# paste the printed key into .env as SECRET_KEY, then fill in the rest
python main.py
```

On start the log shows one `reseller bot N (@name) started` line per shop and `Run polling` for each bot.

### Settings

Every setting is an environment variable, or a line in `.env`.

| Variable | Default | Meaning |
|---|---|---|
| `SECRET_KEY` | (required) | Fernet key that encrypts every bot token and API key in the database. Back it up: without it the stored tokens and keys cannot be read |
| `BUILDER_BOT_TOKEN` | empty | Token of the platform's builder bot. Empty means no builder: existing shops run, nobody can create new ones |
| `ADMIN_IDS` | empty | Telegram IDs of platform operators, comma separated. They get `/platform`, `/disable` and `/enable` in the builder bot |
| `DB_URL` | `sqlite+aiosqlite:///./reseller.db` | Database. SQLite is the tested choice |
| `NUMBERHUB_API` | `https://api.numberhub.io/v1` | NumberHub API base URL |
| `NUMBERHUB_SITE` | `https://numberhub.io` | Shown to resellers in the builder's instructions |
| `DEFAULT_MARKUP_PCT` | `30` | The commission the builder marks as recommended (⭐) when a shop is created |
| `MAX_MARKUP_PCT` | `300` | Highest commission a reseller may set |
| `SYNC_INTERVAL_SEC` | `5` | How often open orders are synced with NumberHub |
| `MAX_OPEN_PER_MEMBER` | `10` | Open orders one customer may have |
| `MAX_OPEN_PER_ROUTE` | `3` | Open orders one customer may have for the same app and country |
| `PROVISION_SECRET` | empty | Turns on the hosted path's listener (32+ characters). The NumberHub backend sends the same value. Empty means off |
| `PROVISION_PORT` | `8097` | Port of that listener, always on `127.0.0.1` |

## Deploy on a Linux server

The steps below assume Ubuntu or Debian with systemd. The code lives in `/opt/numberhub-reseller` and belongs to your admin account; the service runs as a separate user called `reseller` that can read the code but write only its data directory, `/var/lib/numberhub-reseller`. The unit file and backup script are in `deploy/`.

```bash
sudo apt install -y python3-venv sqlite3 git
sudo adduser --system --group --home /var/lib/numberhub-reseller reseller
sudo mkdir /opt/numberhub-reseller && sudo chown "$USER": /opt/numberhub-reseller
git clone https://github.com/numberhub-io/numberhub-reseller.git /opt/numberhub-reseller
cd /opt/numberhub-reseller
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# edit .env: SECRET_KEY, BUILDER_BOT_TOKEN, ADMIN_IDS, and
#   DB_URL=sqlite+aiosqlite:////var/lib/numberhub-reseller/reseller.db
sudo chown root:reseller .env && sudo chmod 640 .env
sudo cp deploy/numberhub-reseller.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now numberhub-reseller
journalctl -u numberhub-reseller -f        # live log
```

Run exactly one instance. Two processes polling the same bot tokens take updates from each other (Telegram answers with `TelegramConflictError`) and customers' taps get lost.

To update:

```bash
cd /opt/numberhub-reseller
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart numberhub-reseller
```

A restart is safe: on SIGTERM every bot stops taking new updates first, then purchases already in progress get up to 60 seconds, and only then are the connections closed. Anything still unclear after that is finished by the sweep loop after the restart. The process then exits on its own, so systemd's `Restart=always` brings it back after a crash too.

The database schema is created on first start (`create_all`). There is no migration tool yet, so a release that adds a column to an existing table has to come with its own `ALTER TABLE` step.

Backups: `deploy/backup.sh` reads `DB_URL` from `.env`, makes a consistent copy with `sqlite3 .backup` while the bots run, and keeps 14 days in a `backups` folder next to the database. Run it from root's cron, for example `17 */6 * * * /opt/numberhub-reseller/deploy/backup.sh`, and copy the backups and `SECRET_KEY` to a second machine. A backup on the same disk does not survive the disk.

To restore a backup, stop the service first and remove the old WAL files, or SQLite may replay them over the restored copy:

```bash
sudo systemctl stop numberhub-reseller
cd /var/lib/numberhub-reseller
sudo rm -f reseller.db-wal reseller.db-shm
gunzip -c backups/reseller-20261002T061700Z.db.gz | sudo -u reseller tee reseller.db > /dev/null
sudo systemctl start numberhub-reseller
```

A restore takes balances and orders back to the moment of the backup. Purchases made after it are still on the resellers' NumberHub accounts; compare with their NumberHub order lists before adding any balance back by hand.

## Operating the platform

Operators listed in `ADMIN_IDS` have three commands in the builder bot:

| Command | What it does |
|---|---|
| `/platform` | Every shop with its number, owner, status, customers and 7 day sales |
| `/disable 12` | Stops new sales in shop number 12. Its customers are told it is paused and open orders still finish. The owner can't undo it (no resume, no new key, no broadcasts) |
| `/enable 12` | Lets it sell again |

Useful log lines: `reseller N created by ...` (a new shop), `reseller N reconnected` (a new token for an existing bot), `bought order ...` (a sale), `order N settled: member X charged|refunded`, `recovered purchase` (an unclear purchase that the sweep finished), `given back` (a number nobody was holding money for, cancelled at NumberHub), and `sync failed for reseller N` (look at the traceback below it). A line at ERROR level always deserves a look.

## Tests

```bash
python -X utf8 tests/run_tests.py     # prints RESULT: N passed, M failed and exits 1 on failure
python -X utf8 tests/preview.py en    # prints every screen, text and buttons, in one language
```

`run_tests.py` runs the real code against `tests/fakes.py`: an in-memory NumberHub API with the same JSON shapes, idempotency rules and errors as the real one, and a fake Telegram session that records every message and button. It uses its own temporary database. CI runs it with pyflakes on Python 3.11 and 3.12 for every push (`.github/workflows/ci.yml`).

`tests/live_e2e.py` checks the whole path against the real NumberHub API with a real key, on a temporary database:

```bash
NH_KEY=nh_live_... python -X utf8 tests/live_e2e.py
```

It loads the catalog, buys the cheapest WhatsApp number in stock, checks the hold on both sides, replays the purchase with the same Idempotency-Key, waits for the cancel lock and cancels, then checks that both the customer's hold and the NumberHub wallet are back where they started. The wallet needs a little money (about $0.50), and nothing is charged unless an SMS happens to arrive in those 2 minutes.

## Project layout

| Path | What is in it |
|---|---|
| `main.py` | Starts the builder bot, every shop bot and the sync, sweep and warm loops; graceful shutdown |
| `app/runtime.py` | Starts and stops one shop bot (aiogram `Bot` and `Dispatcher`) and its NumberHub client |
| `app/provision.py` | The hosted path: a localhost listener NumberHub calls to create, reconnect, list, pause and resume shops |
| `app/selling.py` | Prices, buying, cancelling, recovery of lost purchases, sync with NumberHub, settlement |
| `app/repo.py` | All database reads and writes, including the atomic balance updates |
| `app/models.py` | Tables: `resellers`, `members`, `member_tx`, `orders` |
| `app/numberhub.py` | NumberHub API client (retries, idempotency, error mapping) |
| `app/bots/builder.py` | The builder bot: create a shop, my bots, operator commands |
| `app/bots/reseller.py` | The shop bot's customer screens |
| `app/bots/admin.py` | The shop owner's admin panel |
| `app/bots/callbacks.py` | Button payloads (Telegram allows 64 bytes) |
| `app/catalog_ui.py` | Popular apps, icons, clean app names, the A to Z grouping |
| `app/texts.py` | Order card, code message, owner alerts |
| `app/i18n.py`, `app/locales/` | The 14 customer languages (missing keys fall back to English) |
| `app/crypto.py` | Fernet encryption of stored secrets |
| `deploy/` | systemd unit and backup script |
| `tests/` | Offline suite, fakes, screen preview, live check |

## Troubleshooting

The bot does not answer at all. Check `journalctl -u numberhub-reseller` for `stopped with an error` (the bot retries on its own, with backoff) or `Telegram rejected the token`. A revoked token stops that one shop until the owner sends the new token through ➕ Create my bot, which reconnects the same bot. If the log shows `TelegramConflictError`, a second copy of the process (or another program) is polling the same token.

Customers keep seeing "price changed". The price on NumberHub moved between the list and the purchase. The bot shows the new price and the next tap buys; if it repeats for one route, the route's price is moving fast or is out of stock.

"Buying is paused" for every customer. Usually the reseller's NumberHub wallet is empty, the key hit its daily limit, or the key was revoked. The owner gets an alert in their own bot saying which.

A customer says they were charged without a code. A charge only happens when NumberHub reports the order as received or completed, or reports a code. In the database, `orders` holds each order's status and codes and `member_tx` has an audit row for every change to the customer's balance.

`SECRET_KEY` was lost. The stored bot tokens and API keys cannot be decrypted, and the log says so for each shop. Customer balances and orders are still in the database: each reseller sends their bot token again through ➕ Create my bot, then their API key, and the shop comes back as it was.
