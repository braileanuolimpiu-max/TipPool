"""Search: "Search features" in the top bar (only what this person can open) and "Find someone" lists."""
import json
import re

from django.test import TestCase
from django.urls import reverse

from tips.models import Location, Manager, Night
from tips.tests.helpers import hire, make_manager, make_operator


def jump_items(page):
    found = re.search(r'<script id="jump-data" type="application/json">(.*?)</script>', page.content.decode(), re.S)
    return json.loads(found.group(1)) if found else None


class FeatureSearchTests(TestCase):
    def test_managers_can_jump_to_every_settings_card(self):
        self.client.force_login(make_manager("owner"))
        page = self.client.get(reverse("dashboard"))
        self.assertContains(page, 'id="jump-btn"')
        items = {i["t"]: i for i in jump_items(page)}
        self.assertEqual(items["Closing time"]["u"], reverse("settings") + "#closing")
        self.assertIn("close time", items["Closing time"]["k"])
        self.assertIn("Add someone", items)
        self.assertNotIn("Platform settings", items)  # only for whoever runs the site
        self.assertTrue(items["Guest tip page"]["u"].endswith(f"?at={Location.default().slug}"))
        # every card it points to exists on its page
        for item in items.values():
            path, _, anchor = item["u"].partition("#")
            if anchor:
                self.assertContains(self.client.get(path), f'id="{anchor}"', msg_prefix=item["t"])

    def test_shift_leads_only_see_what_they_can_open(self):
        lead = make_manager("lead", owner=False)
        lead.manager.level = Manager.Level.LEAD
        lead.manager.save()
        self.client.force_login(lead)
        titles = {i["t"] for i in jump_items(self.client.get(reverse("dashboard")))}
        self.assertIn("Close out", titles)
        self.assertIn("Two-step sign-in", titles)
        for hidden in ("Settings", "Closing time", "Reports", "Team", "Tip rules", "Payments"):
            self.assertNotIn(hidden, titles)

    def test_the_operator_also_jumps_to_platform_cards(self):
        self.client.force_login(make_operator("op"))
        items = {i["t"]: i for i in jump_items(self.client.get(reverse("dashboard")))}
        self.assertEqual(items["Scheduler"]["u"], reverse("platform_settings") + "#scheduler")

    def test_no_search_for_people_without_a_business(self):
        self.assertNotContains(self.client.get(reverse("login")), 'id="jump-btn"')


class FindSomeoneTests(TestCase):
    def setUp(self):
        self.client.force_login(make_manager("owner"))
        self.ana, self.bo = hire("Ana Lopez", "server"), hire("Bo Chen", "bartender")

    def test_team_list_can_be_searched_by_name_email_and_role(self):
        page = self.client.get(reverse("staff_list")).content.decode()
        self.assertIn('data-find=".team-rows > li"', page)
        rows = re.findall(r'data-find-text="([^"]*)"', page)
        self.assertTrue(any("Ana Lopez" in r and self.ana.paypal_email in r for r in rows), rows)
        self.assertTrue(any("Bo Chen" in r and "Bartender" in r for r in rows), rows)

    def test_close_out_roster_can_be_searched(self):
        night = Night.current(Location.default())
        page = self.client.get(reverse("close_night_pk", args=[night.pk])).content.decode()
        self.assertIn('data-find=".roster > li"', page)
        rows = re.findall(r'data-find-text="([^"]*)"', page)
        self.assertTrue(any(r.startswith("Ana Lopez") for r in rows), rows)
