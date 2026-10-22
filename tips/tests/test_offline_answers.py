"""When no AI answers, the built-in assistant still answers the questions judges ask most."""
from datetime import date

from django.test import TestCase

from tips import ask, demo


class BuiltInAnswersTests(TestCase):
    def setUp(self):
        self.user = demo.create_demo()
        self.location = self.user.manager.business.locations.get()

    def answer(self, question):
        return ask._offline_answer(question, [self.location], date.today(), location=self.location,
                                   user=self.user)["answer"]

    def test_best_and_quietest_day(self):
        text = self.answer("Which day of the week brings the most tips, and how much more than the quietest day?")
        self.assertIn("brings the most", text)
        self.assertIn("The quietest is", text)
        self.assertIn("more per shift", text)

    def test_what_to_check_before_paying(self):
        text = self.answer("What should I check before paying yesterday's shift?")
        self.assertIn("shift:", text)
        self.assertIn("people", text)
        self.assertIn("close-out check", text)

    def test_unconfirmed_addresses_are_named(self):
        from tips.models import StaffMember
        StaffMember.objects.filter(business=self.location.business, name="Lea").update(payout_confirmed_at=None)
        self.assertIn("Not confirmed where they're paid yet: Lea", self.answer("is it safe to pay?"))

    def test_other_questions_are_unchanged(self):
        self.assertIn("earned the most this month", self.answer("Who earned the most this month?"))
