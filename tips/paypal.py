"""Small PayPal REST client (sandbox by default).

Covers what TipPool needs:
- Orders API v2: create + capture a tip
- Payouts API: pay the whole team in one batch
- Webhooks: verify that an incoming event really came from PayPal
"""
import logging
import time
import uuid
from decimal import Decimal

import requests
from django.conf import settings

log = logging.getLogger("tips.paypal")


class PayPalError(Exception):
    def __init__(self, message, status=None, debug_id=None, details=None):
        super().__init__(message)
        self.status = status
        self.debug_id = debug_id
        self.details = details or {}


class PayPalClient:
    def __init__(self, client_id=None, client_secret=None, base_url=None):
        self.client_id = client_id or settings.PAYPAL_CLIENT_ID
        self.client_secret = client_secret or settings.PAYPAL_CLIENT_SECRET
        self.base_url = (base_url or settings.PAYPAL_BASE_URL).rstrip("/")
        self._token = None
        self._token_expires_at = 0

    # --- auth ---------------------------------------------------------------
    def access_token(self):
        """Get an OAuth token and cache it until shortly before it expires."""
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        if not self.client_id or not self.client_secret:
            raise PayPalError("PayPal credentials are missing. Fill PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET in .env")
        response = requests.post(
            f"{self.base_url}/v1/oauth2/token",
            auth=(self.client_id, self.client_secret),
            data={"grant_type": "client_credentials"},
            timeout=30,
        )
        if not response.ok:
            raise PayPalError("Could not log in to PayPal", response.status_code,
                              response.headers.get("PayPal-Debug-Id"))
        data = response.json()
        self._token = data["access_token"]
        self._token_expires_at = time.time() + int(data.get("expires_in", 3600))
        return self._token

    def _request(self, method, path, json=None, request_id=None, retries=3):
        headers = {"Authorization": f"Bearer {self.access_token()}", "Content-Type": "application/json"}
        if method == "POST":
            # same id on every retry, so PayPal never does the same thing twice
            headers["PayPal-Request-Id"] = request_id or str(uuid.uuid4())
        for attempt in range(retries):
            response = requests.request(method, f"{self.base_url}{path}", headers=headers, json=json, timeout=30)
            if response.status_code == 429 and attempt < retries - 1:
                wait = int(response.headers.get("Retry-After", 2 ** attempt))
                log.warning("PayPal rate limit, retrying in %ss", wait)
                time.sleep(wait)
                continue
            break
        if not response.ok:
            body = response.json() if response.content else {}
            debug_id = body.get("debug_id") or response.headers.get("PayPal-Debug-Id")
            log.error("PayPal %s %s failed: %s debug_id=%s body=%s", method, path,
                      response.status_code, debug_id, body)
            raise PayPalError(body.get("message") or f"PayPal error {response.status_code}",
                              response.status_code, debug_id, body)
        return response.json() if response.content else {}

    # --- Orders API v2 (collecting tips) -------------------------------------
    def create_order(self, amount, currency="USD", description="Tip", reference_id=None,
                     return_url=None, cancel_url=None):
        body = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": reference_id or "tip",
                "description": description,
                "amount": {"currency_code": currency, "value": f"{amount:.2f}"},
            }],
        }
        experience = {
            "shipping_preference": "NO_SHIPPING",  # a tip has nothing to ship
            "user_action": "PAY_NOW",              # show "Pay Now", not "Continue to Review Order"
        }
        if return_url and cancel_url:
            experience.update({"return_url": return_url, "cancel_url": cancel_url})
        body["payment_source"] = {"paypal": {"experience_context": experience}}
        return self._request("POST", "/v2/checkout/orders", json=body)

    def capture_order(self, order_id):
        return self._request("POST", f"/v2/checkout/orders/{order_id}/capture", json={},
                             request_id=f"capture-{order_id}")

    def get_order(self, order_id):
        return self._request("GET", f"/v2/checkout/orders/{order_id}")

    # --- Payouts API (paying the team) ---------------------------------------
    def create_payout_batch(self, sender_batch_id, items, subject="You received tips!", currency="USD"):
        """items: dicts with receiver, amount (Decimal), note, sender_item_id, and optionally
        recipient_type ("EMAIL" default, "PHONE") and wallet ("VENMO")."""
        body = {
            "sender_batch_header": {
                "sender_batch_id": sender_batch_id,
                "email_subject": subject,
                "email_message": "Your share of the tips, paid with TipPool.",
            },
            "items": [],
        }
        for item in items:
            row = {
                "recipient_type": item.get("recipient_type", "EMAIL"),
                "receiver": item["receiver"],
                "amount": {"value": f"{item['amount']:.2f}", "currency": currency},
                "note": item.get("note", "")[:4000],
                "sender_item_id": item["sender_item_id"],
            }
            if item.get("wallet"):
                row["recipient_wallet"] = item["wallet"]
            body["items"].append(row)
        return self._request("POST", "/v1/payments/payouts", json=body, request_id=sender_batch_id)

    def get_payout_batch(self, paypal_batch_id):
        return self._request("GET", f"/v1/payments/payouts/{paypal_batch_id}")

    def cancel_payout_item(self, payout_item_id):
        """Take back an UNCLAIMED payout (the money returns to the business)."""
        return self._request("POST", f"/v1/payments/payouts-item/{payout_item_id}/cancel", json={},
                             request_id=f"cancel-{payout_item_id}")

    def available_balance(self, currency="USD"):
        """The PayPal balance available for payouts, or None if PayPal won't tell us."""
        try:
            data = self._request("GET", f"/v1/reporting/balances?currency_code={currency}")
        except PayPalError:
            return None
        for row in data.get("balances", []):
            if row.get("currency") == currency:
                value = (row.get("available_balance") or row.get("total_balance") or {}).get("value")
                return Decimal(value) if value is not None else None
        return None

    # --- Webhooks ------------------------------------------------------------
    def verify_webhook(self, headers, event, webhook_id):
        """Ask PayPal whether this webhook is genuine. Returns True/False."""
        body = {
            "auth_algo": headers.get("PAYPAL-AUTH-ALGO"),
            "cert_url": headers.get("PAYPAL-CERT-URL"),
            "transmission_id": headers.get("PAYPAL-TRANSMISSION-ID"),
            "transmission_sig": headers.get("PAYPAL-TRANSMISSION-SIG"),
            "transmission_time": headers.get("PAYPAL-TRANSMISSION-TIME"),
            "webhook_id": webhook_id,
            "webhook_event": event,
        }
        result = self._request("POST", "/v1/notifications/verify-webhook-signature", json=body)
        return result.get("verification_status") == "SUCCESS"


SANDBOX_API = "https://api-m.sandbox.paypal.com"
LIVE_API = "https://api-m.paypal.com"
SANDBOX_SDK = "https://www.sandbox.paypal.com/web-sdk/v6/core"
LIVE_SDK = "https://www.paypal.com/web-sdk/v6/core"

_clients = {}


def get_client(business=None):
    """The PayPal account money goes through: the business's own, once connected in Settings.
    Until then (sandbox demo only), the TipPool platform's sandbox account.
    One client per account per process, so OAuth tokens are reused."""
    if business is not None and business.paypal_connected:
        key = (business.pk, business.paypal_client_id, business.paypal_live, business.paypal_secret_encrypted)
        if key not in _clients:
            _clients[key] = PayPalClient(business.paypal_client_id, business.paypal_secret,
                                         LIVE_API if business.paypal_live else SANDBOX_API)
        return _clients[key]
    if business is not None and not settings.PAYPAL_PLATFORM_FALLBACK:
        raise PayPalError("Connect your business's PayPal account in Settings first.")
    if "platform" not in _clients:
        _clients["platform"] = PayPalClient()
    return _clients["platform"]


def checkout_config(business):
    """Client id + SDK URL for the guest tip page (the browser talks to the same PayPal account)."""
    if business is not None and business.paypal_connected:
        return business.paypal_client_id, (LIVE_SDK if business.paypal_live else SANDBOX_SDK)
    return settings.PAYPAL_CLIENT_ID, settings.PAYPAL_SDK_URL


def webhook_id_for(business):
    if business is not None and business.paypal_connected:
        return business.paypal_webhook_id
    return settings.PAYPAL_WEBHOOK_ID
