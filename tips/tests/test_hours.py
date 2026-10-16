from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from tips import ai, autoclose, hours
from tips.models import ClockEntry, CloseTime, Location, Membership, Night, Tip, TipRuleSet
from tips.tests.helpers import hire
from tips.tests.test_autoclose import FakePayPal

NY = ZoneInfo("America/New_York")


@override_settings(AI_MODE="mock")
class HoursTests(TestCase):
    def setUp(self):
        self.location = Location.default()
        self.maria = hire("Maria", "server")
        self.alex = hire("Alex", "bartender")
        parsed = ai.parse_rules("Bartenders get 20%, the rest goes to servers by hours", self.location)
        TipRuleSet.objects.create(location=self.location, text="x", rules=parsed["rules"], summary=parsed["summary"])
        self.paypal = FakePayPal()
        patcher = mock.patch("tips.services.get_client", return_value=self.paypal)
        patcher.start()
        self.addCleanup(patcher.stop)

    def set_week(self, person, week):
        Membership.objects.filter(staff=person).update(schedule=week)

    def test_schedule_summary(self):
        m = self.maria.memberships.get()
        m.schedule = {"0": 6, "1": 6, "2": 6, "3": 6, "4": 6, "5": 8}
        self.assertEqual(m.schedule_summary, "Mon–Fri 6h · Sat 8h")
        m.schedule = {}
        self.assertEqual(m.schedule_summary, "")

    def test_clock_in_and_out(self):
        entry = hours.clock_in(self.maria, self.location)
        with self.assertRaises(hours.ClockError):
            hours.clock_in(self.maria, self.location)  # twice
        entry.started_at = timezone.now() - timedelta(hours=5, minutes=30)
        entry.save()
        out = hours.clock_out(self.maria)
        self.assertEqual(out.hours(), Decimal("5.50"))
        self.assertEqual(entry.night, Night.current(self.location))

    def test_suggestions_prefer_clock_over_usual_week(self):
        night = Night.current(self.location)
        self.set_week(self.maria, {str(night.date.weekday()): 6})
        self.set_week(self.alex, {str(night.date.weekday()): 4})
        ClockEntry.objects.create(staff=self.alex, night=night, started_at=timezone.now() - timedelta(hours=7),
                                  ended_at=timezone.now())
        roster = hours.suggested_roster(night)
        self.assertEqual((roster[self.maria.pk]["hours"], roster[self.maria.pk]["source"]), (Decimal("6"), "usual"))
        self.assertEqual((roster[self.alex.pk]["hours"], roster[self.alex.pk]["source"]), (Decimal("7.00"), "clock"))

    def test_close_out_page_is_filled_in(self):
        self.client.force_login(get_user_model().objects.create_user("m", password="pw"))
        night = Night.current(self.location)
        self.set_week(self.maria, {str(night.date.weekday()): 6})
        page = self.client.get(reverse("close_night"))
        self.assertContains(page, "usual hours")
        self.assertContains(page, f'name="hours_{self.maria.pk}" value="6"')

    def test_staff_page_clock_buttons(self):
        url = reverse("my_tips", args=[self.maria.access_token])
        self.client.post(url, {"clock": "in", "location": self.location.pk})
        self.assertTrue(ClockEntry.objects.filter(staff=self.maria, ended_at__isnull=True).exists())
        self.assertContains(self.client.get(url), "On the clock")
        self.client.post(url, {"clock": "out"})
        self.assertFalse(ClockEntry.objects.filter(staff=self.maria, ended_at__isnull=True).exists())

    def test_cannot_clock_in_somewhere_you_dont_work(self):
        other = Location.objects.create(name="Other", slug="other")
        with self.assertRaises(hours.ClockError):
            hours.clock_in(self.maria, other)

    def test_save_usual_week_on_team_page(self):
        self.client.force_login(get_user_model().objects.create_user("m", password="pw"))
        m = self.maria.memberships.get()
        self.client.post(reverse("staff_schedule", args=[m.pk]), {"h_0": "6", "h_4": "7.5", "h_6": "abc"})
        m.refresh_from_db()
        self.assertEqual(m.schedule, {"0": 6.0, "4": 7.5})

    # --- automatic close-out ---
    def closing_night(self):
        self.location.auto_close_enabled = True
        self.location.after_close = Location.AfterClose.PAY
        self.location.timezone = "America/New_York"
        self.location.save()
        CloseTime.objects.create(location=self.location, weekday=3, time=time(23, 0))
        night = Night.objects.create(location=self.location, date=date(2026, 10, 15))
        Tip.objects.create(night=night, amount=Decimal("100"), paypal_order_id="O1", status="completed")
        return night

    def test_auto_close_uses_clock_ins_and_pays(self):
        night = self.closing_night()
        start = datetime(2026, 10, 15, 17, 0, tzinfo=NY)
        ClockEntry.objects.create(staff=self.maria, night=night, started_at=start, ended_at=start + timedelta(hours=6))
        ClockEntry.objects.create(staff=self.alex, night=night, started_at=start, ended_at=start + timedelta(hours=6))
        autoclose.run(now=datetime(2026, 10, 15, 23, 1, tzinfo=NY))
        night.refresh_from_db()
        self.assertEqual(night.status, Night.Status.PAYING)
        self.assertEqual({s.staff.name: s.amount for s in night.shares.all()},
                         {"Alex": Decimal("20.00"), "Maria": Decimal("80.00")})

    def test_forgot_to_clock_out_waits_for_the_manager(self):
        night = self.closing_night()
        ClockEntry.objects.create(staff=self.maria, night=night, started_at=datetime(2026, 10, 15, 17, 0, tzinfo=NY))
        autoclose.run(now=datetime(2026, 10, 15, 23, 1, tzinfo=NY))
        night.refresh_from_db()
        self.assertEqual(night.status, Night.Status.REVIEW)  # split ready, but not paid
        self.assertEqual(self.paypal.payouts, 0)
        self.assertTrue(night.clock_entries.get().auto_ended)

    def test_usual_week_only_when_allowed(self):
        night = self.closing_night()
        self.set_week(self.maria, {"3": 6})
        autoclose.run(now=datetime(2026, 10, 15, 23, 1, tzinfo=NY))
        night.refresh_from_db()
        self.assertEqual(night.status, Night.Status.CLOSED)  # waits: not allowed

        night2 = Night.objects.create(location=self.location, date=date(2026, 10, 22))
        Tip.objects.create(night=night2, amount=Decimal("50"), paypal_order_id="O2", status="completed")
        self.location.use_usual_schedule = True
        self.location.save()
        autoclose.run(now=datetime(2026, 10, 22, 23, 1, tzinfo=NY))
        night2.refresh_from_db()
        self.assertEqual(night2.status, Night.Status.PAYING)
