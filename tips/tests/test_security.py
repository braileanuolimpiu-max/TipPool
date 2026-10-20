import json
import os
import subprocess
import sys
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from tips import reports, services
from tips.models import Invite, Location, Manager, Night, Role, Tip
from tips.tests.helpers import hire, make_manager
from tips.tests.test_money import FakePayPal


@override_settings(RATELIMIT_ENABLED=True)
class RateLimitTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        make_manager("boss")

    def test_password_guessing_is_stopped(self):
        for _ in range(10):
            self.client.post(reverse("login"), {"username": "boss@example.com", "password": "wrong"})
        res = self.client.post(reverse("login"), {"username": "boss@example.com", "password": "pw"})
        self.assertContains(res, "Too many wrong tries")  # even the right password waits a while
        self.assertFalse(res.wsgi_request.user.is_authenticated)

    def test_successful_logins_never_lock_anyone_out(self):
        for _ in range(15):
            res = self.client.post(reverse("login"), {"username": "boss@example.com", "password": "pw"})
            self.assertEqual(res.status_code, 302)
            self.client.logout()

    def test_tip_orders_per_ip(self):
        with mock.patch("tips.services.get_client", return_value=FakePayPal()):
            codes = [self.client.post(reverse("api_create_order"), content_type="application/json",
                                      data=json.dumps({"amount": "5", "at": Location.default().slug})).status_code
                     for _ in range(22)]
        self.assertEqual(codes[:20], [200] * 20)
        self.assertEqual(codes[-1], 429)

    @override_settings(TRUSTED_PROXY_COUNT=1)
    def test_made_up_forwarded_for_does_not_help(self):
        for n in range(50):
            self.client.post(reverse("login"), {"username": f"guess{n}@example.com", "password": "x"},
                             HTTP_X_FORWARDED_FOR=f"10.0.0.{n}, 1.2.3.4")
        res = self.client.post(reverse("login"), {"username": "someone@else.com", "password": "x"},
                               HTTP_X_FORWARDED_FOR="9.9.9.9, 1.2.3.4")
        self.assertContains(res, "Too many wrong tries")  # same real address (1.2.3.4) keeps counting

    def test_whole_team_on_one_wifi_can_clock_in(self):
        people = [hire(f"P{n}", "server") for n in range(12)]
        for person in people:
            for _ in range(3):
                res = self.client.post(reverse("my_tips", args=[person.access_token]), {"clock": "out"})
                self.assertNotEqual(res.status_code, 429)


@override_settings(AI_MODE="mock")
class HardeningTests(TestCase):
    def setUp(self):
        self.location = Location.default()
        self.owner = make_manager("owner")
        self.client.force_login(self.owner)

    def test_invite_only_for_its_email(self):
        self.client.post(reverse("invite_manager"), {"email": "sam@example.com"})
        invite = Invite.objects.get()
        stranger = get_user_model().objects.create_user("eve", email="eve@example.com", password="pw")
        self.client.force_login(stranger)
        res = self.client.post(reverse("accept_invite", args=[invite.token]))
        self.assertContains(res, "This invite is for sam@example.com")
        self.assertFalse(Manager.objects.filter(user=stranger).exists())

    def test_invite_links_hidden_from_non_owners(self):
        self.client.post(reverse("invite_manager"), {"email": "sam@example.com"})
        token = Invite.objects.get().token
        self.client.force_login(make_manager("helper", owner=False))
        self.assertNotContains(self.client.get(reverse("settings")), token)

    def test_csv_cells_are_not_formulas(self):
        self.assertEqual(reports._cell("=HYPERLINK(\"http://evil\")"), "'=HYPERLINK(\"http://evil\")")
        self.assertEqual(reports._cell("-12.50"), "-12.50")
        self.assertEqual(reports._cell("Maria"), "Maria")

    def test_no_personal_tips_to_no_tips_roles(self):
        self.location.tip_mode = Location.TipMode.BOTH
        self.location.save()
        boss = hire("Boss", "manager")
        member = boss.memberships.get()
        self.assertTrue(Role.objects.get(location=self.location, key="manager").no_tips)
        res = self.client.post(reverse("api_create_order"), content_type="application/json",
                               data=json.dumps({"amount": "5", "at": self.location.slug, "to": member.pk}))
        self.assertEqual(res.status_code, 400)

    def test_bad_inputs_are_400s_not_crashes(self):
        for body in ([1], {"amount": "5", "at": self.location.slug, "to": [1]}, {"amount": "x", "at": self.location.slug}):
            res = self.client.post(reverse("api_create_order"), data=json.dumps(body), content_type="application/json")
            self.assertIn(res.status_code, (400, 404))
        long_url = "http://testserver/" + "a" * 5000
        self.assertEqual(self.client.get(reverse("qr_svg") + "?url=" + long_url).status_code, 400)

    def test_switch_location_never_redirects_away(self):
        for nxt in ("//evil.com", "/\\evil.com", "https://evil.com/"):
            res = self.client.post(reverse("location_switch"), {"location": self.location.pk, "next": nxt})
            self.assertEqual(res["Location"], reverse("dashboard"))

    def test_ask_confirmation_works_once(self):
        with mock.patch("tips.services.get_client", return_value=FakePayPal()):
            page = self.client.get(reverse("reports") + "?q=add+%2440+cash+tips")
            token = page.context["asked"]["proposal"]["token"]
            self.client.post(reverse("ask_confirm"), {"token": token})
            res = self.client.post(reverse("ask_confirm"), {"token": token}, follow=True)
        self.assertContains(res, "already done")
        self.assertEqual(Night.current(self.location).tip_total, Decimal("40.00"))

    def test_abandoned_orders_expire(self):
        night = Night.current(self.location)
        old = Tip.objects.create(night=night, amount=Decimal("5"), paypal_order_id="OLD")
        Tip.objects.filter(pk=old.pk).update(created_at=old.created_at - __import__("datetime").timedelta(hours=4))
        with mock.patch("tips.services.get_client", return_value=FakePayPal()):
            services.sync_pending_tips(night)
        old.refresh_from_db()
        self.assertEqual(old.status, Tip.Status.FAILED)


class ProductionSettingsTests(TestCase):
    """The app refuses to start in production without its secrets, instead of running insecurely."""

    def start(self, **env):
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("DJANGO_", "TIPPOOL_", "DATABASE_URL"))}
        clean.update(env)
        return subprocess.run([sys.executable, "-c", "import django, os; os.environ.setdefault('DJANGO_SETTINGS_MODULE',"
                               "'config.settings'); django.setup()"], cwd=settings.BASE_DIR, env=clean,
                              capture_output=True, text=True)

    def test_missing_secrets_stop_startup(self):
        result = self.start(DJANGO_DEBUG="0", DJANGO_SECRET_KEY="")
        self.assertIn("DJANGO_SECRET_KEY must be set", result.stderr)
        result = self.start(DJANGO_DEBUG="0", DJANGO_SECRET_KEY="k" * 50, DATABASE_URL="postgres://u:p@h/db")
        self.assertIn("TIPPOOL_ENCRYPTION_KEY must be set", result.stderr)
        result = self.start(DJANGO_DEBUG="0", DJANGO_SECRET_KEY="k" * 50, TIPPOOL_ENCRYPTION_KEY="x")
        self.assertIn("DATABASE_URL", result.stderr)
