from django.core import mail
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from tips.models import Business, Change, Location
from tips.tests.helpers import hire, make_manager


class StaffLinkTests(TestCase):
    def setUp(self):
        cache.clear()
        self.location = Location.default()
        self.maria = hire("Maria", "server", email="maria@example.com")
        self.client.force_login(make_manager("boss"))

    def test_team_page_shows_the_full_link_to_copy(self):
        page = self.client.get(reverse("staff_list"))
        self.assertContains(page, f"http://testserver/me/{self.maria.access_token}/")
        self.assertContains(page, "data-copy")
        self.assertNotContains(page, "Their tips page")

    def test_new_link_replaces_the_old_one(self):
        old = self.maria.access_token
        self.client.post(reverse("staff_new_link", args=[self.maria.memberships.get().pk]))
        self.maria.refresh_from_db()
        self.assertNotEqual(self.maria.access_token, old)
        self.assertEqual(self.client.get(reverse("my_tips", args=[old])).status_code, 404)
        self.assertEqual(self.client.get(reverse("my_tips", args=[self.maria.access_token])).status_code, 200)
        self.assertTrue(Change.objects.filter(what__contains="new tips link for Maria").exists())

    def test_cannot_reset_another_business_link(self):
        other = Location.objects.create(business=Business.objects.create(name="Other"), name="Other", slug="o")
        sue = hire("Sue", "server", other)
        old = sue.access_token
        res = self.client.post(reverse("staff_new_link", args=[sue.memberships.get().pk]))
        self.assertEqual(res.status_code, 404)
        sue.refresh_from_db()
        self.assertEqual(sue.access_token, old)

    def test_staff_page_has_no_manager_banner(self):
        self.assertNotContains(self.client.get(reverse("my_tips", args=[self.maria.access_token])), "Back to team")


class EmailMyLinkTests(TestCase):
    def setUp(self):
        cache.clear()
        self.maria = hire("Maria", "server", email="maria@example.com")

    def test_link_is_emailed_to_that_address_only(self):
        res = self.client.post(reverse("email_my_link"), {"email": "MARIA@example.com"})
        self.assertContains(res, "Check your email")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["maria@example.com"])
        self.assertIn(f"/me/{self.maria.access_token}/", mail.outbox[0].body)

    def test_unknown_email_gets_the_same_answer_and_no_email(self):
        res = self.client.post(reverse("email_my_link"), {"email": "nobody@example.com"})
        self.assertContains(res, "Check your email")
        self.assertEqual(len(mail.outbox), 0)

    def test_one_email_per_two_minutes(self):
        self.client.post(reverse("email_my_link"), {"email": "maria@example.com"})
        self.client.post(reverse("email_my_link"), {"email": "maria@example.com"})
        self.assertEqual(len(mail.outbox), 1)

    def test_removed_staff_get_nothing(self):
        self.maria.memberships.update(active=False)
        self.client.post(reverse("email_my_link"), {"email": "maria@example.com"})
        self.assertEqual(len(mail.outbox), 0)


class StaffAppTests(TestCase):
    def test_totals_and_manifest(self):
        from decimal import Decimal

        from django.utils import timezone

        from tips.models import Night, Share
        maria = hire("Maria", "server")
        night = Night.objects.create(location=Location.default(), date=timezone.localdate(), status="paid")
        Share.objects.create(night=night, staff=maria, role="server", hours=Decimal("5"), amount=Decimal("42.50"))
        page = self.client.get(reverse("my_tips", args=[maria.access_token]))
        self.assertContains(page, "$42.50", count=4)  # week, month, year + the receipt
        self.assertContains(page, 'rel="manifest"')
        manifest = self.client.get(reverse("staff_manifest", args=[maria.access_token])).json()
        self.assertEqual(manifest["start_url"], f"/me/{maria.access_token}/")
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(self.client.get(reverse("staff_manifest", args=["nope"])).status_code, 404)
