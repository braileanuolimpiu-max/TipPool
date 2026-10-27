from datetime import date, datetime, time
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from django.urls import reverse

from tips import ai, autoclose, reports, services
from tips.models import Change, CloseTime, Location, Message, Night, Shift, Tip, TipRuleSet
from tips.tests.helpers import hire, make_manager
from tips.tests.test_autoclose import FakePayPal

NY = ZoneInfo("America/New_York")


@override_settings(AI_MODE="mock")
class ManagerToolTests(TestCase):
    def setUp(self):
        self.paypal = FakePayPal()
        patcher = mock.patch("tips.services.get_client", return_value=self.paypal)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client.force_login(make_manager("boss"))
        self.location = Location.default()
        self.maria = hire("Maria", "server")
        self.alex = hire("Alex", "bartender")
        parsed = ai.parse_rules("Bartenders get 20%, the rest goes to servers by hours", self.location)
        self.rules = TipRuleSet.objects.create(location=self.location, text="x", rules=parsed["rules"],
                                               summary=parsed["summary"])

    def split_night(self, day=date(2026, 10, 15), amount="100", auto=False):
        night = Night.objects.create(location=self.location, date=day, auto_closed=auto)
        Tip.objects.create(night=night, amount=Decimal(amount), paypal_order_id=f"T{day}", status="completed")
        Shift.objects.create(night=night, staff=self.maria, role="server", hours=Decimal("6"))
        Shift.objects.create(night=night, staff=self.alex, role="bartender", hours=Decimal("6"))
        services.build_split(night, self.rules)
        return night

    # --- flags / messages ---
    def test_staff_flags_a_shift_and_manager_resolves(self):
        night = self.split_night()
        url = reverse("my_tips", args=[self.maria.access_token])
        self.client.post(url, {"flag_night": night.pk, "text": "I worked 7 hours"})
        msg = Message.objects.get()
        self.assertEqual((msg.night, msg.location), (night, self.location))
        night.refresh_from_db()
        self.assertIn("Maria", night.hold)
        self.assertContains(self.client.get(reverse("dashboard")), 'class="badge"')
        self.assertContains(self.client.get(reverse("review", args=[night.pk])), "I worked 7 hours")
        self.client.post(reverse("messages_page"), {"message": msg.pk, "reply": "Fixed, thanks", "resolve": "1"})
        msg.refresh_from_db()
        self.assertFalse(msg.is_open)
        self.assertContains(self.client.get(url), "Fixed, thanks")

    def test_staff_cannot_flag_someone_elses_shift(self):
        night = Night.objects.create(location=self.location, date=date(2026, 10, 15))
        self.client.post(reverse("my_tips", args=[self.maria.access_token]), {"flag_night": night.pk, "text": "x"})
        self.assertFalse(Message.objects.exists())

    def test_general_message(self):
        self.client.post(reverse("my_tips", args=[self.maria.access_token]),
                         {"location": self.location.pk, "text": "Can I swap Friday?"})
        self.assertContains(self.client.get(reverse("messages_page")), "Can I swap Friday?")

    # --- review window ---
    def auto_pay_location(self, minutes):
        self.location.auto_close_enabled = True
        self.location.after_close = Location.AfterClose.PAY
        self.location.review_minutes = minutes
        self.location.timezone = "America/New_York"
        self.location.save()
        CloseTime.objects.create(location=self.location, weekday=3, time=time(23, 0))

    def test_review_window_pays_later_unless_flagged(self):
        self.auto_pay_location(60)
        night = Night.objects.create(location=self.location, date=date(2026, 10, 15))
        Tip.objects.create(night=night, amount=Decimal("100"), paypal_order_id="A1", status="completed")
        Shift.objects.create(night=night, staff=self.maria, role="server", hours=Decimal("6"))
        autoclose.run(now=datetime(2026, 10, 15, 23, 1, tzinfo=NY))
        night.refresh_from_db()
        self.assertEqual(night.status, Night.Status.REVIEW)  # waiting for the review window
        Night.objects.filter(pk=night.pk).update(closed_at=datetime(2026, 10, 15, 23, 0, tzinfo=NY))
        autoclose.run(now=datetime(2026, 10, 16, 0, 1, tzinfo=NY))
        night.refresh_from_db()
        self.assertEqual(night.status, Night.Status.PAYING)

    def auto_closed_in_review(self, tip_id):
        night = Night.objects.create(location=self.location, date=date(2026, 10, 15))
        Tip.objects.create(night=night, amount=Decimal("100"), paypal_order_id=tip_id, status="completed")
        Shift.objects.create(night=night, staff=self.maria, role="server", hours=Decimal("6"))
        autoclose.run(now=datetime(2026, 10, 15, 23, 1, tzinfo=NY))
        Night.objects.filter(pk=night.pk).update(closed_at=datetime(2026, 10, 15, 23, 0, tzinfo=NY))
        night.refresh_from_db()
        self.assertEqual((night.status, night.auto_closed), (Night.Status.REVIEW, True))
        return night

    def test_a_late_tip_starts_the_review_window_again(self):
        self.auto_pay_location(60)
        night = self.auto_closed_in_review("A3")
        # a guest's tip lands at 23:55: the split is calculated again and the team gets a fresh hour to look
        Tip.objects.create(night=night, amount=Decimal("20"), paypal_order_id="A3-late", status="completed")
        services._invalidate_split(night)
        autoclose.run(now=datetime(2026, 10, 16, 0, 1, tzinfo=NY))  # an hour after closing, a minute after the new split
        night.refresh_from_db()
        self.assertEqual(night.status, Night.Status.REVIEW)
        self.assertEqual(sum(s.amount for s in night.shares.all()), Decimal("120.00"))
        self.assertEqual(self.paypal.payouts, 0)
        autoclose.run(now=datetime(2026, 10, 16, 1, 2, tzinfo=NY))  # the new window is over
        night.refresh_from_db()
        self.assertEqual(night.status, Night.Status.PAYING)

    def test_a_manager_editing_an_automatic_shift_takes_it_over(self):
        self.auto_pay_location(60)
        night = self.auto_closed_in_review("A4")
        self.client.post(reverse("close_night_pk", args=[night.pk]),
                         {"save_roster": "1", f"worked_{self.maria.pk}": "on", f"hours_{self.maria.pk}": "5",
                          f"role_{self.maria.pk}": "server"})
        night.refresh_from_db()
        self.assertFalse(night.auto_closed)
        autoclose.run(now=datetime(2026, 10, 16, 3, 0, tzinfo=NY))  # long after the window: still not paid
        night.refresh_from_db()
        self.assertNotIn(night.status, (Night.Status.PAYING, Night.Status.PAID))
        self.assertEqual(self.paypal.payouts, 0)
        # adding cash tips to an automatic shift does the same
        other = Night.objects.create(location=self.location, date=date(2026, 10, 14), auto_closed=True,
                                     status=Night.Status.CLOSED)
        services.add_manual_tip(other, "10", "cash")
        other.refresh_from_db()
        self.assertFalse(other.auto_closed)

    def test_flag_during_review_window_holds_payout(self):
        self.auto_pay_location(60)
        night = Night.objects.create(location=self.location, date=date(2026, 10, 15))
        Tip.objects.create(night=night, amount=Decimal("100"), paypal_order_id="A2", status="completed")
        Shift.objects.create(night=night, staff=self.maria, role="server", hours=Decimal("6"))
        autoclose.run(now=datetime(2026, 10, 15, 23, 1, tzinfo=NY))
        self.client.post(reverse("my_tips", args=[self.maria.access_token]),
                         {"flag_night": night.pk, "text": "Wrong hours"})
        Night.objects.filter(pk=night.pk).update(closed_at=datetime(2026, 10, 15, 23, 0, tzinfo=NY))
        autoclose.run(now=datetime(2026, 10, 16, 0, 1, tzinfo=NY))
        night.refresh_from_db()
        self.assertEqual(night.status, Night.Status.REVIEW)
        self.assertEqual(self.paypal.payouts, 0)

    # --- reports ---
    def test_report_numbers(self):
        night = self.split_night(amount="100")
        Tip.objects.create(night=night, amount=Decimal("10"), source="cash", status="completed", staff=self.maria)
        services.build_split(night, self.rules)
        report = reports.build([self.location], date(2026, 10, 1), date(2026, 10, 31))
        people = {p["name"]: p for p in report["people"]}
        self.assertEqual(report["collected"], Decimal("110.00"))
        self.assertEqual((people["Maria"]["pool"], people["Maria"]["personal"], people["Maria"]["total"]),
                         (Decimal("80.00"), Decimal("10.00"), Decimal("90.00")))
        self.assertEqual(people["Maria"]["per_hour"], Decimal("13.33"))
        self.assertEqual(dict(report["by_source"])["Cash"], Decimal("10.00"))

    def test_report_page_and_csv(self):
        self.split_night(day=date(2026, 10, 15))
        page = self.client.get(reverse("reports") + "?period=custom&from=2026-10-01&to=2026-10-31")
        self.assertContains(page, "Maria")
        csv = self.client.get(reverse("reports") + "?period=custom&from=2026-10-01&to=2026-10-31&export=people")
        self.assertEqual(csv["Content-Type"], "text/csv")
        self.assertIn("Maria,Server,1,6.00,80.00", csv.content.decode())

    def test_period_ranges(self):
        thursday = date(2026, 10, 15)
        self.assertEqual(reports.period_range("this_week", thursday), (date(2026, 10, 12), thursday))
        self.assertEqual(reports.period_range("last_week", thursday), (date(2026, 10, 5), date(2026, 10, 11)))
        self.assertEqual(reports.period_range("last_month", thursday), (date(2026, 9, 1), date(2026, 9, 30)))

    # --- history ---
    def test_changes_are_logged_and_shown(self):
        member = self.maria.memberships.get()
        self.client.post(reverse("staff_schedule", args=[member.pk]), {"h_0": "6"})
        self.client.post(reverse("role_add"), {"name": "Sommelier"})
        whats = list(Change.objects.values_list("what", flat=True))
        self.assertTrue(any("Maria's usual week" in w for w in whats))
        self.assertTrue(any("Sommelier" in w for w in whats))
        self.assertEqual(Change.objects.first().who, "boss")
        self.assertContains(self.client.get(reverse("settings")), "Sommelier")

    def test_automatic_close_out_is_logged(self):
        self.auto_pay_location(0)
        night = Night.objects.create(location=self.location, date=date(2026, 10, 15))
        Tip.objects.create(night=night, amount=Decimal("100"), paypal_order_id="A3", status="completed")
        autoclose.run(now=datetime(2026, 10, 15, 23, 1, tzinfo=NY))
        self.assertTrue(Change.objects.filter(who="Automatic close-out").exists())
