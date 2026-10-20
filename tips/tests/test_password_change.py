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
        session["pw_code"]["at"] -= 5 * 60 + 1
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


class SamePasswordTests(TestCase):
    def setUp(self):
        self.user = make_manager("owner")
        self.user.email = "owner@example.com"
        self.user.set_password("old-pass-123!")
        self.user.save()

    @override_settings(EMAIL_ENABLED=False)
    def test_change_to_the_same_password_is_refused(self):
        self.client.force_login(self.user)
        page = self.client.post(reverse("password_change"), {"old_password": "old-pass-123!",
                                                             "new_password1": "old-pass-123!",
                                                             "new_password2": "old-pass-123!"})
        self.assertContains(page, "current password")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("old-pass-123!"))

    def test_forgot_password_link_works_and_refuses_the_same_password(self):
        self.client.post(reverse("password_reset"), {"email": "owner@example.com"})
        self.assertEqual(len(mail.outbox), 1)
        link = re.search(r"https?://[^/]+(/password/reset/\S+/)", mail.outbox[0].body).group(1)
        form_page = self.client.get(link, follow=True)
        url = form_page.redirect_chain[-1][0] if form_page.redirect_chain else link
        same = self.client.post(url, {"new_password1": "old-pass-123!", "new_password2": "old-pass-123!"})
        self.assertContains(same, "current password")
        done = self.client.post(url, {"new_password1": "Brand-new-77", "new_password2": "Brand-new-77"})
        self.assertRedirects(done, reverse("password_reset_complete"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("Brand-new-77"))


@override_settings(RATELIMIT_ENABLED=True)
class WrongPasswordPauseTests(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.user = make_manager("owner")
        self.user.email = "owner@example.com"
        self.user.set_password("right-pass-123")
        self.user.save()

    def login(self, password):
        return self.client.post(reverse("login"), {"username": "owner@example.com", "password": password})

    def test_ten_wrong_in_a_row_pause_and_warn_the_owner(self):
        for _ in range(9):
            self.login("nope")
        self.assertEqual(len(mail.outbox), 0)
        page = self.login("nope")
        self.assertContains(page, "10 times in a row")
        self.assertEqual(mail.outbox[-1].to, ["owner@example.com"])
        self.assertIn("kept trying", mail.outbox[-1].subject)
        self.assertNotEqual(self.login("right-pass-123").status_code, 302)  # paused, even with the right one
        from django.core.cache import cache
        cache.delete("login-pause:owner@example.com")  # a minute later
        self.assertEqual(self.login("right-pass-123").status_code, 302)

    def test_a_right_password_resets_the_count(self):
        for _ in range(9):
            self.login("nope")
        self.assertEqual(self.login("right-pass-123").status_code, 302)
        self.client.logout()
        for _ in range(9):
            self.login("nope")
        self.assertEqual(len(mail.outbox), 0)
