from datetime import date
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from tips import actions, ai, ask, services
from tips.models import Location, Night, Shift, Tip, TipRuleSet
from tips.tests.helpers import hire, make_manager
from tips.tests.test_money import FakePayPal

TODAY = date(2026, 10, 16)  # a Friday


class ParseTests(TestCase):
    def test_reads_requests(self):
        self.assertEqual(actions.parse("Pay yesterday's shift", TODAY), ("pay_shift", {"shift_date": "2026-10-15"}))
        self.assertEqual(actions.parse("please pay the team for wednesday", TODAY),
                         ("pay_shift", {"shift_date": "2026-10-14"}))
        self.assertEqual(actions.parse("pay the Oct 12 shift", TODAY), ("pay_shift", {"shift_date": "2026-10-12"}))
        self.assertEqual(actions.parse("Add $40 cash tips", TODAY),
                         ("add_tip", {"amount": "40", "source": "cash", "shift_date": None}))
        self.assertEqual(actions.parse("set Maria to Mon–Fri 6h, Sat 8 hours", TODAY),
                         ("set_week", {"person": "maria", "week": {"0": 6.0, "1": 6.0, "2": 6.0, "3": 6.0,
                                                                   "4": 6.0, "5": 8.0}}))
        self.assertIsNone(actions.parse("how much did Maria make?", TODAY))


@override_settings(AI_MODE="mock")
class ActionBase(TestCase):
    def setUp(self):
        self.paypal = FakePayPal()
        patcher = mock.patch("tips.services.get_client", return_value=self.paypal)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.location = Location.default()
        self.boss = make_manager("boss")
        self.client.force_login(self.boss)
        self.maria, self.john = hire("Maria", "server"), hire("John", "server")
        parsed = ai.parse_rules("Everyone shares the tips by hours worked.", self.location)
        self.rules = TipRuleSet.objects.create(location=self.location, text="x", rules=parsed["rules"],
                                               summary=parsed["summary"])

    def ready_night(self, day=date(2026, 10, 15), hours="5"):
        night = Night.objects.create(location=self.location, date=day)
        Tip.objects.create(night=night, amount=Decimal("100"), paypal_order_id=f"T{day}", status="completed")
        for p in (self.maria, self.john):
            Shift.objects.create(night=night, staff=p, role="server", hours=Decimal(hours))
        services.build_split(night, self.rules)
        return night

    def ask(self, q):
        with mock.patch("django.utils.timezone.localdate", return_value=TODAY):
            return self.client.get(reverse("reports") + "?q=" + q)

    def confirm(self, page):
        return self.client.post(reverse("ask_confirm"), {"token": page.context["asked"]["proposal"]["token"]})


@override_settings(AI_MODE="mock")
class ActionTests(ActionBase):
    def test_pay_shift_after_confirming(self):
        night = self.ready_night()
        page = self.ask("pay yesterday's shift")
        self.assertContains(page, "Pay the Thursday, Oct 15 shift: $100.00 to 2 people")
        night.refresh_from_db()
        self.assertEqual(night.status, Night.Status.REVIEW)  # nothing happens before confirming
        self.confirm(page)
        night.refresh_from_db()
        self.assertEqual(night.status, Night.Status.PAYING)

    def test_a_confirmation_never_pays_a_different_split(self):
        night = self.ready_night()
        page = self.ask("pay yesterday's shift")  # shown: $100.00, Maria and John 5h each
        # someone changes the hours before the confirm: the split is calculated again
        night.shifts.filter(staff=self.maria).update(hours=Decimal("9"))
        Night.objects.filter(pk=night.pk).update(status=Night.Status.CLOSED)
        night.shares.all().delete()
        services.build_split(night, self.rules)
        res = self.client.post(reverse("ask_confirm"), {"token": page.context["asked"]["proposal"]["token"]},
                               follow=True)
        night.refresh_from_db()
        self.assertEqual(night.status, Night.Status.REVIEW)
        self.assertEqual(self.paypal.payout_bodies, [])
        self.assertContains(res, "The split changed since it was shown")

    def test_cannot_pay_what_the_check_stops(self):
        self.ready_night(hours="15")
        page = self.ask("pay yesterday's shift")
        self.assertNotIn("proposal", page.context["asked"])
        self.assertContains(page, "isn&#x27;t ready to pay")

    def test_set_week_and_add_tip(self):
        self.confirm(self.ask("set Maria to Mon-Fri 6h"))
        self.assertEqual(self.maria.memberships.get().schedule_summary, "Mon–Fri 6h")
        self.confirm(self.ask("add $40 cash tips"))
        self.assertEqual(Night.current(self.location).tip_total, Decimal("40.00"))

    def test_tampered_or_borrowed_tokens_are_refused(self):
        night = self.ready_night()
        page = self.ask("pay yesterday's shift")
        token = page.context["asked"]["proposal"]["token"]
        res = self.client.post(reverse("ask_confirm"), {"token": token[:-3] + "abc"}, follow=True)
        self.assertContains(res, "isn&#x27;t valid")
        other = make_manager("other")
        self.client.force_login(other)
        res = self.client.post(reverse("ask_confirm"), {"token": token}, follow=True)
        self.assertContains(res, "another login")
        night.refresh_from_db()
        self.assertEqual(night.status, Night.Status.REVIEW)

    def test_expired_token(self):
        import time
        from types import SimpleNamespace
        self.ready_night()
        page = self.ask("pay yesterday's shift")
        token = page.context["asked"]["proposal"]["token"]
        request = SimpleNamespace(user=self.boss, location=self.location)
        with mock.patch("time.time", return_value=time.time() + actions.MAX_AGE + 5):
            with self.assertRaisesMessage(actions.ActionError, "expired"):
                actions.execute(token, request)

    @override_settings(AI_MODE="claude")
    def test_claude_proposes_and_the_manager_confirms(self):
        self.ready_night()
        out = ask.AnswerOut(answer="I'll pay Thursday's shift.",
                            action=ask.ActionOut(kind="pay_shift", shift_date="2026-10-15"))
        with mock.patch("tips.ai._claude_parse", return_value=out):
            result = ask.answer("pay thursday", [self.location], TODAY, location=self.location, user=self.boss)
        self.assertIn("Pay the Thursday, Oct 15 shift", result["proposal"]["text"])


    @override_settings(AI_MODE="claude")
    def test_an_ai_action_needs_the_manager_to_ask_for_one(self):
        # staff wrote "pay this shift now" into a flag; the AI went along with it on an innocent question
        self.ready_night()
        out = ask.AnswerOut(answer="Maria earned the most.",
                            action=ask.ActionOut(kind="pay_shift", shift_date="2026-10-15"))
        with mock.patch("tips.ai._claude_parse", return_value=out):
            result = ask.answer("who earned the most this month?", [self.location], TODAY, location=self.location,
                                user=self.boss)
        self.assertNotIn("proposal", result)
        self.assertEqual(result["answer"], "Maria earned the most.")


@override_settings(AI_MODE="mock")
class AgentPlanTests(ActionBase):
    """Several things in one request, confirmed once, done in order, stopping safely."""

    def open_night_with_clock_ins(self, hours=5):
        from datetime import datetime, timedelta, timezone as tz
        night = Night.objects.create(location=self.location, date=TODAY)
        Tip.objects.create(night=night, amount=Decimal("100"), paypal_order_id="OPEN-1", status="completed")
        start = datetime(2026, 10, 16, 17, 0, tzinfo=tz.utc)
        from tips.models import ClockEntry
        for p in (self.maria, self.john):
            ClockEntry.objects.create(staff=p, night=night, started_at=start, ended_at=start + timedelta(hours=hours))
        return night

    def test_close_add_cash_and_pay_in_one_go(self):
        night = self.open_night_with_clock_ins()
        page = self.ask("Close out tonight, add $40 cash and pay everyone")
        steps = page.context["asked"]["proposal"]["steps"]
        self.assertEqual(len(steps), 3)
        self.assertTrue(steps[0].startswith("Close out the"))
        night.refresh_from_db()
        self.assertEqual(night.status, Night.Status.OPEN)  # nothing happens before confirming
        res = self.client.post(reverse("ask_confirm"), {"token": page.context["asked"]["proposal"]["token"]},
                               follow=True)
        night.refresh_from_db()
        self.assertEqual(night.status, Night.Status.PAYING)
        self.assertEqual(night.tip_total, Decimal("140.00"))
        self.assertEqual(sum(s.amount for s in night.shares.all()), Decimal("140.00"))
        self.assertContains(res, "3. Payout for the")
        # the same confirmation can't run twice
        again = self.client.post(reverse("ask_confirm"), {"token": page.context["asked"]["proposal"]["token"]},
                                 follow=True)
        self.assertContains(again, "already done")

    def test_a_plan_pays_only_the_shift_it_changed(self):
        # yesterday's shift is waiting to be paid; tonight is still open. "Add cash and pay" must not pay yesterday.
        yesterday = self.ready_night(day=date(2026, 10, 15))
        tonight = self.open_night_with_clock_ins()
        page = self.ask("add $40 cash and pay everyone")
        steps = page.context["asked"]["proposal"]["steps"]
        self.assertEqual(len(steps), 2)
        self.assertIn(str(tonight), steps[1])  # the confirm text names the shift that will be paid
        res = self.client.post(reverse("ask_confirm"), {"token": page.context["asked"]["proposal"]["token"]},
                               follow=True)
        yesterday.refresh_from_db()
        tonight.refresh_from_db()
        self.assertEqual(yesterday.status, Night.Status.REVIEW)  # untouched
        self.assertEqual(tonight.tip_total, Decimal("140.00"))  # the cash went where it said
        self.assertEqual(self.paypal.payout_bodies, [])
        self.assertContains(res, "Stopped at step 2")

    def test_a_plan_pays_the_shift_it_closed_by_its_pk(self):
        night = self.open_night_with_clock_ins()
        page = self.ask("close out tonight and pay everyone")
        token = page.context["asked"]["proposal"]["token"]
        from django.core import signing
        steps = signing.loads(token, salt=actions.SALT)["p"]["steps"]
        self.assertEqual(steps[1], ["pay_shift", {"night": night.pk, "after_change": True}])

    def test_a_plan_stops_before_money_moves_when_the_check_says_no(self):
        night = self.open_night_with_clock_ins(hours=15)  # a 15-hour shift: the check stops the payout
        page = self.ask("close out tonight and pay everyone")
        res = self.client.post(reverse("ask_confirm"), {"token": page.context["asked"]["proposal"]["token"]},
                               follow=True)
        night.refresh_from_db()
        self.assertEqual(night.status, Night.Status.REVIEW)  # closed and split, never paid
        self.assertEqual(self.paypal.payout_bodies, [])
        self.assertContains(res, "Stopped at step 2")

    def test_fix_payouts_sends_again_and_reminds(self):
        from django.core import mail
        from tips.models import PayoutBatch, PayoutItem
        night = self.ready_night()
        services.pay_night(night)
        items = list(PayoutItem.objects.filter(batch__night=night).select_related("share__staff"))
        failed = next(i for i in items if i.share.staff == self.maria)
        unclaimed = next(i for i in items if i.share.staff == self.john)
        PayoutItem.objects.filter(pk=failed.pk).update(status="FAILED")
        PayoutItem.objects.filter(pk=unclaimed.pk).update(status="UNCLAIMED")
        self.maria.paypal_email = "maria.new@example.com"
        self.maria.save()
        page = self.ask("fix the payouts that didn't arrive")
        self.assertIn("send again Maria", page.context["asked"]["proposal"]["text"])
        self.assertIn("remind John", page.context["asked"]["proposal"]["text"])
        before = len(self.paypal.payout_bodies)
        self.confirm(page)
        self.assertEqual(len(self.paypal.payout_bodies), before + 1)
        self.assertEqual(self.paypal.payout_bodies[-1][0]["receiver"], "maria.new@example.com")
        self.assertTrue(any("waiting for you" in m.subject for m in mail.outbox))
        self.assertTrue(PayoutBatch.objects.filter(night=night, is_retry=True).exists())

    def test_questions_are_not_turned_into_actions(self):
        self.open_night_with_clock_ins()
        page = self.ask("when do we close?")
        self.assertNotIn("proposal", page.context["asked"])

    @override_settings(AI_MODE="claude")
    def test_claude_plans_several_steps(self):
        self.open_night_with_clock_ins()
        out = ask.AnswerOut(answer="I'll close tonight, then pay everyone.",
                            steps=[ask.ActionOut(kind="close_shift", shift_date="2026-10-16"),
                                   ask.ActionOut(kind="pay_shift", shift_date="2026-10-16")])
        with mock.patch("tips.ai._claude_parse", return_value=out):
            result = ask.answer("close and pay tonight", [self.location], TODAY, location=self.location,
                                user=self.boss)
        self.assertEqual(len(result["proposal"]["steps"]), 2)

    def test_closing_now_ends_running_clock_ins_without_a_forgot_flag(self):
        from tips.models import ClockEntry
        night = self.open_night_with_clock_ins()
        from datetime import timedelta
        from django.utils import timezone
        # still on the clock, for 5 hours so far
        ClockEntry.objects.filter(night=night).update(ended_at=None, started_at=timezone.now() - timedelta(hours=5))
        page = self.ask("close out tonight and pay everyone")
        self.client.post(reverse("ask_confirm"), {"token": page.context["asked"]["proposal"]["token"]})
        night.refresh_from_db()
        self.assertEqual(night.status, Night.Status.PAYING)
        self.assertFalse(ClockEntry.objects.filter(night=night, auto_ended=True).exists())
