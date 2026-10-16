from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from tips import ai, ask, roles, services
from tips.models import Location, Night, Shift, Tip, TipRuleSet
from tips.tests.helpers import hire, make_manager


class TipButtonTests(TestCase):
    def setUp(self):
        self.location = Location.default()
        self.client.force_login(make_manager("m"))

    def test_defaults_follow_the_kind_of_business(self):
        self.assertEqual(self.location.tip_buttons, ("percent", [15, 18, 20, 25]))
        roles.apply_preset(self.location, "cafe")
        self.assertEqual(Location.objects.get(pk=self.location.pk).tip_buttons, ("amounts", [1, 2, 3, 5]))

    def test_percent_page_asks_for_the_bill(self):
        page = self.client.get(reverse("tip_page"))
        self.assertContains(page, "Your bill")
        self.assertContains(page, 'data-percent="18"')

    def test_custom_buttons_and_message(self):
        self.client.post(reverse("settings"), {"tip_style": "amounts", "tip_value_0": "20", "tip_value_1": "4",
                                               "tip_value_2": "8", "tip_value_3": "12", "tip_message": "Thank you!",
                                               "after_close": "review", "timezone": "UTC", "tip_mode": "pool"})
        loc = Location.objects.get(pk=self.location.pk)
        self.assertEqual(loc.tip_buttons, ("amounts", [4, 8, 12, 20]))
        page = self.client.get(reverse("tip_page"))
        self.assertContains(page, "Thank you!")
        self.assertContains(page, 'data-amount="12"')
        self.assertNotContains(page, "Your bill")

    def test_bad_values_are_ignored(self):
        self.client.post(reverse("settings"), {"tip_style": "percent", "tip_value_0": "500", "tip_value_1": "4",
                                               "tip_value_2": "8", "tip_value_3": "12",
                                               "after_close": "review", "timezone": "UTC", "tip_mode": "pool"})
        self.assertEqual(Location.objects.get(pk=self.location.pk).tip_style, "")


@override_settings(AI_MODE="mock")
class AskTests(TestCase):
    def setUp(self):
        self.location = Location.default()
        self.client.force_login(make_manager("m"))
        self.maria = hire("Maria", "server")
        self.sam = hire("Sam", "busser")
        parsed = ai.parse_rules("Bussers get 20%, the rest goes to servers by hours", self.location)
        rules = TipRuleSet.objects.create(location=self.location, text="x", rules=parsed["rules"],
                                          summary=parsed["summary"])
        for day, amount in [(date(2026, 10, 12), "100"), (date(2026, 10, 14), "50"), (date(2026, 9, 20), "80")]:
            night = Night.objects.create(location=self.location, date=day)
            Tip.objects.create(night=night, amount=Decimal(amount), paypal_order_id=f"A{day}", status="completed")
            Shift.objects.create(night=night, staff=self.maria, role="server", hours=Decimal("5"))
            Shift.objects.create(night=night, staff=self.sam, role="busser", hours=Decimal("5"))
            services.build_split(night, rules)
        self.today = date(2026, 10, 16)

    def ask(self, q):
        return ask.answer(q, [self.location], self.today)["answer"]

    def test_person_this_month(self):
        self.assertEqual(self.ask("How much did Maria make this month?"),
                         "Maria made $120.00 this month: 2 shifts, 10 hours, about $12.00 an hour from shared tips.")

    def test_role_in_a_named_month(self):
        self.assertIn("Bussers made $16.00 in September", self.ask("how much did bussers get in september"))

    def test_top_earner_and_total(self):
        self.assertIn("Maria earned the most this week: $120.00", self.ask("Who earned the most this week?"))
        self.assertIn("Total tips this year: $230.00", self.ask("What were our total tips this year?"))

    def test_tie_names_everyone(self):
        john = hire("John", "server")
        night = Night.objects.create(location=self.location, date=date(2026, 8, 3))
        Tip.objects.create(night=night, amount=Decimal("20"), paypal_order_id="TIE", status="completed")
        for person in (self.maria, john):
            Shift.objects.create(night=night, staff=person, role="server", hours=Decimal("4"))
        services.build_split(night, TipRuleSet.active(self.location))
        self.assertEqual(self.ask("who earned the most in august"),
                         "John and Maria earned the most in August: $10.00 each.")

    def test_unknown_question_lists_examples(self):
        self.assertIn("Offline I can answer", self.ask("what's the weather"))

    def test_page_shows_answer(self):
        page = self.client.get(reverse("reports") + "?q=Who+earned+the+most+this+year%3F")
        self.assertContains(page, "Maria earned the most this year")

    @override_settings(AI_MODE="claude")
    def test_claude_gets_the_numbers(self):
        with mock.patch("tips.ai._claude_parse", return_value=ask.AnswerOut(answer="Maria: $120.00")) as call:
            out = ask.answer("How did Maria do?", [self.location], self.today)
        self.assertEqual(out, {"answer": "Maria: $120.00", "by": "Claude"})
        sent = call.call_args[0][1][0]["text"]
        self.assertIn("October 2026", sent)
        self.assertIn("Maria (Server)", sent)
        self.assertIn("Question: How did Maria do?", sent)
