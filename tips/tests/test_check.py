from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings

from tips import ai, autoclose, check, services
from tips.models import ClockEntry, CloseTime, Location, Night, Shift, Tip, TipRuleSet
from tips.tests.helpers import hire
from tips.tests.test_money import FakePayPal

NY = ZoneInfo("America/New_York")


@override_settings(AI_MODE="mock")
class CloseOutCheckTests(TestCase):
    def setUp(self):
        self.paypal = FakePayPal()
        patcher = mock.patch("tips.services.get_client", return_value=self.paypal)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.location = Location.default()
        self.maria, self.john = hire("Maria", "server"), hire("John", "server")
        parsed = ai.parse_rules("Everyone shares the tips by hours worked.", self.location)
        self.rules = TipRuleSet.objects.create(location=self.location, text="x", rules=parsed["rules"],
                                               summary=parsed["summary"])

    def night(self, amount="100", hours=("5", "5"), day=date(2026, 10, 15), status=Night.Status.OPEN):
        night = Night.objects.create(location=self.location, date=day, status=status)
        Tip.objects.create(night=night, amount=Decimal(amount), paypal_order_id=f"T{night.pk}", status="completed")
        for person, h in zip((self.maria, self.john), hours):
            Shift.objects.create(night=night, staff=person, role="server", hours=Decimal(h))
        return night

    def split(self, night):
        services.build_split(night, self.rules)
        night.refresh_from_db()
        return night.review_check

    def test_clean_shift_is_ready(self):
        result = self.split(self.night())
        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"], "Ready to pay: nothing unusual found.")

    def test_long_shift_stops(self):
        result = self.split(self.night(hours=("15", "5")))
        self.assertFalse(result["ok"])
        self.assertIn("Maria has 15 hours", result["findings"][0]["text"])

    def test_forgot_clock_out_stops(self):
        night = self.night()
        ClockEntry.objects.create(staff=self.maria, night=night, started_at=datetime(2026, 10, 15, 17, tzinfo=NY),
                                  ended_at=datetime(2026, 10, 15, 23, tzinfo=NY), auto_ended=True)
        self.assertFalse(self.split(night)["ok"])

    def test_unusual_total_is_a_warning(self):
        for n in range(4):
            self.night(amount="200", day=date(2026, 10, 1 + n), status=Night.Status.PAID)
        result = self.split(self.night(amount="20"))
        self.assertTrue(result["ok"])
        self.assertTrue(any("usual shift" in f["text"] for f in result["findings"]))

    @override_settings(AI_MODE="claude")
    def test_ai_can_hold_a_payout(self):
        out = check.CheckOut(summary="Maria's share looks too high for 5 hours.", suggestions=["Check her hours"],
                             extra_concerns=[], hold=True, hold_reason="Maria's share looks off.")
        with mock.patch("tips.ai._claude_parse", side_effect=[ai.AIError("explain"), out]):
            result = self.split(self.night())
        self.assertFalse(result["ok"])
        self.assertEqual(result["by"], "Claude")
        self.assertIn("Maria's share looks off.", [f["text"] for f in check.blocking(Night.objects.get())])

    @override_settings(AI_MODE="claude")
    def test_ai_cannot_release_a_stop(self):
        out = check.CheckOut(summary="All good!", suggestions=[], extra_concerns=[], hold=False)
        with mock.patch("tips.ai._claude_parse", side_effect=[ai.AIError("explain"), out]):
            result = self.split(self.night(hours=("15", "5")))
        self.assertFalse(result["ok"])

    def test_automatic_payout_waits_for_a_stop(self):
        self.location.auto_close_enabled = True
        self.location.after_close = Location.AfterClose.PAY
        self.location.timezone = "America/New_York"
        self.location.save()
        CloseTime.objects.create(location=self.location, weekday=3, time=time(23, 0))
        night = self.night(hours=("15", "5"))
        autoclose.run(now=datetime(2026, 10, 15, 23, 1, tzinfo=NY))
        night.refresh_from_db()
        self.assertEqual(night.status, Night.Status.REVIEW)
        self.assertIn("15 hours", night.hold)
        self.assertFalse(self.paypal.payout_bodies)

        clean = self.night(day=date(2026, 10, 22))
        autoclose.run(now=datetime(2026, 10, 22, 23, 1, tzinfo=NY) + timedelta(minutes=1))
        clean.refresh_from_db()
        self.assertEqual(clean.status, Night.Status.PAYING)
