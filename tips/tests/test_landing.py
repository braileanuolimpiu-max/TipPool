"""The front page: visitors see what TipPool does; managers who are signed in go straight to their shift."""
import re

from django.test import TestCase
from django.urls import reverse

from tips.tests.helpers import make_manager


class LandingTests(TestCase):
    def test_visitors_see_the_front_page_with_the_ways_in(self):
        page = self.client.get(reverse("dashboard"))
        self.assertEqual(page.status_code, 200)
        self.assertTemplateUsed(page, "landing.html")
        html = page.content.decode()
        self.assertIn(f'action="{reverse("demo_start")}"', html)  # Try the demo
        self.assertIn(f'href="{reverse("login")}"', html)
        self.assertIn(f'href="{reverse("signup")}"', html)
        self.assertIn('name="csrfmiddlewaretoken"', html)  # the demo buttons post

    def test_signed_in_managers_go_straight_to_their_shift(self):
        self.client.force_login(make_manager("owner"))
        page = self.client.get(reverse("dashboard"))
        self.assertTemplateNotUsed(page, "landing.html")
        self.assertTemplateUsed(page, "tips/dashboard.html")

    def test_the_demo_button_works_from_the_front_page(self):
        res = self.client.post(reverse("demo_start"))
        self.assertRedirects(res, reverse("dashboard"))
        self.assertTemplateUsed(self.client.get(reverse("dashboard")), "tips/dashboard.html")

    def test_the_front_page_keeps_the_strict_security_policy(self):
        page = self.client.get(reverse("dashboard"))
        policy = page["Content-Security-Policy"]
        self.assertNotIn("unsafe-inline", policy)
        nonce = re.search(r"'nonce-([^']+)'", policy).group(1)
        html = page.content.decode()
        for tag in re.findall(r"<script\b[^>]*>", html):
            self.assertIn(f'nonce="{nonce}"', tag)
        self.assertIsNone(re.search(r"\sstyle=\"", html))
        self.assertIsNone(re.search(r"\son[a-z]+=\"", html))
