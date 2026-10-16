# TipPool

**AI tip pooling for any business where staff share tips (restaurants, cafés, bars, salons, hotels...), built on PayPal.**

Guests tip with PayPal or Venmo by scanning a QR code. Staff clock in from their phone. At closing time
AI turns the business's plain-language tip rules into an exact, fair split, explains every person's share,
**checks the shift before any money moves**, and one click (or an automatic close-out) pays the whole team
with **PayPal Payouts**.

Built for the PayPal AI Hackathon 2026 ("Build What's Next with PayPal and AI").

## How it works

1. **Rules, in plain words.** The manager writes: *"Bartenders get 20%, bussers 10% split equally,
   the rest goes to servers by hours."* AI converts it into structured rules and flags problems,
   e.g. a manager taking from the pool (not allowed under US federal law, FLSA).
2. **Tips in (PayPal Orders API v2 + JS SDK v6).** One shared QR code per location (print as many copies
   as you need) or a personal code per person opens a tip page with PayPal and Venmo buttons. Orders are created and captured on the server. The tip buttons fit
   the business: % of the bill for restaurants and salons, small amounts for cafés.
3. **Card and cash tips too.** At close-out the manager adds the card-machine and cash tips, so the
   whole day's tips are shared, not only the QR ones.
4. **Who worked, without typing.** Staff clock in and out from their own tips page. Each person can
   also have a usual week (Mon–Fri 6h). The close-out fills itself in; the manager fixes exceptions.
5. **The split.** Plain Python does the money math in exact cents (the AI never decides amounts),
   so the shares always add up to the total. AI writes a short explanation for every person.
6. **Team tips or personal tips.** Pooled (restaurants), personal (salons, delivery), or the guest
   chooses. Personal tips go in full to that person, with personal QR codes for their station.
7. **Tips out (PayPal Payouts API).** One batch pays everyone straight to their PayPal or **Venmo**.
   Each worker confirms their own payout address; changes need a manager's approval. Failed or unclaimed
   payouts can be reminded, taken back and sent again. The PayPal balance is checked first.
   Automatic payouts can wait a review window.
8. **Staff can flag a mistake** ("my hours are wrong") before the money goes out. A flagged shift
   is never paid automatically; the manager answers in Messages.
9. **Several locations, any kind of business.** Each location has its own roles (presets for
   restaurant, café, bar, salon, hotel, delivery, or your own), rules, codes and closing times.
   People can work at several and keep one tips page.
10. **AI close-out check.** Before any payout, rules catch hard problems (a 15-hour shift, a forgotten
    clock-out, an unconfirmed address, a flagged problem, a total far from normal) and the AI reviews the
    shift, explains it and suggests fixes. **The AI can stop a payout, never start one.**
11. **Money handled honestly.** PayPal's fee is read from every payment: the business covers it, or it
    comes off before the split (allowed in the US). Refunds, chargebacks and disputes are tracked.
    Currency per business (USD, EUR, GBP, CAD, AUD).
12. **Business accounts.** Sign up, invite managers, connect **your own PayPal** (keys stored encrypted).
    Each business only ever sees its own data. An "All locations" overview and email alerts for the owner.
13. **Reports and history.** Tips per person, per role and per shift, CSV export for payroll, and a
    log of who changed what. **Ask about your tips** ("How much did bussers make last week?"): Claude
    answers from your numbers, and can **act** ("pay yesterday's shift", "set Maria to Mon–Fri 6h") once
    the manager confirms.

## PayPal APIs used
| API | Used for |
|---|---|
| Orders API v2 (create, capture) | collecting each tip |
| JS SDK v6 (`paypal-payments`, `venmo-payments`) | PayPal / Venmo buttons on the tip page |
| Payouts API (batch, status, cancel unclaimed item; PayPal + Venmo recipients) | paying the whole team in one call, fixing payouts that didn't arrive |
| Reporting: balances | checking there's enough PayPal balance before paying |
| Webhooks (+ signature verification per business) | `PAYMENT.CAPTURE.COMPLETED/REFUNDED/REVERSED`, `CUSTOMER.DISPUTE.CREATED`, `PAYMENT.PAYOUTS-ITEM.*`, `PAYMENT.PAYOUTSBATCH.*` |
| OAuth 2.0 client credentials | server authentication (token cached) |

## AI used
- **Claude API** (`claude-opus-5-5`, structured outputs) for: reading tip rules, reading schedule
  photos (vision), writing per-person explanations, the close-out check before payouts, and answering
  (and acting on) questions about your tips.
- **Guardrails:** the AI never decides amounts (exact-cent Python does), it can hold a payout but never
  release one, and every action it proposes needs a manager's confirmation (signed, expiring proposal).
- `AI_MODE=mock` runs a free built-in rule parser, template explanations and offline answers for
  common questions, so the app works without an API key.

## Run it locally (sandbox)

Requirements: Python 3.11+, a free PayPal Developer account.

```bash
git clone <this repo> && cd olimp-tippool
python -m venv .venv
.venv\Scripts\activate          # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env          # macOS/Linux: cp .env.example .env
```

Fill `.env`:
- `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`: from developer.paypal.com → Apps & Credentials → **Sandbox**
  (make sure **Payouts** is enabled for the app)
- `DEMO_STAFF`: your sandbox **personal** account emails, e.g. `Maria:server:sb-xxx@personal.example.com,...`
- optional: `AI_MODE=claude` and `ANTHROPIC_API_KEY`

```bash
python manage.py migrate
python manage.py seed_demo      # manager login + staff + starter rules
python manage.py runserver 8765
```

Open http://127.0.0.1:8765 and sign in with `manager` / `tippool-demo` (the demo business), or click
**Create a business account** to sign up a new one. Each business only sees its own locations, team and tips.
The owner can invite other managers from Settings → Managers. Staff never need an account: each person
has a private tips link.

Invite and password-reset emails print in the terminal until you set `EMAIL_HOST` (plus `EMAIL_HOST_USER`,
`EMAIL_HOST_PASSWORD`) in `.env`.

**Try the full flow:** open `/tip/` → pay with a sandbox *personal* account → "Close out"
→ tick who worked → review the split → "Approve & pay" → watch the payouts turn SUCCESS.

## Tests
```bash
python manage.py test tips
```
Covers the split engine (cents always add up, edge cases, legal warnings), the rule parser, the full
tip → split → payout flow with PayPal mocked, fees, payouts that fail or go unclaimed, refunds and disputes,
the close-out check, Ask actions, and that one business can never reach another's data.

## License
MIT
