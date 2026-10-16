from datetime import date, datetime, time
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings

from tips import ai, autoclose
from tips.models import CloseTime, Location, Night, Shift, Tip, TipRuleSet
from tips.tests.helpers import hire

NY = ZoneInfo("America/New_York")


class FakePayPal:
    def __init__(self):
        self.payouts = 0

    def create_payout_batch(self, sender_batch_id, items, subject="", currency="USD"):
        self.payouts += 1
        return {"batch_header": {"payout_batch_id": "B1", "batch_status": "PENDING"}}


@override_settings(AI_MODE="mock")
class AutoCloseTests(TestCase):
    def setUp(self):
        self.restaurant = Location.default()
        self.restaurant.timezone = "America/New_York"
        self.restaurant.auto_close_enabled = True
        self.restaurant.save()
        # Thursday 23:00, Friday closes after midnight at 01:00
        CloseTime.objects.create(location=self.restaurant, weekday=3, time=time(23, 0))
        CloseTime.objects.create(location=self.restaurant, weekday=4, time=time(1, 0))
        parsed = ai.parse_rules("Bartenders get 20%, the rest goes to servers by hours", self.restaurant)
        TipRuleSet.objects.create(location=self.restaurant, text="x", rules=parsed["rules"], summary=parsed["summary"])
        self.maria = hire("Maria", "server", self.restaurant)
        self.paypal = FakePayPal()
        patcher = mock.patch("tips.services.get_client", return_value=self.paypal)
        patcher.start()
        self.addCleanup(patcher.stop)

    def night(self, day, tips=Decimal("50"), roster=True):
        n = Night.objects.create(location=self.restaurant, date=day)
        Tip.objects.create(night=n, amount=tips, paypal_order_id=f"O-{day}", status="completed")
        if roster:
            Shift.objects.create(night=n, staff=self.maria, role="server", hours=Decimal("6"))
        return n

    def test_due_time_same_day_and_after_midnight(self):
        thursday = self.night(date(2026, 10, 15))
        friday = self.night(date(2026, 10, 16))
        self.assertEqual(autoclose.close_due_at(thursday, NY), datetime(2026, 10, 15, 23, 0, tzinfo=NY))
        self.assertEqual(autoclose.close_due_at(friday, NY), datetime(2026, 10, 17, 1, 0, tzinfo=NY))

    def test_not_closed_before_time(self):
        n = self.night(date(2026, 10, 15))
        autoclose.run(now=datetime(2026, 10, 15, 22, 59, tzinfo=NY))
        n.refresh_from_db()
        self.assertEqual(n.status, Night.Status.OPEN)

    def test_closes_and_prepares_split(self):
        n = self.night(date(2026, 10, 15))
        autoclose.run(now=datetime(2026, 10, 15, 23, 1, tzinfo=NY))
        n.refresh_from_db()
        self.assertEqual(n.status, Night.Status.REVIEW)
        self.assertTrue(n.auto_closed)
        self.assertEqual(n.shares.get().amount, Decimal("50.00"))
        self.assertEqual(self.paypal.payouts, 0)

    def test_auto_pay(self):
        self.restaurant.after_close = Location.AfterClose.PAY
        self.restaurant.save()
        n = self.night(date(2026, 10, 15))
        autoclose.run(now=datetime(2026, 10, 15, 23, 5, tzinfo=NY))
        n.refresh_from_db()
        self.assertEqual(n.status, Night.Status.PAYING)
        self.assertEqual(self.paypal.payouts, 1)

    def test_without_roster_it_waits(self):
        self.restaurant.after_close = Location.AfterClose.PAY
        self.restaurant.save()
        n = self.night(date(2026, 10, 15), roster=False)
        autoclose.run(now=datetime(2026, 10, 15, 23, 5, tzinfo=NY))
        n.refresh_from_db()
        self.assertEqual(n.status, Night.Status.CLOSED)
        self.assertEqual(self.paypal.payouts, 0)

    def test_day_without_close_time_stays_open(self):
        n = self.night(date(2026, 10, 18))  # Sunday: no close time
        autoclose.run(now=datetime(2026, 10, 19, 9, 0, tzinfo=NY))
        n.refresh_from_db()
        self.assertEqual(n.status, Night.Status.OPEN)

    def test_disabled_does_nothing(self):
        self.restaurant.auto_close_enabled = False
        self.restaurant.save()
        n = self.night(date(2026, 10, 15))
        autoclose.run(now=datetime(2026, 10, 16, 9, 0, tzinfo=NY))
        n.refresh_from_db()
        self.assertEqual(n.status, Night.Status.OPEN)

    def test_new_tips_after_close_go_to_a_new_night(self):
        n = self.night(date(2026, 10, 15))
        autoclose.run(now=datetime(2026, 10, 15, 23, 1, tzinfo=NY))
        self.assertNotEqual(Night.current(self.restaurant).pk, n.pk)


class SettingsPageTests(TestCase):
    def test_save_schedule(self):
        from django.contrib.auth import get_user_model
        from django.urls import reverse
        user = get_user_model().objects.create_user("m", password="pw")
        self.client.force_login(user)
        data = {"auto_close_enabled": "on", "after_close": "pay", "timezone": "Europe/Bucharest",
                "on_4": "on", "time_4": "01:30", "on_0": "", "time_0": "22:00"}
        res = self.client.post(reverse("settings"), data)
        self.assertRedirects(res, reverse("settings"))
        restaurant = Location.default()
        self.assertEqual((restaurant.auto_close_enabled, restaurant.after_close, restaurant.timezone),
                         (True, "pay", "Europe/Bucharest"))
        friday = CloseTime.objects.get(weekday=4)
        self.assertEqual((friday.time, friday.enabled, friday.after_midnight), (time(1, 30), True, True))
        self.assertFalse(CloseTime.objects.get(weekday=0).enabled)
        self.assertContains(self.client.get(reverse("settings")), "Next automatic close-out")
