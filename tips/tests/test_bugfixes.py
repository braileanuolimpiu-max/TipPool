"""One test per bug from the bug hunt, so none of them comes back."""
from datetime import date, datetime, time
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from django.urls import reverse

from tips import ai, autoclose, roles, services
from tips.models import CloseTime, Location, Night, PayoutItem, Role, Shift, Tip, TipRuleSet
from tips.split import PersonShare
from tips.tests.helpers import hire, make_manager
from tips.tests.test_money import FakePayPal

NY = ZoneInfo("America/New_York")


@override_settings(AI_MODE="mock")
class BugFixTests(TestCase):
    def setUp(self):
        self.paypal = FakePayPal()
        for target in ("tips.services.get_client", "tips.views.get_client"):
            patcher = mock.patch(target, return_value=self.paypal)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.location = Location.default()
        self.location.auto_close_enabled, self.location.timezone = True, "America/New_York"
        self.location.save()
        CloseTime.objects.create(location=self.location, weekday=4, time=time(23, 0))   # Friday
        CloseTime.objects.create(location=self.location, weekday=1, time=time(22, 0))   # Tuesday (Monday closed)
        self.client.force_login(make_manager("boss"))
        self.maria, self.john = hire("Maria", "server"), hire("John", "server")
        parsed = ai.parse_rules("Everyone shares the tips by hours worked.", self.location)
        self.rules = TipRuleSet.objects.create(location=self.location, text="x", rules=parsed["rules"],
                                               summary=parsed["summary"])

    # 1. a shift opened after closing time belongs to the next service day, and empty ones don't pile up
    def test_shift_opened_after_close_is_next_days(self):
        with mock.patch("django.utils.timezone.now", return_value=datetime(2026, 10, 16, 23, 30, tzinfo=NY)):
            night = Night.current(self.location)
        self.assertEqual(night.date, date(2026, 10, 20))  # next close is Tuesday's
        with mock.patch("django.utils.timezone.now", return_value=datetime(2026, 10, 16, 23, 31, tzinfo=NY)):
            autoclose.run(now=datetime(2026, 10, 16, 23, 31, tzinfo=NY))
        night.refresh_from_db()
        self.assertEqual(night.status, Night.Status.OPEN)

    def test_empty_shift_is_removed_at_closing(self):
        night = Night.objects.create(location=self.location, date=date(2026, 10, 16))
        autoclose.run(now=datetime(2026, 10, 16, 23, 1, tzinfo=NY))
        self.assertFalse(Night.objects.filter(pk=night.pk).exists())
        self.assertFalse(Night.objects.filter(status=Night.Status.CLOSED).exists())

    # 2. a shift on a day with no close time closes at the next one
    def test_closed_monday_shift_closes_tuesday(self):
        night = Night.objects.create(location=self.location, date=date(2026, 10, 19))  # Monday
        self.assertEqual(autoclose.close_due_at(night), datetime(2026, 10, 20, 22, 0, tzinfo=NY))

    # 3. clock-in hours like 7.43 are accepted by the form
    def test_hours_inputs_accept_any_decimal(self):
        page = self.client.get(reverse("close_night"))
        self.assertContains(page, 'step="any"')
        self.assertNotContains(page, 'step="0.25"')

    # 4. a tip that completes after the split
    def late_tip(self, status):
        night = Night.objects.create(location=self.location, date=date(2026, 10, 15), status=status)
        tip = Tip.objects.create(night=night, amount=Decimal("10"), paypal_order_id="LATE")
        return night, tip

    def test_late_tip_in_review_means_recalculate(self):
        night, tip = self.late_tip(Night.Status.REVIEW)
        services.capture_tip("LATE")
        night.refresh_from_db()
        self.assertEqual(night.status, Night.Status.CLOSED)

    def test_late_tip_after_payout_moves_to_the_current_shift(self):
        night, tip = self.late_tip(Night.Status.PAID)
        services.capture_tip("LATE")
        tip.refresh_from_db()
        self.assertNotEqual(tip.night, night)
        self.assertEqual(tip.night.status, Night.Status.OPEN)

    # 5. someone removed from the team stays on a shift they worked
    def test_removed_person_stays_on_their_shift(self):
        night = Night.current(self.location)
        Shift.objects.create(night=night, staff=self.john, role="server", hours=Decimal("5"))
        self.john.memberships.update(active=False)
        self.assertContains(self.client.get(reverse("close_night")), f'name="hours_{self.john.pk}"')

    # 6. a rules preview only saves at its own location
    def test_rules_preview_is_per_location(self):
        other = Location.objects.create(business=self.location.business, name="Other", slug="other")
        roles.apply_preset(other, "salon")
        self.client.post(reverse("rules"), {"text": "Everyone shares the tips by hours worked.", "parse": "1"})
        self.client.post(reverse("location_switch"), {"location": other.pk})
        self.client.post(reverse("rules"), {"save": "1"})
        self.assertFalse(TipRuleSet.objects.filter(location=other).exists())

    # 8. a failed payout keeps the shift open for attention
    def test_failed_payout_is_not_paid(self):
        night = Night.objects.create(location=self.location, date=date(2026, 10, 15))
        Tip.objects.create(night=night, amount=Decimal("50"), paypal_order_id="P1", status="completed")
        Shift.objects.create(night=night, staff=self.maria, role="server", hours=Decimal("5"))
        services.build_split(night, self.rules)
        services.pay_night(night)
        item = PayoutItem.objects.get()
        services.apply_payout_item_event({"payout_item": {"sender_item_id": item.sender_item_id},
                                          "transaction_status": "FAILED"})
        night.refresh_from_db()
        self.assertEqual(night.status, Night.Status.PAYING)

    # 9. the thank-you page without an order
    def test_thanks_page_without_order(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("tip_thanks")).status_code, 404)

    # 10. personal tips only, nobody to tip
    def test_nobody_to_tip(self):
        self.location.tip_mode = Location.TipMode.PERSONAL
        self.location.save()
        self.maria.memberships.update(active=False)
        self.john.memberships.update(active=False)
        self.assertContains(self.client.get(reverse("tip_page") + "?at=" + self.location.slug), "Nobody is taking tips")

    # 11. a role someone removed from the team still has can't be deleted
    def test_role_of_removed_person_is_kept(self):
        self.john.memberships.update(active=False, default_role="busser")
        busser = Role.objects.get(location=self.location, key="busser")
        self.client.post(reverse("role_remove", args=[busser.pk]))
        self.assertTrue(Role.objects.filter(pk=busser.pk).exists())

    # 12. settings say what they ignored
    def test_settings_warn_about_ignored_values(self):
        res = self.client.post(reverse("settings"), {"tip_style": "percent", "tip_value_0": "300", "tip_value_1": "4",
                                                     "tip_value_2": "8", "tip_value_3": "12", "on_2": "on", "time_2": "",
                                                     "after_close": "review", "timezone": "UTC", "tip_mode": "pool"},
                               follow=True)
        self.assertContains(res, "Saved, except")
        self.assertContains(res, "Wednesday")

    # 15. two people with the same name get their own explanation
    def test_same_names_get_their_own_explanation(self):
        a = PersonShare("1", "Ana", "server", Decimal("5"), Decimal("10"), [{"group": "Everyone", "pool": "20",
                                                                             "basis": "5h of 10h", "amount": "10"}])
        b = PersonShare("2", "Ana", "server", Decimal("5"), Decimal("10"), [{"group": "Everyone", "pool": "20",
                                                                             "basis": "5h of 10h", "amount": "10"}])
        out = ai.explain_shares(Decimal("20"), "x", [a, b], [])
        self.assertEqual(set(out), {"1", "2"})
