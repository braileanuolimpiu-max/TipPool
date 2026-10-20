"""Owner features: per-location managers, owner controls, smart alerts, digest, live board, activity, bulk add."""
from datetime import timedelta
from decimal import Decimal

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from tips import digest, insights, watch
from tips.models import (Alert, Change, ClockEntry, Location, Membership, Night, StaffMember,
                         Tip)
from tips.tests.helpers import hire, make_manager


class PerLocationManagerTests(TestCase):
    def setUp(self):
        self.owner = make_manager("owner")
        self.business = self.owner.manager.business
        self.main = Location.default()
        self.second = Location.objects.create(business=self.business, name="Uptown", slug="uptown")
        self.manager = make_manager("manager2", self.business, owner=False)
        self.manager.manager.locations.set([self.second])
        self.client.force_login(self.manager)

    def test_manager_only_sees_their_locations(self):
        page = self.client.get(reverse("dashboard"))
        self.assertEqual(page.context["location"], self.second)
        self.assertEqual(self.client.post(reverse("location_switch"), {"location": self.main.pk}).status_code, 404)
        self.assertNotContains(self.client.get(reverse("overview")), self.main.display_name)

    def test_owner_picks_locations(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("manager_locations", args=[self.manager.manager.pk]), {"locations": [self.main.pk]})
        self.assertEqual(list(self.manager.manager.locations.all()), [self.main])
        self.client.post(reverse("manager_locations", args=[self.manager.manager.pk]), {})
        self.assertTrue(self.manager.manager.all_access)

    def test_limited_manager_cannot_add_locations(self):
        self.client.post(reverse("location_add"), {"name": "Sneaky"})
        self.assertFalse(Location.objects.filter(name="Sneaky").exists())

    def test_alert_emails_go_to_who_runs_the_location(self):
        self.owner.email, self.manager.email = "owner@example.com", "m2@example.com"
        self.owner.save()
        self.manager.save()
        from tips.alerts import raise_alert
        raise_alert(self.business, "Something at main", location=self.main)
        self.assertEqual(mail.outbox[-1].to, ["owner@example.com"])
        before = len(mail.outbox)
        raise_alert(self.business, "Something uptown", location=self.second)
        # one email each, so nobody sees the other managers' addresses
        self.assertEqual(sorted(m.to[0] for m in mail.outbox[before:]), ["m2@example.com", "owner@example.com"])
        self.assertTrue(all(len(m.to) == 1 for m in mail.outbox[before:]))


class OwnerControlTests(TestCase):
    def setUp(self):
        self.owner = make_manager("owner")
        self.business = self.owner.manager.business
        self.client.force_login(self.owner)

    def test_save_controls(self):
        self.client.post(reverse("owner_controls"), {"approval_limit": "1500", "digest": "daily"})
        self.business.refresh_from_db()
        self.assertEqual(self.business.approval_limit, Decimal("1500.00"))
        self.assertFalse(self.business.smart_alerts)
        self.assertEqual(self.business.digest, "daily")
        self.client.post(reverse("owner_controls"), {"approval_limit": "", "smart_alerts": "on", "digest": "off"})
        self.business.refresh_from_db()
        self.assertIsNone(self.business.approval_limit)

    def test_freeze_and_unfreeze(self):
        self.client.post(reverse("freeze_payouts"), {"frozen": "1"})
        self.business.refresh_from_db()
        self.assertTrue(self.business.payouts_frozen)
        self.assertContains(self.client.get(reverse("dashboard")), "All payouts are frozen")
        self.client.post(reverse("freeze_payouts"), {"frozen": "0"})
        self.business.refresh_from_db()
        self.assertFalse(self.business.payouts_frozen)

    def test_only_the_owner(self):
        other = make_manager("other", self.business, owner=False)
        self.client.force_login(other)
        self.client.post(reverse("freeze_payouts"), {"frozen": "1"})
        self.client.post(reverse("owner_controls"), {"approval_limit": "5"})
        self.business.refresh_from_db()
        self.assertFalse(self.business.payouts_frozen)
        self.assertIsNone(self.business.approval_limit)


class InsightTests(TestCase):
    def setUp(self):
        self.location = Location.default()
        self.now = timezone.now()

    def past_night(self, weeks, amounts, hour_offset=-1):
        night = Night.objects.create(location=self.location, date=self.today - timedelta(weeks=weeks),
                                     status=Night.Status.PAID)
        for a in amounts:
            tip = Tip.objects.create(night=night, amount=Decimal(a), status=Tip.Status.COMPLETED, paypal_order_id=None)
            Tip.objects.filter(pk=tip.pk).update(created_at=self.now - timedelta(weeks=weeks, hours=-hour_offset))
        return night

    def test_quiet_shift_raises_an_alert_once(self):
        from tips.autoclose import location_tz
        self.today = self.now.astimezone(location_tz(self.location)).date()
        for w in (1, 2, 3):
            self.past_night(w, [10, 10, 10, 10], hour_offset=-1)  # tips an hour earlier than now on past weeks
        Night.objects.create(location=self.location, date=self.today)
        pace = insights.pace(self.location, self.now)
        self.assertEqual(pace["status"], "quiet")
        self.assertEqual(pace["normal_now"], Decimal("40.00"))
        self.assertEqual(len(watch.run(self.now)), 1)
        Alert.objects.update(resolved_at=self.now)
        self.assertEqual(watch.run(self.now), [])  # handled problems aren't raised again

    def test_long_clock_in_alert(self):
        maria = hire("Maria", "server", self.location)
        night = Night.current(self.location)
        ClockEntry.objects.create(staff=maria, night=night, started_at=self.now - timedelta(hours=14))
        texts = [a.text for a in watch.run(self.now)]
        self.assertTrue(any("clocked in" in t for t in texts))


@override_settings(EMAIL_ENABLED=True)
class DigestTests(TestCase):
    def test_weekly_digest_is_sent_once_on_monday_morning(self):
        owner = make_manager("owner")
        owner.email = "owner@example.com"
        owner.save()
        business = owner.manager.business
        business.digest = "weekly"
        business.save()
        location = Location.default()
        monday = timezone.now()
        while monday.weekday() != 0:
            monday += timedelta(days=1)
        monday = monday.replace(hour=15)  # well after 7 am in any US zone
        night = Night.objects.create(location=location, date=(monday - timedelta(days=3)).date(),
                                     status=Night.Status.PAID)
        Tip.objects.create(night=night, amount=Decimal("120"), status=Tip.Status.COMPLETED)
        self.assertEqual(digest.run(monday), 1)
        self.assertIn("120.00", mail.outbox[-1].body)
        self.assertEqual(digest.run(monday), 0)

    def test_preview_page(self):
        owner = make_manager("owner")
        self.client.force_login(owner)
        self.assertContains(self.client.get(reverse("digest_preview")), "Summary email")


class ActivityAndBulkTests(TestCase):
    def setUp(self):
        self.owner = make_manager("owner")
        self.client.force_login(self.owner)
        self.location = Location.default()

    def test_activity_filters_and_csv(self):
        Change.objects.create(location=self.location, who="Ana", what="Paid the Friday shift")
        Change.objects.create(location=self.location, who="Bo", what="Changed the tip rules")
        page = self.client.get(reverse("activity") + "?who=Ana")
        self.assertContains(page, "Paid the Friday shift")
        self.assertNotContains(page, "Changed the tip rules")
        csv = self.client.get(reverse("activity") + "?q=rules&export=csv")
        self.assertIn(b"Changed the tip rules", csv.content)
        self.assertNotIn(b"Friday", csv.content)

    def test_bulk_add(self):
        text = ("Name, Email, Role\nMaria Lopez, maria@example.com, servers\nJohn, john@example.com\n"
                "Bad Row, not-an-email, server\nZed, zed@example.com, astronaut")
        self.client.post(reverse("staff_bulk"), {"people": text, "default_role": "bartender"})
        roles = dict(Membership.objects.filter(location=self.location).values_list("staff__name", "default_role"))
        self.assertEqual(roles, {"Maria Lopez": "server", "John": "bartender"})


class PolishTests(TestCase):
    def setUp(self):
        self.client.force_login(make_manager("owner"))
        self.location = Location.default()
        hire("Maria Lopez", "server", self.location, email="maria@example.com")

    def test_two_people_cannot_share_a_name(self):
        self.client.post(reverse("staff_list"), {"name": "maria lopez", "paypal_email": "other@example.com",
                                                 "default_role": "server"})
        self.assertEqual(StaffMember.objects.filter(name__iexact="maria lopez").count(), 1)
        # the same person (same email) can still join another location
        second = Location.objects.create(business=self.location.business, name="Uptown", slug="uptown")
        from tips import roles
        roles.apply_preset(second, "restaurant")
        self.client.post(reverse("location_switch"), {"location": second.pk})
        self.client.post(reverse("staff_list"), {"name": "Maria Lopez", "paypal_email": "maria@example.com",
                                                 "default_role": "server"})
        self.assertTrue(Membership.objects.filter(location=second, staff__name="Maria Lopez").exists())

    def test_two_locations_cannot_share_a_name(self):
        self.client.post(reverse("location_add"), {"name": "Uptown"})
        self.client.post(reverse("location_add"), {"name": "uptown"})
        self.assertEqual(Location.objects.filter(name__iexact="uptown").count(), 1)
