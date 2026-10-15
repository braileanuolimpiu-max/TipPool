# Olimp TipPool

AI tip pooling for restaurants, built on PayPal.

Customers tip with PayPal. At closing, AI turns the manager's plain-language tip rules
and the shift schedule into a fair split, explains each person's share, and the manager
pays the whole team in one batch with PayPal Payouts (PayPal or Venmo).

Built for the PayPal AI Hackathon 2026 ("Build What's Next with PayPal and AI").

> Work in progress. Setup and run instructions will be added as the app is built.

## Current status
- `test_payout.py`: sends a $1.00 sandbox payout (PayPal Payouts API)
- `check_payout.py <batch_id>`: checks a payout batch status

## Setup (sandbox)
1. Copy `.env.example` to `.env` and fill in your PayPal **sandbox** Client ID, Secret and a test recipient email.
2. `pip install requests`
3. `python test_payout.py`

## License
MIT
