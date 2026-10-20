"""Every email has the app's look (HTML) and a plain-text copy; codes and links can be copied."""
from django.core import mail
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from tips import emails
from tips.tests.helpers import make_manager


class EmailLookTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def html(self, message):
        return next(content for content, kind in message.alternatives if kind == "text/html")

    def test_a_code_is_big_selectable_and_in_the_text_copy(self):
        emails.send("a@example.com", "Your TipPool code: K7Q2MX", heading="Your code", code="K7Q2MX")
        message = mail.outbox[-1]
        self.assertIn("K7Q2MX", message.body)
        html = self.html(message)
        self.assertIn("user-select:all", html)
        self.assertIn(">K7Q2MX</span>", html)

    def test_links_get_a_button_and_a_copy_box(self):
        emails.send("a@example.com", "Hi", heading="Your link", button=("Open", "https://x.example/t/abc"),
                    links=[("Your private link", "https://x.example/t/abc")])
        html = self.html(mail.outbox[-1])
        self.assertIn('href="https://x.example/t/abc"', html)
        self.assertIn("Your private link", html)
        self.assertIn("https://x.example/t/abc", mail.outbox[-1].body)

    def test_text_is_escaped(self):
        emails.send("a@example.com", "Hi", heading="Note", quote="<script>alert(1)</script>")
        self.assertNotIn("<script>alert", self.html(mail.outbox[-1]))

    def test_the_password_code_email_uses_the_design(self):
        user = make_manager("ana")
        self.client.force_login(user)
        self.client.post(reverse("password_change"), {"action": "send"})
        message = mail.outbox[-1]
        self.assertTrue(message.subject.startswith("Your TipPool code: "))
        code = message.subject.rsplit(" ", 1)[-1]
        self.assertIn(f">{code}</span>", self.html(message))

    def test_the_reset_link_email_uses_the_design(self):
        make_manager("bo")
        self.client.post(reverse("password_reset"), {"email": "bo@example.com"})
        message = mail.outbox[-1]
        self.assertEqual(message.subject, "Reset your TipPool password")
        self.assertIn("/password/reset/", message.body)
        self.assertIn("Choose a new password", self.html(message))
