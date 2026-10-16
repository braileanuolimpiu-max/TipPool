from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from tips import ai, roles
from tips.models import RestaurantSettings, Role, StaffMember
from tips.split import ShiftInput, calculate_split


@override_settings(AI_MODE="mock")
class RolePresetTests(TestCase):
    def test_existing_restaurant_roles_are_created(self):
        self.assertEqual(Role.objects.get(key="server").name, "Server")
        self.assertTrue(Role.objects.get(key="manager").no_tips)

    def test_salon_preset_replaces_unused_roles_and_keeps_used_ones(self):
        StaffMember.objects.create(name="Alex", default_role="bartender", paypal_email="a@example.com")
        kept = roles.apply_preset("salon")
        self.assertEqual(kept, ["Bartender"])
        names = set(Role.objects.values_list("name", flat=True))
        self.assertIn("Stylist", names)
        self.assertIn("Bartender", names)
        self.assertNotIn("Busser", names)

    def test_every_preset_example_parses_with_its_own_roles(self):
        for business_type, preset in roles.PRESETS.items():
            roles.apply_preset(business_type)
            for label, sentence in preset["examples"]:
                with self.subTest(business=business_type, example=label):
                    out = ai.parse_rules(sentence)
                    keys = {k for p in out["rules"]["pools"] for k in p["roles"]}
                    keys |= set(out["rules"]["remainder"]["roles"])
                    self.assertTrue(keys)
                    self.assertTrue(keys <= set(Role.objects.values_list("key", flat=True)))
                    self.assertNotIn("manager", keys)

    def test_salon_rules(self):
        roles.apply_preset("salon")
        out = ai.parse_rules("Assistants get 15% split equally, the rest goes to hairdressers by hours")
        self.assertEqual([(p["roles"], p["percent"], p["split"]) for p in out["rules"]["pools"]],
                         [(["assistant"], 15.0, "equal")])
        self.assertEqual(out["rules"]["remainder"]["roles"], ["stylist"])

    def test_everyone_means_every_role_that_may_get_tips(self):
        roles.apply_preset("hotel")
        out = ai.parse_rules("Everyone shares the tips by hours worked.")
        remainder = out["rules"]["remainder"]
        self.assertEqual(remainder["name"], "Everyone")
        self.assertEqual(len(remainder["roles"]), Role.objects.filter(no_tips=False).count())

    def test_custom_manager_role_never_gets_tips(self):
        roles.add_role("Owner on duty", no_tips=True)
        key = Role.objects.get(name="Owner on duty").key
        rules = {"pools": [], "remainder": {"name": "All", "roles": ["server", key], "split": "hours"}}
        result = calculate_split(Decimal("100"), [ShiftInput("1", "Ana", "server", Decimal("5")),
                                                  ShiftInput("2", "Bo", key, Decimal("5"))],
                                 rules, never_in_pool={key})
        self.assertEqual({s.name: s.amount for s in result.shares}, {"Ana": Decimal("100.00"), "Bo": Decimal("0.00")})

    def test_plural(self):
        self.assertEqual([roles.plural(n) for n in ["Busser", "Front desk agent", "Barista", "Door staff", "Valet"]],
                         ["Bussers", "Front desk agents", "Baristas", "Door staff", "Valets"])


class RoleViewTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("manager", password="pw"))

    def test_add_and_remove_role(self):
        self.client.post(reverse("role_add"), {"name": "Stylist", "aliases": "barber"})
        role = Role.objects.get(name="Stylist")
        self.assertEqual(role.key, "stylist")
        self.client.post(reverse("role_remove", args=[role.pk]))
        self.assertFalse(Role.objects.filter(name="Stylist").exists())

    def test_role_in_use_cannot_be_removed(self):
        StaffMember.objects.create(name="Maria", default_role="server", paypal_email="m@example.com")
        self.client.post(reverse("role_remove", args=[Role.objects.get(key="server").pk]))
        self.assertTrue(Role.objects.filter(key="server").exists())

    def test_preset_sets_business_type_and_team_page_shows_roles(self):
        self.client.post(reverse("role_preset"), {"business_type": "salon"})
        self.assertEqual(RestaurantSettings.load().business_type, "salon")
        page = self.client.get(reverse("staff_list"))
        self.assertContains(page, "Nail tech")
        self.assertContains(self.client.get(reverse("rules")), "Assistants get 15%")

    def test_change_someones_role(self):
        maria = StaffMember.objects.create(name="Maria", default_role="server", paypal_email="m@example.com")
        self.client.post(reverse("staff_role", args=[maria.pk]), {"default_role": "bartender"})
        maria.refresh_from_db()
        self.assertEqual(maria.default_role, "bartender")
