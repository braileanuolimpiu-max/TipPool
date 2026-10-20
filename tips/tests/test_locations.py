import json
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from tips import roles
from tips.models import Location, Membership, Night, Role, StaffMember, Tip
from tips.tests.helpers import hire, make_manager
from tips.tests.test_flow import FakePayPal


@override_settings(AI_MODE="mock")
class LocationTests(TestCase):
    def setUp(self):
        self.paypal = FakePayPal()
        patcher = mock.patch("tips.services.get_client", return_value=self.paypal)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client.force_login(make_manager("owner"))
        self.diner = Location.default()
        self.client.post(reverse("location_add"), {"name": "Luna Salon", "business_type": "salon"})
        self.salon = Location.objects.get(name="Luna Salon")

    def test_new_location_gets_its_own_roles_and_is_selected(self):
        self.assertEqual(self.salon.slug, "luna-salon")
        self.assertTrue(Role.objects.filter(location=self.salon, name="Stylist").exists())
        self.assertFalse(Role.objects.filter(location=self.diner, name="Stylist").exists())
        self.assertContains(self.client.get(reverse("staff_list")), "Nail tech")

    def test_switching_location(self):
        self.client.post(reverse("location_switch"), {"location": self.diner.pk, "next": "/staff/"})
        self.assertNotContains(self.client.get(reverse("staff_list")), "Nail tech")

    def test_tip_goes_to_the_location_in_the_link(self):
        Location.objects.filter(pk=self.salon.pk).update(tip_mode="pool")  # salons start with personal tips
        res = self.client.post(reverse("api_create_order"), data=json.dumps({"amount": "5", "at": "luna-salon"}),
                               content_type="application/json")
        tip = Tip.objects.get(paypal_order_id=res.json()["orderId"])
        self.assertEqual(tip.night.location, self.salon)
        # a link that doesn't name a real location is refused: the tip must never land in another business
        for body in ({"amount": "5"}, {"amount": "5", "at": "no-such-place"}):
            res = self.client.post(reverse("api_create_order"), data=json.dumps(body), content_type="application/json")
            self.assertEqual(res.status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("tip_page")).status_code, 404)

    def test_each_location_has_its_own_open_shift(self):
        self.assertNotEqual(Night.current(self.diner), Night.current(self.salon))

    def test_same_person_at_two_locations_has_one_tips_page(self):
        hire("Ana", "server", self.diner, email="ana@example.com")
        self.client.post(reverse("staff_list"), {"name": "Ana", "paypal_email": "ANA@example.com",
                                                 "default_role": "stylist"})
        ana = StaffMember.objects.get()
        self.assertEqual(set(Membership.objects.filter(staff=ana).values_list("default_role", flat=True)),
                         {"server", "stylist"})
        page = self.client.get(reverse("my_tips", args=[ana.access_token]))
        self.assertContains(page, "Luna Salon")

    def test_other_locations_shift_is_not_reachable(self):
        night = Night.current(self.diner)  # the salon is selected
        self.assertEqual(self.client.get(reverse("review", args=[night.pk])).status_code, 404)

    def test_rules_use_the_locations_roles(self):
        self.client.post(reverse("rules"), {"text": "Assistants get 15%, the rest goes to stylists by hours",
                                            "parse": "1"})
        self.client.post(reverse("rules"), {"save": "1"})
        rule_set = self.salon.rule_sets.get()
        self.assertEqual(rule_set.rules["remainder"]["roles"], ["stylist"])
        self.assertFalse(self.diner.rule_sets.exists())

    def test_qr_links_name_the_location(self):
        self.assertContains(self.client.get(reverse("qr_page")), "at=luna-salon")

    def test_close_out_only_lists_this_locations_team(self):
        hire("Bo", "server", self.diner)
        hire("Cy", "stylist", self.salon)
        page = self.client.get(reverse("close_night"))
        self.assertContains(page, "Cy")
        self.assertNotContains(page, "Bo</strong>")

    def test_split_and_pay_at_a_second_location(self):
        cy = hire("Cy", "stylist", self.salon)
        self.client.post(reverse("rules"), {"text": "Everyone shares the tips by hours worked.", "parse": "1"})
        self.client.post(reverse("rules"), {"save": "1"})
        night = Night.current(self.salon)
        Tip.objects.create(night=night, amount=Decimal("40"), paypal_order_id="S1", status="completed")
        res = self.client.post(reverse("close_night"), {f"worked_{cy.pk}": "on", f"hours_{cy.pk}": "5",
                                                        f"role_{cy.pk}": "stylist"})
        self.assertRedirects(res, reverse("review", args=[night.pk]))
        self.assertEqual(night.shares.get().amount, Decimal("40.00"))

    def test_preset_is_per_location(self):
        roles.apply_preset(self.salon, "hotel")
        self.assertEqual(Location.objects.get(pk=self.salon.pk).business_type, "hotel")
        self.assertEqual(Location.objects.get(pk=self.diner.pk).business_type, "restaurant")
