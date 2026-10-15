"""Check a payout batch again later.

Run:  python check_payout.py S4X4BBBQT6F72
"""
import sys

import requests

from test_payout import BASE_URL, get_access_token, load_env

if __name__ == "__main__":
    batch_id = sys.argv[1]
    env = load_env()
    token = get_access_token(env["PAYPAL_CLIENT_ID"], env["PAYPAL_CLIENT_SECRET"])

    response = requests.get(
        f"{BASE_URL}/v1/payments/payouts/{batch_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()

    print("Batch status:", data["batch_header"]["batch_status"])
    for item in data.get("items", []):
        receiver = item["payout_item"]["receiver"]
        amount = item["payout_item"]["amount"]
        print(f"  {receiver}: {amount['value']} {amount['currency']} -> {item['transaction_status']}")
