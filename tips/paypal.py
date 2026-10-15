"""Small PayPal REST client (sandbox by default).

Covers what TipPool needs:
- Orders API v2: create + capture a tip
- Payouts API: pay the whole team in one batch
- Webhooks: verify that an incoming event really came from PayPal
"""
import logging
import time
import uuid

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
    def create_order(self, amount, currency="USD", description="Tip", reference_id=None):
        body = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": reference_id or "tip",
                "description": description,
                "amount": {"currency_code": currency, "value": f"{amount:.2f}"},
            }],
        }
        return self._request("POST", "/v2/checkout/orders", json=body)

    def capture_order(self, order_id):
        return self._request("POST", f"/v2/checkout/orders/{order_id}/capture", json={},
                             request_id=f"capture-{order_id}")

    def get_order(self, order_id):
        return self._request("GET", f"/v2/checkout/orders/{order_id}")

    # --- Payouts API (paying the team) ---------------------------------------
    def create_payout_batch(self, sender_batch_id, items, subject="You received tips!", currency="USD"):
        """items: list of dicts with receiver (email), amount (Decimal), note, sender_item_id."""
        body = {
            "sender_batch_header": {
                "sender_batch_id": sender_batch_id,
                "email_subject": subject,
                "email_message": "Your share of tonight's tips, paid with TipPool.",
            },
            "items": [{
                "recipient_type": "EMAIL",
                "receiver": item["receiver"],
                "amount": {"value": f"{item['amount']:.2f}", "currency": currency},
                "note": item.get("note", "")[:4000],
                "sender_item_id": item["sender_item_id"],
            } for item in items],
        }
        return self._request("POST", "/v1/payments/payouts", json=body, request_id=sender_batch_id)

    def get_payout_batch(self, paypal_batch_id):
        return self._request("GET", f"/v1/payments/payouts/{paypal_batch_id}")

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


_client = None


def get_client():
    """One shared client per process, so the OAuth token is reused."""
    global _client
    if _client is None:
        _client = PayPalClient()
    return _client
