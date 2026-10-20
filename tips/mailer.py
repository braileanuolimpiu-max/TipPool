"""Sends email with the account saved on the Platform settings page. Without one, falls back to
whatever the environment chose (the terminal locally, nothing in production).

Brevo is used through its web API (https), so it also works on hosts that block email ports
(Render's free plan blocks SMTP). Gmail, Outlook and other services use SMTP."""
import base64
from email.utils import getaddresses, parseaddr

import requests
from django.conf import settings
from django.core.mail import get_connection
from django.core.mail.backends.base import BaseEmailBackend

from . import platform

BREVO_API = "https://api.brevo.com/v3"


class BrevoError(Exception):
    pass


def smtp_connection(host, port, user, password, fail_silently=False):
    return get_connection("django.core.mail.backends.smtp.EmailBackend", host=host, port=port, username=user,
                          password=password, use_tls=port != 465, use_ssl=port == 465, timeout=20,
                          fail_silently=fail_silently)


def _people(addresses):
    return [{"email": email, **({"name": name} if name else {})} for name, email in getaddresses(addresses) if email]


def brevo_payload(message):
    """One Django email message as Brevo's JSON."""
    name, email = parseaddr(message.from_email)
    payload = {"sender": {"email": email, **({"name": name} if name else {})}, "to": _people(message.to),
               "subject": message.subject, "textContent": message.body or " "}
    for key, value in (("cc", message.cc), ("bcc", message.bcc)):
        if value:
            payload[key] = _people(value)
    if message.reply_to:
        payload["replyTo"] = _people(message.reply_to[:1])[0]
    for content, mimetype in getattr(message, "alternatives", []):
        if mimetype == "text/html":
            payload["htmlContent"] = content
    files = []
    for attachment in message.attachments:
        if isinstance(attachment, tuple):
            filename, content = attachment[0], attachment[1]
            content = content.encode() if isinstance(content, str) else content
            files.append({"name": filename or "attachment", "content": base64.b64encode(content).decode()})
    if files:
        payload["attachment"] = files
    return payload


def brevo_send(api_key, message):
    response = requests.post(f"{BREVO_API}/smtp/email", json=brevo_payload(message), timeout=20,
                             headers={"api-key": api_key, "accept": "application/json"})
    if response.status_code >= 400:
        try:
            reason = response.json().get("message", "")
        except ValueError:
            reason = ""
        raise BrevoError(f"Brevo refused the email ({response.status_code}): {reason}"[:300])


def brevo_check(api_key, sender):
    """"" when the key works and `sender` is a confirmed sender in Brevo, else what to fix (in a sentence)."""
    headers = {"api-key": api_key, "accept": "application/json"}
    try:
        account = requests.get(f"{BREVO_API}/account", headers=headers, timeout=15)
        if account.status_code in (401, 403):
            try:
                said = str(account.json().get("message", ""))[:200]
            except ValueError:
                said = ""
            if "ip" in said.lower().split() or "ip address" in said.lower():
                return ("Brevo blocked this server's internet address. In Brevo: your name (top right) → Security → "
                        "Authorized IPs → turn off blocking of unknown IP addresses, then save here again.")
            if account.status_code == 403:  # the key is real but wasn't given this permission
                return ("Brevo knows that key, but it can't read your account. Make a key with these permissions: "
                        "Transactional emails: send (write), and Account: read. Everything else can stay off.")
            return ("Brevo didn't accept that API key" + (f" (Brevo says: {said})" if said else "") + ". Use an API "
                    "key (it starts with xkeysib-) from SMTP & API → API Keys, not the SMTP key.")
        if account.status_code >= 400:
            return "Brevo couldn't check that key just now. Try again in a minute."
        senders = requests.get(f"{BREVO_API}/senders", headers=headers, timeout=15)
    except requests.RequestException:
        return "Couldn't reach Brevo. Try again in a minute."
    if senders.status_code < 400:
        try:
            listed = senders.json().get("senders", [])
        except ValueError:
            return ""  # the key works; the send itself reports a sender problem
        known = {s.get("email", "").lower(): s.get("active", True) for s in listed}
        if sender.lower() not in known and _domain_verified(headers, sender):
            return ""  # any address at a domain verified in Brevo can send
        if sender.lower() not in known:
            return (f"{sender} isn't a sender in your Brevo account yet. In Brevo: Senders, Domains & Dedicated IPs "
                    "→ Senders → Add a sender, then click the link Brevo emails you.")
        if not known[sender.lower()]:
            return f"{sender} is in Brevo but not confirmed yet: click the link Brevo emailed to that address."
    return ""


def _domain_verified(headers, sender):
    domain = sender.rsplit("@", 1)[-1].lower()
    try:
        response = requests.get(f"{BREVO_API}/senders/domains", headers=headers, timeout=15)
        domains = response.json().get("domains", []) if response.status_code < 400 else []
    except (requests.RequestException, ValueError):
        return False
    return any(d.get("domain_name", "").lower() == domain and d.get("authenticated") for d in domains)


class PlatformBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        saved = platform.email_settings()
        if saved is None:
            fallback = get_connection(settings.EMAIL_FALLBACK_BACKEND, fail_silently=self.fail_silently)
            return fallback.send_messages(email_messages)
        host, port, user, password, sender = saved
        for message in email_messages:
            if message.from_email in ("", None, settings.DEFAULT_FROM_EMAIL):
                message.from_email = f"TipPool <{sender}>" if "<" not in sender else sender
        if platform.config().email_provider == "brevo":
            sent = 0
            for message in email_messages:
                try:
                    brevo_send(password, message)
                    sent += 1
                except (BrevoError, requests.RequestException):
                    if not self.fail_silently:
                        raise
            return sent
        return smtp_connection(host, port, user, password, self.fail_silently).send_messages(email_messages)
