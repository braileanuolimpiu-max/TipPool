"""Regression tests for the October audit: money races, refunds, totals, bad input."""
import json
from decimal import Decimal
from unittest import mock

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from tips import ai, autoclose, services
from tips.models import (Alert, Business, ClockEntry, Location, Membership, Night, PayoutItem, Shift, Tip,
                         TipRuleSet)
from tips.tests.helpers import hire, make_manager
from tips.tests.test_flow import FakePayPal


@override_settings(AI_MODE="mock")
class AuditBase(TestCase):
    def setUp(self):
        self.paypal = FakePayPal()
        for target in ("tips.services.get_client", "tips.views.get_client"):
            patcher = mock.patch(target, return_value=self.paypal)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.user = make_manager("manager")
        self.client.force_login(self.user)
        self.client.raise_request_exception = False
        self.location = Location.default()
        self.staff = {n: hire(n, r, self.location) for n, r in
                      [("Maria", "server"), ("John", "server"), ("Alex", "bartender")]}
        parsed = ai.parse_rules("Bartenders get 20%, the rest goes to servers by hours", self.location)
        self.rules = TipRuleSet.objects.create(location=self.location, text="x", rules=parsed["rules"],
                                               summary=parsed["summary"])

    def make_night(self, amounts=(10, 20), hours=5):
        night = Night.objects.create(location=self.location, status=Night.Status.CLOSED)
        for a in amounts:
            Tip.objects.create(night=night, amount=Decimal(a), status=Tip.Status.COMPLETED)
        for s in self.staff.values():
            Shift.objects.create(night=night, staff=s, role=Membership.objects.get(staff=s).default_role,
                                 hours=hours)
        services.build_split(night, self.rules)
        return night


class MoneyRaceTests(AuditBase):
    def test_a_stale_resplit_never_wipes_a_payout_being_sent(self):
        night = self.make_night()
        stale = Night.objects.get(pk=night.pk)  # someone opened "recalculate" before the payout went out
        services.pay_night(night)
        with self.assertRaises(services.TipPoolError):
            services.build_split(stale, self.rules)
        night.refresh_from_db()
        self.assertEqual(night.status, Night.Status.PAYING)
        self.assertEqual(PayoutItem.objects.filter(batch__night=night).count(), 3)

    def test_a_stale_tip_add_never_reopens_a_paid_shift(self):
        night = self.make_night()
        stale = Night.objects.get(pk=night.pk)
        services.pay_night(night)
        with self.assertRaises(services.TipPoolError):
            services.add_manual_tip(stale, "40", Tip.Source.CASH)
        night.refresh_from_db()
        self.assertEqual(night.status, Night.Status.PAYING)
        self.assertEqual(night.shares.count(), 3)


class RefundTests(AuditBase):
    def test_partial_refund_only_takes_the_refunded_part(self):
        night = Night.objects.create(location=self.location, status=Night.Status.CLOSED)
        tip = Tip.objects.create(night=night, amount=Decimal("50"), status=Tip.Status.COMPLETED,
                                 paypal_order_id="O1", paypal_capture_id="CAP1")
        self.paypal.get_capture = lambda cid: {"status": "PARTIALLY_REFUNDED"}
        self.paypal.get_order = lambda oid: {"purchase_units": [{"payments": {"refunds": [
            {"status": "COMPLETED", "amount": {"value": "1.00"}}]}}]}
        services.sync_capture("CAP1")
        tip.refresh_from_db()
        self.assertEqual((tip.status, tip.amount, tip.refunded), (Tip.Status.COMPLETED, Decimal("49.00"), Decimal("1.00")))
        services.sync_capture("CAP1")  # the same refund seen twice is counted once
        tip.refresh_from_db()
        self.assertEqual(tip.amount, Decimal("49.00"))


class TotalsAndInputTests(AuditBase):
    def test_history_totals_are_not_multiplied_by_people(self):
        self.make_night(amounts=(10, 20))
        rows = self.client.get(reverse("history")).context["rows"]
        self.assertEqual(rows[0]["total"], Decimal("30.00"))

    def test_hours_print_as_plain_numbers(self):
        night = self.make_night(hours=10)
        text = json.dumps([s.breakdown for s in night.shares.all()]) + " ".join(s.explanation for s in night.shares.all())
        self.assertNotIn("E+", text)

    def test_close_out_refuses_impossible_hours_without_touching_the_roster(self):
        night = self.make_night()
        data = {}
        for s in self.staff.values():
            role = Membership.objects.get(staff=s).default_role
            data.update({f"worked_{s.pk}": "1", f"hours_{s.pk}": "5", f"role_{s.pk}": role})
        alex = self.staff["Alex"]
        for bad in ("1000", "NaN", "-3"):
            data[f"hours_{alex.pk}"] = bad
            res = self.client.post(reverse("close_night_pk", args=[night.pk]), data)
            self.assertEqual(res.status_code, 302, bad)
            self.assertEqual(night.shifts.count(), 3)

    def test_no_tip_shift_can_be_finished(self):
        night = Night.objects.create(location=self.location, status=Night.Status.CLOSED)
        maria = self.staff["Maria"]
        self.client.post(reverse("close_night_pk", args=[night.pk]),
                         {f"worked_{maria.pk}": "1", f"hours_{maria.pk}": "4", f"role_{maria.pk}": "server"})
        night.refresh_from_db()
        self.assertEqual(night.status, Night.Status.PAID)
        self.assertEqual(night.shifts.count(), 1)

    def test_bad_ids_and_numbers_are_never_500s(self):
        member = Membership.objects.first()
        night = self.make_night()
        for url, data in [
            (reverse("staff_schedule", args=[member.pk]), {"h_0": "NaN"}),
            (reverse("settings"), {"tip_style": "amounts", "tip_value_0": "NaN", "tip_value_1": "Infinity"}),
            (reverse("messages_page"), {"alert": "abc"}),
            (reverse("messages_page"), {"message": "abc"}),
            (reverse("location_switch"), {"location": "abc"}),
            (reverse("night_tip_add", args=[night.pk]), {"for": "abc", "amount": "5", "source": "cash"}),
            (reverse("my_tips", args=[member.staff.access_token]), {"clock": "in", "location": "abc"}),
            (reverse("my_tips", args=[member.staff.access_token]), {"text": "hi", "location": "abc"}),
        ]:
            self.assertLess(self.client.post(url, data).status_code, 500, (url, data))
        guest = Client(raise_request_exception=False)
        for body in [{"id": "E1", "resource": None}, {"id": "E2", "resource": {"supplementary_data": None}},
                     {"id": "E3", "resource": []}, {"id": "E4", "resource": {"links": "x"}}]:
            res = guest.post(reverse("paypal_webhook"), data=json.dumps(body), content_type="application/json")
            self.assertLess(res.status_code, 500, body)

    def test_staff_need_a_real_email_and_a_sane_name(self):
        self.client.post(reverse("staff_list"), {"name": "Bad", "paypal_email": "not-an-email", "default_role": "server"})
        self.assertFalse(Membership.objects.filter(staff__name="Bad").exists())
        self.client.post(reverse("staff_list"), {"name": "N" * 200, "paypal_email": "n@example.com",
                                                 "default_role": "server"})
        self.assertEqual(len(Membership.objects.get(staff__paypal_email="n@example.com").staff.name), 80)

    def test_staff_cannot_clock_in_at_someone_elses_location(self):
        other = Location.objects.create(business=Business.objects.create(name="Other"), name="Other", slug="other")
        maria = self.staff["Maria"]
        self.client.post(reverse("my_tips", args=[maria.access_token]), {"clock": "in", "location": other.pk})
        self.assertFalse(ClockEntry.objects.filter(staff=maria).exists())


class OwnerControlTests(AuditBase):
    def test_freeze_stops_every_payout(self):
        night = self.make_night()
        business = self.location.business
        business.payouts_frozen_at, business.payouts_frozen_by = "2026-10-20T10:00Z", "Owner"
        business.save()
        with self.assertRaises(services.PayoutsFrozen):
            services.pay_night(night)
        night.refresh_from_db()
        self.assertEqual(night.status, Night.Status.REVIEW)

    def test_over_the_limit_waits_for_the_owner(self):
        night = self.make_night(amounts=(100, 200))
        business = self.location.business
        business.approval_limit = Decimal("250")
        business.save()
        with self.assertRaises(services.NeedsOwner):
            services.pay_night(night)
        night.refresh_from_db()
        self.assertIn("owner's OK", night.hold)
        self.assertTrue(Alert.objects.filter(key=f"owner-ok:{night.pk}").exists())
        services.pay_night(night, by_owner=True)
        night.refresh_from_db()
        self.assertEqual((night.status, night.hold), (Night.Status.PAYING, ""))


class AutoCloseTests(AuditBase):
    def test_one_broken_location_does_not_stop_the_others(self):
        with mock.patch("tips.autoclose._run_location", side_effect=[RuntimeError("boom"), ["ok"]]):
            Location.objects.create(business=self.location.business, name="Second", slug="second",
                                    auto_close_enabled=True)
            Location.objects.filter(pk=self.location.pk).update(auto_close_enabled=True)
            self.assertEqual(autoclose.run(), ["ok"])


class ShownCorrectlyTests(AuditBase):
    def test_review_check_follows_new_flags(self):
        from tips.models import Message
        night = self.make_night()
        night.review_check = {"findings": [], "summary": "Ready to pay: nothing unusual found.", "by": "rules"}
        night.save()
        Message.objects.create(staff=self.staff["John"], location=self.location, night=night, text="I stayed late")
        page = self.client.get(reverse("review", args=[night.pk]))
        self.assertNotContains(page, "nothing unusual found")
        self.assertContains(page, "flagged a problem")

    def test_answering_the_last_flag_lifts_the_hold(self):
        from tips.models import Message
        night = self.make_night()
        msg = Message.objects.create(staff=self.staff["John"], location=self.location, night=night, text="Hours?")
        Night.objects.filter(pk=night.pk).update(hold="John flagged a problem.")
        lead = make_manager("lead", self.location.business, owner=False)
        lead.manager.level = lead.manager.Level.LEAD
        lead.manager.save()
        self.client.force_login(lead)  # a shift lead can't lift a hold that stops money going out
        self.client.post(reverse("messages_page"), {"message": msg.pk, "reply": "Fixed"})
        night.refresh_from_db()
        self.assertEqual(night.hold, "John flagged a problem.")
        self.client.force_login(self.user)
        self.client.post(reverse("messages_page"), {"message": msg.pk, "reply": "Fixed"})
        night.refresh_from_db()
        self.assertEqual(night.hold, "")

    def test_shift_leads_get_their_own_sign_in_page(self):
        lead = make_manager("lead", self.location.business, owner=False)
        lead.manager.level = "lead"
        lead.manager.save()
        self.client.force_login(lead)
        self.assertContains(self.client.get(reverse("two_step")), "Turn on two-step sign-in")

    def test_approving_a_change_never_shares_an_email(self):
        from tips.models import Message
        john, maria = self.staff["John"], self.staff["Maria"]
        msg = Message.objects.create(staff=john, location=self.location, kind="payout",
                                     payload={"method": "paypal", "email": maria.paypal_email}, text="pay me there")
        self.client.post(reverse("messages_page"), {"message": msg.pk, "decision": "approve"})
        john.refresh_from_db()
        self.assertNotEqual(john.paypal_email, maria.paypal_email)

    def test_salons_start_with_personal_tips(self):
        self.client.post(reverse("location_add"), {"name": "Studio Nine", "business_type": "salon"})
        self.assertEqual(Location.objects.get(name="Studio Nine").tip_mode, "personal")

    def test_ask_knows_first_names(self):
        from datetime import timedelta
        from django.utils import timezone
        night = self.make_night()
        Night.objects.filter(pk=night.pk).update(status=Night.Status.PAID, date=timezone.localdate() - timedelta(days=1))
        page = self.client.get(reverse("reports") + "?period=this_year&q=How+much+did+maria+make")
        self.assertContains(page, "Maria made")

    def test_money_has_thousands_separators(self):
        self.make_night(amounts=(1500, 250))
        self.assertContains(self.client.get(reverse("history")), "1,750.00")
