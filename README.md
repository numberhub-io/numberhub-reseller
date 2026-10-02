# NumberHub Reseller Bots

Anyone with customers can run **their own Telegram bot that sells virtual numbers**: their bot name, their prices, their customers. Every number is supplied by [NumberHub](https://numberhub.io) through its public API, using the reseller's own API key and wallet.

```
customer ──▶ reseller's bot (@their_shop_bot) ──▶ NumberHub API (reseller's key)
            member balance (credit the                 numbers, codes, holds and
            reseller sells their own way)               charges on the reseller's wallet
```

## How a reseller starts (2 minutes)

1. Opens the **builder bot** and taps **➕ Create my bot**.
2. Sends a bot token from @BotFather (the message is deleted at once).
3. Sends a NumberHub API key (numberhub.io → Account → API keys). It is checked live and the wallet balance is shown.
4. Picks a markup (20/30/50/100% or any number). The bot is live immediately.

Inside their bot the reseller sends **/admin** to:
- see sales and profit (today, 7 days, 30 days) and their NumberHub wallet balance;
- add or remove a customer's balance (customers find their ID under 💰 Balance);
- change the markup, the welcome text and the support contact;
- message all customers, list customers, block or unblock someone.

## What customers get

- **📱 Buy a number:** pick a service (or just type its name), then a country. Countries are ordered by delivery rate and show the price and the success %.
- **Clear confirm screen:** "you are charged only if the code arrives — no code = automatic refund".
- **Live order card:**
  - the number (tap to copy) and a countdown;
  - the code as soon as it arrives, also sent as a message;
  - cancel (locked for the first minutes, the way the supplier works) and "📱 New number".
- **Everyday screens:** 🧾 My orders, 🔁 Buy again, 💰 Balance with their ID and how to top up, and 🆘 Support with the reseller's contact.
- **14 languages**, picked from the phone's language (and switchable): English, Russian, Arabic, Spanish, Portuguese, French, Indonesian, Hindi, Bengali, Turkish, Persian, Urdu, Chinese, Vietnamese.
- Fully white-label: customers only ever see the reseller's bot.

## Money rules (tested)

- **Holding the price:** buying holds the customer's price on their balance first, and only then buys at NumberHub (`POST /numbers` with `max_price` and an `Idempotency-Key`). Any failure gives the hold back.
- **Lost replies:** a lost reply is retried with the same key, so a number is never bought twice. A purchase whose reply never came back is finished by the recovery sweep.
- **Pricing:** the customer's price is the markup on NumberHub's **ceiling** for the route (`price_max`), the most NumberHub can charge the reseller. A sale can never cost the reseller more than the customer paid.
- **When an order ends:** the hold is charged if a code arrived and released if not, exactly once (a claim and the balance update share one transaction).
- **Atomic balance changes:** every change to a customer's balance is a single conditional SQL update with an audit row. Reserved credit can't be removed.
- **Encrypted secrets:** bot tokens and API keys are stored encrypted (Fernet, `SECRET_KEY`).

## Run

```bash
pip install -r requirements.txt
cp .env.example .env      # BUILDER_BOT_TOKEN, ADMIN_IDS, SECRET_KEY (see the file)
python main.py
```

One process runs the builder bot, every reseller bot and the order sync (one `GET /orders` per reseller every 5 s, well inside the API's 60 requests / 10 s per key). Platform operators (`ADMIN_IDS`) get `/platform`, `/disable <id>` and `/enable <id>` in the builder bot.

## Tests

```bash
python tests/run_tests.py     # RESULT: N passed, M failed
python tests/preview.py en    # print every screen (text + buttons) in a language
```

The tests run the real code against an in-memory NumberHub API (`tests/fakes.py`, the same JSON shapes, idempotency and errors as the real one) and a fake Telegram session.
