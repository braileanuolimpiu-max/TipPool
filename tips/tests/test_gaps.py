import time
from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from tips import totp
from tips.models import Location, Manager, Night, Share, Tip
from tips.tests.helpers import hire, make_manager
from tips.vault import encrypt


class ShiftLeadTests(TestCase):
    def setUp(self):
        self.location = Location.default()
        self.lead = make_manager("lead", owner=False)
        Manager.objects.filter(user=self.lead).update(level="lead")
        self.client.force_login(self.lead)

    def test_lead_runs_the_shift_but_cannot_pay_or_change_things(self):
        for name in ("dashboard", "close_night", "qr_page", "messages_page", "history"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        for name in ("staff_list", "rules", "settings", "reports"):
            self.assertRedirects(self.client.get(reverse(name)), reverse("dashboard"), msg_prefix=name)
        self.assertNotContains(self.client.get(reverse("dashboard")), 'href="/settings/"')
        night = Night.objects.create(location=self.location, date=date(2026, 10, 15), status=Night.Status.REVIEW)
        self.assertRedirects(self.client.post(reverse("pay", args=[night.pk])), reverse("dashboard"))
        self.assertContains(self.client.get(reverse("review", args=[night.pk])), "Shift leads can check the split")

    def test_owner_invites_a_lead_and_changes_levels(self):
        owner = make_manager("owner2")
        self.client.force_login(owner)
        self.client.post(reverse("invite_manager"), {"email": "x@example.com", "level": "lead"})
        from tips.models import Invite
        self.assertEqual(Invite.objects.get().level, "lead")
        lead = self.lead.manager
        self.client.post(reverse("manager_level", args=[lead.pk]), {"level": "manager"})
        lead.refresh_from_db()
        self.assertEqual(lead.level, "manager")


class TeamEditTests(TestCase):
    def setUp(self):
        self.location = Location.default()
        self.client.force_login(make_manager("boss"))
        self.maria = hire("Maria", "server", email="maria@example.com")
        self.member = self.maria.memberships.get()

    def test_edit_name_and_email_needs_new_confirmation(self):
        self.client.post(reverse("staff_edit", args=[self.member.pk]), {"name": "Maria Pop", "paypal_email": "mp@example.com"})
        self.maria.refresh_from_db()
        self.assertEqual((self.maria.name, self.maria.paypal_email), ("Maria Pop", "mp@example.com"))
        self.assertIsNone(self.maria.payout_confirmed_at)

    def test_erase_keeps_amounts_and_removes_personal_data(self):
        night = Night.objects.create(location=self.location, date=date(2026, 10, 1), status=Night.Status.PAID)
        Share.objects.create(night=night, staff=self.maria, role="server", hours=Decimal("5"), amount=Decimal("40"))
        old_token = self.maria.access_token
        self.client.post(reverse("staff_erase", args=[self.member.pk]))
        self.maria.refresh_from_db()
        self.assertTrue(self.maria.name.startswith("Former team member"))
        self.assertNotIn("maria", self.maria.paypal_email)
        self.assertEqual(self.client.get(reverse("my_tips", args=[old_token])).status_code, 404)
        self.assertEqual(Share.objects.get(staff=self.maria).amount, Decimal("40.00"))

    def test_erase_refused_while_a_payout_waits(self):
        night = Night.objects.create(location=self.location, date=date(2026, 10, 1), status=Night.Status.REVIEW)
        Share.objects.create(night=night, staff=self.maria, role="server", hours=Decimal("5"), amount=Decimal("40"))
        self.client.post(reverse("staff_erase", args=[self.member.pk]))
        self.maria.refresh_from_db()
        self.assertEqual(self.maria.name, "Maria")


class ArchiveTests(TestCase):
    def setUp(self):
        self.first = Location.default()
        self.second = Location.objects.create(business=self.first.business, name="Second", slug="second")
        self.client.force_login(make_manager("boss"))

    def test_archive_hides_location_and_stops_codes(self):
        self.client.post(reverse("location_archive", args=[self.second.pk]))
        self.second.refresh_from_db()
        self.assertTrue(self.second.archived)
        self.assertEqual(self.client.get(reverse("tip_page") + "?at=second").status_code, 404)
        self.assertEqual([r["location"] for r in self.client.get(reverse("overview")).context["rows"]], [self.first])
        self.client.post(reverse("location_archive", args=[self.second.pk]), {"restore": "1"})
        self.second.refresh_from_db()
        self.assertFalse(self.second.archived)

    def test_cannot_archive_the_only_location(self):
        self.client.post(reverse("location_archive", args=[self.second.pk]))
        self.client.post(reverse("location_archive", args=[self.first.pk]))
        self.first.refresh_from_db()
        self.assertFalse(self.first.archived)


class TwoStepTests(TestCase):
    def setUp(self):
        self.user = make_manager("owner")
        self.secret = totp.new_secret()

    def test_codes(self):
        now = time.time()
        code = totp._code(self.secret, int(now // 30))
        self.assertTrue(totp.verify(self.secret, code, at=now))
        self.assertTrue(totp.verify(self.secret, code[:3] + " " + code[3:], at=now))
        self.assertFalse(totp.verify(self.secret, "000000" if code != "000000" else "111111", at=now))
        self.assertTrue(totp._code("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", 1) == "287082")  # RFC 6238 vector (59s)

    def test_login_needs_the_code(self):
        profile = self.user.manager
        profile.totp_secret_encrypted, profile.totp_enabled = encrypt(self.secret), True
        profile.save()
        res = self.client.post(reverse("login"), {"username": "owner@example.com", "password": "pw"})
        self.assertRedirects(res, reverse("login_code"))
        self.assertFalse(self.client.get(reverse("dashboard")).wsgi_request.user.is_authenticated)
        self.client.post(reverse("login_code"), {"code": "000000"})
        self.assertFalse(self.client.get(reverse("login_code")).wsgi_request.user.is_authenticated)
        code = totp._code(self.secret, int(time.time() // 30))
        res = self.client.post(reverse("login_code"), {"code": code})
        self.assertRedirects(res, reverse("dashboard"))

    def test_turn_on_from_settings(self):
        self.client.force_login(self.user)
        self.client.post(reverse("two_step"), {"action": "start"})
        secret = self.client.session["two_step_new"]
        self.assertContains(self.client.get(reverse("settings")), "data:image/svg+xml;base64")
        self.client.post(reverse("two_step"), {"action": "confirm", "code": totp._code(secret, int(time.time() // 30))})
        self.user.manager.refresh_from_db()
        self.assertTrue(self.user.manager.totp_enabled)
