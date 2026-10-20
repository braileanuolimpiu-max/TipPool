import re

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from tips.tests.helpers import make_manager


class EmailCodePasswordChangeTests(TestCase):
    def setUp(self):
        self.user = make_manager("owner")
        self.user.email = "owner@example.com"
        self.user.set_password("old-pass-123!")
        self.user.save()
        self.client.force_login(self.user)
        self.url = reverse("password_change")

    def code(self):
        self.client.post(self.url, {"action": "send"})
        return re.search(r"code: ([A-Z0-9]{6})", mail.outbox[-1].subject).group(1)

    def test_code_then_new_password(self):
        self.assertContains(self.client.get(self.url), "Email me a code")
        code = self.code()
        self.assertEqual(mail.outbox[-1].to, ["owner@example.com"])
        self.assertContains(self.client.get(self.url), "Code from the email")
        # no shortcut: the new password can't be set before the code
        self.client.post(self.url, {"action": "set", "new_password1": "Brand-new-77", "new_password2": "Brand-new-77"})
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("old-pass-123!"))
        self.client.post(self.url, {"action": "verify", "code": code.lower()[:3] + " " + code.lower()[3:]})
        res = self.client.post(self.url, {"action": "set", "new_password1": "Brand-new-77",
                                          "new_password2": "Brand-new-77"})
        self.assertRedirects(res, reverse("password_change_done"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("Brand-new-77"))
        self.assertIn("was changed", mail.outbox[-1].subject)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)  # still signed in here

    def test_wrong_codes_run_out(self):
        self.code()
        for _ in range(5):
            self.client.post(self.url, {"action": "verify", "code": "ZZZZZZ"})
        self.assertContains(self.client.get(self.url), "Email me a code")  # back to the start

    def test_codes_expire(self):
        self.code()
        session = self.client.session
        session["pw_code"]["at"] -= 11 * 60
        session.save()
        self.assertContains(self.client.get(self.url), "Email me a code")

    @override_settings(EMAIL_ENABLED=False)
    def test_without_email_the_current_password_is_asked(self):
        page = self.client.get(self.url)
        self.assertContains(page, "Current password")
        self.client.post(self.url, {"old_password": "old-pass-123!", "new_password1": "Brand-new-77",
                                    "new_password2": "Brand-new-77"})
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("Brand-new-77"))

    def test_signed_out_people_go_to_sign_in(self):
        self.client.logout()
        self.assertIn(reverse("login"), self.client.get(self.url).url)
