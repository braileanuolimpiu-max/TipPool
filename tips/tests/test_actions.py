from datetime import date
from decimal import Decimal
from unittest import mock

from django.core import signing
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
class ActionTests(TestCase):
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

    def test_pay_shift_after_confirming(self):
        night = self.ready_night()
        page = self.ask("pay yesterday's shift")
        self.assertContains(page, "Pay the Thursday, Oct 15 shift: $100.00 to 2 people")
        night.refresh_from_db()
        self.assertEqual(night.status, Night.Status.REVIEW)  # nothing happens before confirming
        self.confirm(page)
        night.refresh_from_db()
        self.assertEqual(night.status, Night.Status.PAYING)

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
