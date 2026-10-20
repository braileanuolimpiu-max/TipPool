"""Platform settings: everything that used to need code or .env is set up on the website."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.mail import EmailMessage
from django.test import TestCase, override_settings
from django.urls import reverse

from tips import ai, platform
from tips.models import PlatformConfig
from tips.paypal import SANDBOX_API, get_client
from tips.tests.helpers import make_manager


class OperatorTests(TestCase):
    def test_first_real_signup_runs_the_site(self):
        data = {"business": "Cafe One", "business_type": "cafe", "timezone": "UTC", "name": "Ana Owner",
                "email": "ana@example.com", "password": "Strong-pass-2026"}
        self.client.post(reverse("signup"), data)
        ana = get_user_model().objects.get(email="ana@example.com")
        self.assertTrue(ana.is_superuser)
        self.assertEqual(self.client.get(reverse("platform_settings")).status_code, 200)
        self.client.logout()
        self.client.post(reverse("signup"), {**data, "business": "Cafe Two", "email": "bo@example.com"})
        self.assertFalse(get_user_model().objects.get(email="bo@example.com").is_superuser)
        self.assertEqual(self.client.get(reverse("platform_settings")).status_code, 403)

    def test_claim_when_nobody_runs_the_site(self):
        owner = make_manager("owner")
        self.client.force_login(owner)
        self.assertContains(self.client.get(reverse("platform_settings")), "I run this site")
        self.client.post(reverse("claim_platform"), {"code": "WRONG"})
        owner.refresh_from_db()
        self.assertFalse(owner.is_superuser)  # the setup code from the server's log is needed
        self.client.post(reverse("claim_platform"), {"code": platform.setup_code().lower()})
        owner.refresh_from_db()
        self.assertTrue(owner.is_superuser)
        other = make_manager("other", owner.manager.business, owner=False)
        self.client.force_login(other)
        self.client.post(reverse("claim_platform"), {"code": platform.setup_code()})
        other.refresh_from_db()
        self.assertFalse(other.is_superuser)


class SettingsFromTheWebsiteTests(TestCase):
    def setUp(self):
        self.op = make_manager("op")
        self.op.is_superuser = True
        self.op.email = "op@example.com"
        self.op.save()
        self.client.force_login(self.op)
        self.url = reverse("platform_settings")

    def test_email_account_is_used_for_sending(self):
        with mock.patch("tips.operator.smtplib.SMTP") as smtp:
            self.client.post(self.url, {"section": "email", "email_provider": "gmail", "email_user": "me@gmail.com",
                                        "email_password": "abcd efgh ijkl mnop"})
        smtp.return_value.login.assert_called_once_with("me@gmail.com", "abcdefghijklmnop")
        self.assertEqual(platform.email_settings()[:4], ("smtp.gmail.com", 587, "me@gmail.com", "abcdefghijklmnop"))
        self.assertNotIn("abcdefghijklmnop", PlatformConfig.load().email_password_encrypted)
        from tips.mailer import PlatformBackend
        with mock.patch("tips.mailer.smtp_connection") as conn:
            conn.return_value.send_messages.return_value = 1
            PlatformBackend().send_messages([EmailMessage("Hi", "Body", to=["x@example.com"])])
        conn.assert_called_once()
        self.assertEqual(conn.call_args[0][:3], ("smtp.gmail.com", 587, "me@gmail.com"))

    def test_bad_email_login_is_not_saved(self):
        import smtplib
        with mock.patch("tips.operator.smtplib.SMTP") as smtp:
            smtp.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b"no")
            page = self.client.post(self.url, {"section": "email", "email_provider": "gmail",
                                               "email_user": "me@gmail.com", "email_password": "wrong"}, follow=True)
        self.assertContains(page, "app password")
        self.assertIsNone(platform.email_settings())

    def brevo_api(self, senders=({"email": "hello@tippool.app", "active": True},), key_ok=True,
                  refused="Key not found", status=401):
        """Brevo's API, faked: the account check, the senders list, and sending."""
        def get(url, **kwargs):
            if url.endswith("/account"):
                return mock.Mock(status_code=200 if key_ok else status, json=lambda: {"message": refused})
            if url.endswith("/senders"):
                return mock.Mock(status_code=200, json=lambda: {"senders": list(senders)})
            return mock.Mock(status_code=200, json=lambda: {"domains": []})
        return mock.patch("tips.mailer.requests.get", side_effect=get)

    def test_brevo_sends_through_its_web_api(self):
        with self.brevo_api(), mock.patch("tips.operator.smtplib.SMTP") as smtp:
            self.client.post(self.url, {"section": "email", "email_provider": "brevo", "email_user": "hello@tippool.app",
                                        "email_password": "xkeysib-abc"})
        smtp.assert_not_called()  # no email ports needed
        self.assertEqual(platform.email_settings()[2:4], ("hello@tippool.app", "xkeysib-abc"))
        from tips.mailer import PlatformBackend
        with mock.patch("tips.mailer.requests.post", return_value=mock.Mock(status_code=201)) as post:
            sent = PlatformBackend().send_messages([EmailMessage("Hi", "Body", to=["Ana <x@example.com>"])])
        self.assertEqual(sent, 1)
        body = post.call_args.kwargs["json"]
        self.assertEqual(post.call_args.kwargs["headers"]["api-key"], "xkeysib-abc")
        self.assertEqual(body["sender"], {"email": "hello@tippool.app", "name": "TipPool"})
        self.assertEqual(body["to"], [{"email": "x@example.com", "name": "Ana"}])
        self.assertEqual((body["subject"], body["textContent"]), ("Hi", "Body"))

    def test_brevo_explains_what_to_fix(self):
        cases = [({"key_ok": False}, "xkeysib-bad", "didn&#x27;t accept that API key"),
                 ({"key_ok": False, "refused": "We have detected you are using an unrecognised IP address 1.2.3.4"},
                  "xkeysib-abc", "Authorized IPs"),
                 ({"key_ok": False, "status": 403, "refused": "permission denied"}, "xkeysib-abc", "Account: read"),
                 ({"senders": ()}, "xkeysib-abc", "Add a sender"),
                 ({"senders": ({"email": "hello@tippool.app", "active": False},)}, "xkeysib-abc", "not confirmed"),
                 ({}, "xsmtpsib-abc", "SMTP key")]
        for fake, key, says in cases:
            with self.brevo_api(**fake):
                page = self.client.post(self.url, {"section": "email", "email_provider": "brevo",
                                                   "email_user": "hello@tippool.app", "email_password": key},
                                        follow=True)
            self.assertContains(page, says)
            self.assertIsNone(platform.email_settings())

    def test_brevo_refusing_a_send_shows_why(self):
        with self.brevo_api():
            self.client.post(self.url, {"section": "email", "email_provider": "brevo", "email_user": "hello@tippool.app",
                                        "email_password": "xkeysib-abc"})
        from tips.mailer import BrevoError, PlatformBackend
        refused = mock.Mock(status_code=400, json=lambda: {"message": "sender not valid"})
        with mock.patch("tips.mailer.requests.post", return_value=refused):
            with self.assertRaisesMessage(BrevoError, "sender not valid"):
                PlatformBackend().send_messages([EmailMessage("Hi", "Body", to=["x@example.com"])])
            self.assertEqual(PlatformBackend(fail_silently=True).send_messages(
                [EmailMessage("Hi", "Body", to=["x@example.com"])]), 0)

    def test_claude_key_from_the_website(self):
        with mock.patch("anthropic.Anthropic") as client:
            self.client.post(self.url, {"section": "ai", "ai_enabled": "on", "anthropic_key": "sk-ant-test",
                                        "claude_model": "claude-sonnet-5-5"})
        client.assert_called_with(api_key="sk-ant-test")
        self.assertEqual(ai.mode(), "claude")
        self.assertEqual((platform.anthropic_key(), platform.claude_model()), ("sk-ant-test", "claude-sonnet-5-5"))
        self.client.post(self.url, {"section": "ai"})  # switched off, key kept
        self.assertEqual(ai.mode(), "mock")

    def test_tippools_own_paypal_from_the_website(self):
        with mock.patch("tips.paypal.detect_mode", return_value=False), \
                mock.patch("tips.paypal.PayPalClient.ensure_webhook", return_value="WH-P"):
            self.client.post(self.url, {"section": "paypal", "paypal_client_id": "PCID", "paypal_secret": "PSEC"})
        client = get_client(None)
        self.assertEqual((client.client_id, client.client_secret, client.base_url), ("PCID", "PSEC", SANDBOX_API))
        self.assertTrue(platform.paypal_platform_fallback())  # test money may be shared with new businesses

    def test_scheduler_address_and_check_in(self):
        cfg = PlatformConfig.load()
        self.assertEqual(self.client.get(reverse("cron_tick") + "?key=wrong").status_code, 404)
        self.assertEqual(self.client.get(reverse("cron_tick") + f"?key={cfg.cron_secret}").status_code, 200)
        self.assertIsNotNone(PlatformConfig.load().last_tick_at)
        self.assertContains(self.client.get(self.url), "Checked in just now")

    def test_demo_and_contact(self):
        self.client.post(self.url, {"section": "demo", "demo_buyer_email": "buyer@sandbox.example",
                                    "demo_buyer_password": "pw", "staff_Maria": "maria@sandbox.example"})
        self.client.post(self.url, {"section": "contact", "contact_email": "hello@example.com"})
        self.assertEqual(platform.demo_buyer(), ("buyer@sandbox.example", "pw"))
        self.assertEqual(platform.demo_staff_email("Maria"), "maria@sandbox.example")
        self.assertContains(self.client.get(reverse("privacy")), "hello@example.com")

    def test_only_the_operator(self):
        helper = make_manager("helper", self.op.manager.business, owner=False)
        self.client.force_login(helper)
        self.client.post(self.url, {"section": "contact", "contact_email": "evil@example.com"})
        self.assertNotEqual(platform.contact_email(), "evil@example.com")


class ReviewFixTests(TestCase):
    def test_a_stranger_never_takes_over_a_site_already_in_use(self):
        make_manager("existing")  # a real business already here, nobody running the site
        self.client.post(reverse("signup"), {"business": "Stranger Co", "business_type": "cafe", "timezone": "UTC",
                                             "name": "Eve", "email": "eve@example.com", "password": "Strong-pass-2026"})
        self.assertFalse(get_user_model().objects.get(email="eve@example.com").is_superuser)

    @override_settings(CRON_SECRET="old-key")
    def test_the_environments_scheduler_key_keeps_working(self):
        cfg = PlatformConfig.load()
        self.assertEqual(cfg.cron_secret, "old-key")
        cfg.cron_secret = "new-key"
        cfg.save()
        for key in ("old-key", "new-key"):
            self.assertEqual(self.client.get(reverse("cron_tick") + f"?key={key}").status_code, 200, key)

    def test_open_redirect_on_freeze_is_closed(self):
        owner = make_manager("owner")
        self.client.force_login(owner)
        res = self.client.post(reverse("freeze_payouts"), {"frozen": "0", "next": "//evil.example/x"})
        self.assertNotIn("evil", res.url)

    def test_paypal_unreachable_is_a_message_not_a_crash(self):
        import requests
        owner = make_manager("owner")
        self.client.force_login(owner)
        with mock.patch("tips.paypal.requests.post", side_effect=requests.ConnectionError("down")):
            page = self.client.post(reverse("payment_settings"), {"paypal_client_id": "CID", "paypal_secret": "S",
                                                                  "currency": "USD", "fee_mode": "business"}, follow=True)
        self.assertContains(page, "be reached just now")

    def test_manager_with_only_archived_locations_sees_why(self):
        from tips.models import Location
        owner = make_manager("owner")
        business = owner.manager.business
        uptown = Location.objects.create(business=business, name="Uptown", slug="uptown2", archived=True)
        limited = make_manager("limited", business, owner=False)
        limited.manager.locations.set([uptown])
        self.client.force_login(limited)
        self.assertContains(self.client.get(reverse("dashboard")), "No open locations", status_code=403)


class LimitedManagerMessageTests(TestCase):
    def setUp(self):
        from tips.models import Alert, Location, Message, Night
        from tips.tests.helpers import hire
        owner = make_manager("owner")
        self.business = owner.manager.business
        self.main = Location.default()
        self.uptown = Location.objects.create(business=self.business, name="Uptown", slug="uptown")
        self.alert = Alert.objects.create(business=self.business, location=self.main, text="Main problem")
        ann = hire("Ann", "server", self.main)
        from tips.models import Membership
        Membership.objects.create(staff=ann, location=self.uptown, default_role="server")
        self.night = Night.objects.create(location=self.main, status=Night.Status.REVIEW, hold="Ann flagged a problem.")
        self.flag = Message.objects.create(staff=ann, location=self.main, night=self.night, text="hours wrong")
        limited = make_manager("uptown-mgr", self.business, owner=False)
        limited.manager.locations.set([self.uptown])
        self.client.force_login(limited)

    def test_cannot_dismiss_another_locations_alert(self):
        self.assertEqual(self.client.post(reverse("messages_page"), {"alert": self.alert.pk}).status_code, 404)
        self.alert.refresh_from_db()
        self.assertIsNone(self.alert.resolved_at)

    def test_cannot_answer_another_locations_flag(self):
        self.assertEqual(self.client.post(reverse("messages_page"), {"message": self.flag.pk, "reply": "ok"}).status_code, 404)
        self.night.refresh_from_db()
        self.assertTrue(self.night.hold)


class PolishRoundTests(TestCase):
    def test_a_failed_platform_save_keeps_what_was_typed(self):
        op = make_manager("op")
        op.is_superuser = True
        op.save()
        self.client.force_login(op)
        page = self.client.post(reverse("platform_settings"), {"section": "contact", "contact_email": "not an email"},
                                follow=True)
        self.assertContains(page, 'value="not an email"')

    def test_invite_email_names_the_locations(self):
        from django.core import mail
        from tips.models import Location
        owner = make_manager("owner")
        self.client.force_login(owner)
        uptown = Location.objects.create(business=owner.manager.business, name="Uptown", slug="uptown")
        self.client.post(reverse("invite_manager"), {"email": "new@example.com", "level": "lead",
                                                     "locations": [uptown.pk]})
        self.assertIn("a shift lead", mail.outbox[-1].body)
        self.assertIn("for Uptown", mail.outbox[-1].body)

    def test_clock_out_says_minutes(self):
        from datetime import timedelta
        from django.utils import timezone
        from tips.models import ClockEntry, Location, Night
        from tips.tests.helpers import hire
        location = Location.default()
        maria = hire("Maria", "server", location)
        ClockEntry.objects.create(staff=maria, night=Night.current(location),
                                  started_at=timezone.now() - timedelta(minutes=4))
        page = self.client.post(reverse("my_tips", args=[maria.access_token]), {"clock": "out"}, follow=True)
        self.assertContains(page, "You worked 4 minutes")

    def test_ask_does_not_offer_to_pay_while_frozen(self):
        from tips import actions
        from tips.models import Location, Night
        owner = make_manager("owner")
        business = owner.manager.business
        business.payouts_frozen_at, business.payouts_frozen_by = "2026-10-20T10:00Z", "Owner"
        business.save()
        location = Location.default()
        Night.objects.create(location=location, status=Night.Status.REVIEW)
        with self.assertRaisesMessage(actions.ActionError, "frozen"):
            actions.propose("pay_shift", {}, location, owner)
