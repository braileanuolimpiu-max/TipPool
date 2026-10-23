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
        # whatever the sample day holds: nothing unusual, a note worth a look, or a stop
        self.assertTrue(any(w in text for w in ("close-out check", "Worth a look", "The check stops it")), text)

    def test_unconfirmed_addresses_are_named(self):
        from tips.models import Night, StaffMember
        waiting = Night.objects.filter(location=self.location, status=Night.Status.REVIEW).first()
        person = waiting.shares.filter(amount__gt=0).first().staff
        StaffMember.objects.filter(pk=person.pk).update(payout_confirmed_at=None)
        self.assertIn(f"Not confirmed where they're paid yet: {person.name}", self.answer("is it safe to pay?"))

    def test_other_questions_are_unchanged(self):
        self.assertIn("earned the most this month", self.answer("Who earned the most this month?"))


class NoShiftsYetTests(TestCase):
    def test_a_new_business_is_told_what_to_do(self):
        from tips.models import Location
        text = ask._offline("How much did servers make last week?", [Location.default()], date(2026, 10, 23))
        self.assertIn("no closed shifts last week (Oct 12 to Oct 18)", text)
        self.assertIn("Close out tab", text)
