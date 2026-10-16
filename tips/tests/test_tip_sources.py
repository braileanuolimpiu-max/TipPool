import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from tips import ai, services
from tips.models import Location, Night, Shift, Tip, TipRuleSet
from tips.tests.helpers import hire
from tips.tests.test_flow import FakePayPal


@override_settings(AI_MODE="mock")
class TipSourceTests(TestCase):
    def setUp(self):
        self.paypal = FakePayPal()
        patcher = mock.patch("tips.services.get_client", return_value=self.paypal)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client.force_login(get_user_model().objects.create_user("m", password="pw"))
        self.location = Location.default()
        self.maria = hire("Maria", "server")
        self.john = hire("John", "server")
        parsed = ai.parse_rules("Everyone shares the tips by hours worked.", self.location)
        self.rules = TipRuleSet.objects.create(location=self.location, text="x", rules=parsed["rules"],
                                               summary=parsed["summary"])
        self.night = Night.current(self.location)

    def paypal_tip(self, amount, staff=None):
        return Tip.objects.create(night=self.night, amount=Decimal(amount), paypal_order_id=f"P{Tip.objects.count()}",
                                  status="completed", staff=staff)

    def roster(self, **hours):
        for person, h in hours.items():
            Shift.objects.create(night=self.night, staff=getattr(self, person), role="server", hours=Decimal(h))

    def test_card_and_cash_tips_are_shared(self):
        self.paypal_tip("20")
        self.client.post(reverse("night_tip_add", args=[self.night.pk]), {"amount": "60", "source": "card"})
        self.client.post(reverse("night_tip_add", args=[self.night.pk]), {"amount": "20", "source": "cash"})
        self.assertEqual(self.night.tip_total, Decimal("100.00"))
        self.roster(maria="5", john="5")
        services.build_split(self.night, self.rules)
        self.assertEqual({s.staff.name: s.amount for s in self.night.shares.all()},
                         {"Maria": Decimal("50.00"), "John": Decimal("50.00")})

    def test_removing_a_tip_after_the_split_means_calculating_again(self):
        self.paypal_tip("20")
        cash = services.add_manual_tip(self.night, "30", "cash")
        self.roster(maria="5")
        services.build_split(self.night, self.rules)
        self.client.post(reverse("night_tip_remove", args=[cash.pk]))
        self.night.refresh_from_db()
        self.assertEqual(self.night.status, Night.Status.CLOSED)
        self.assertFalse(self.night.shares.exists())

    def test_paypal_tips_cannot_be_removed_by_hand(self):
        tip = self.paypal_tip("20")
        self.client.post(reverse("night_tip_remove", args=[tip.pk]))
        self.assertTrue(Tip.objects.filter(pk=tip.pk).exists())

    def test_personal_tip_goes_in_full_to_that_person(self):
        self.location.tip_mode = Location.TipMode.BOTH
        self.location.save()
        self.paypal_tip("40")                    # shared
        self.paypal_tip("15", staff=self.maria)  # for Maria only
        self.roster(maria="5", john="5")
        services.build_split(self.night, self.rules)
        shares = {s.staff.name: s for s in self.night.shares.all()}
        self.assertEqual(shares["Maria"].amount, Decimal("35.00"))
        self.assertEqual(shares["John"].amount, Decimal("20.00"))
        self.assertTrue(any(b.get("personal") for b in shares["Maria"].breakdown))

    def test_personal_only_needs_no_rules_or_roster(self):
        self.paypal_tip("25", staff=self.john)
        services.build_split(self.night, None)
        share = self.night.shares.get()
        self.assertEqual((share.staff, share.amount), (self.john, Decimal("25.00")))

    def test_guest_can_tip_a_person_only_when_allowed(self):
        member = self.maria.memberships.get()
        body = {"amount": "10", "to": member.pk}
        res = self.client.post(reverse("api_create_order"), data=json.dumps(body), content_type="application/json")
        self.assertEqual(res.status_code, 400)  # the business shares all tips
        self.location.tip_mode = Location.TipMode.BOTH
        self.location.save()
        res = self.client.post(reverse("api_create_order"), data=json.dumps(body), content_type="application/json")
        self.assertEqual(Tip.objects.get(paypal_order_id=res.json()["orderId"]).staff, self.maria)

    def test_personal_mode_requires_a_person(self):
        self.location.tip_mode = Location.TipMode.PERSONAL
        self.location.save()
        res = self.client.post(reverse("api_create_order"), data=json.dumps({"amount": "10"}),
                               content_type="application/json")
        self.assertEqual(res.status_code, 400)

    def test_tip_page_lists_people_and_qr_page_has_personal_codes(self):
        self.location.tip_mode = Location.TipMode.BOTH
        self.location.save()
        self.assertContains(self.client.get(reverse("tip_page")), "The whole team")
        self.assertContains(self.client.get(reverse("qr_page")), "Personal codes")
        member = self.maria.memberships.get()
        self.assertContains(self.client.get(reverse("qr_show") + f"?to={member.pk}"), "Tip Maria")
