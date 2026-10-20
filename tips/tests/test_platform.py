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
        self.client.post(reverse("claim_platform"))
        owner.refresh_from_db()
        self.assertTrue(owner.is_superuser)
        other = make_manager("other", owner.manager.business, owner=False)
        self.client.force_login(other)
        self.client.post(reverse("claim_platform"))
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
