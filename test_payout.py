"""First PayPal sandbox test: send a $1.00 payout from the restaurant to Maria.

Run:  python test_payout.py
Reads PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET and MARIA_EMAIL from the .env file.
"""
import time
import uuid
from pathlib import Path

import requests

BASE_URL = "https://api-m.sandbox.paypal.com"  # sandbox = fake money


def load_env(path=".env"):
    """Read KEY=value lines from .env into a dict (no extra library needed)."""
    env = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


def get_access_token(client_id, client_secret):
    """Step 1: log in to PayPal with the app's Client ID + Secret."""
    response = requests.post(
        f"{BASE_URL}/v1/oauth2/token",
        auth=(client_id, client_secret),
        data={"grant_type": "client_credentials"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def send_payout(token, receiver_email, amount):
    """Step 2: ask PayPal to send money to one person (a batch with 1 item)."""
    batch_id = f"olimp-test-{uuid.uuid4().hex[:8]}"
    body = {
        "sender_batch_header": {
            "sender_batch_id": batch_id,
            "email_subject": "You received tips!",
        },
        "items": [
            {
                "recipient_type": "EMAIL",
                "receiver": receiver_email,
                "amount": {"value": amount, "currency": "USD"},
                "note": "Test tip payout",
                "sender_item_id": f"{batch_id}-maria",
            }
        ],
    }
    response = requests.post(
        f"{BASE_URL}/v1/payments/payouts",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "PayPal-Request-Id": batch_id,  # stops duplicates if we retry
        },
        json=body,
        timeout=30,
    )
    if not response.ok:
        print("PayPal error:", response.status_code, response.text)
        response.raise_for_status()
    return response.json()["batch_header"]["payout_batch_id"]


def get_batch_status(token, payout_batch_id):
    """Step 3: check whether the payout finished."""
    response = requests.get(
        f"{BASE_URL}/v1/payments/payouts/{payout_batch_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["batch_header"]["batch_status"]


if __name__ == "__main__":
    env = load_env()
    token = get_access_token(env["PAYPAL_CLIENT_ID"], env["PAYPAL_CLIENT_SECRET"])
    print("1) Logged in to PayPal sandbox")

    batch = send_payout(token, env["MARIA_EMAIL"], "1.00")
    print("2) Payout sent, batch id:", batch)

    time.sleep(5)  # give PayPal a few seconds to process
    print("3) Batch status:", get_batch_status(token, batch))
