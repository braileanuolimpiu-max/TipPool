"""Fixes from the November security review: staff links, the demos' AI pool, the cron key."""
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from tips import ai
from tips.models import Business, Location, Membership
from tips.tests.helpers import hire, make_manager


class StaffLinkTests(TestCase):
    def test_a_manager_of_one_location_doesnt_get_links_shared_with_another(self):
        first = Location.default()
        other = Location.objects.create(business=first.business, name="Uptown", slug="uptown")
        both = hire("Lea", "server", first)
        Membership.objects.create(staff=both, location=other, default_role="server")
        only_here = hire("Sam", "server", first)
        user = make_manager("mgr", owner=False)
        user.manager.locations.add(first)
        self.client.force_login(user)
        page = self.client.get(reverse("staff_list")).content.decode()
        self.assertIn(only_here.access_token, page)
        self.assertNotIn(both.access_token, page)
        self.client.force_login(make_manager("boss"))
        self.assertIn(both.access_token, self.client.get(reverse("staff_list")).content.decode())


@override_settings(RATELIMIT_ENABLED=True)
class DemoAIPoolTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_demos_together_get_at_most_half_the_site_allowance(self):
        demos = [Business.objects.create(name=f"Demo {n}", is_demo=True) for n in range(4)]
        real = Business.objects.create(name="Real")
        with mock.patch("tips.platform.config") as config:
            config.return_value.ai_daily_limit = 100
            spent = 0
            for business in demos:
                ai.for_business(business)
                for _ in range(ai.DEMO_DAILY):
                    try:
                        ai._spend()
                        spent += 1
                    except ai.AIError:
                        pass
            self.assertEqual(spent, 50)
            ai.for_business(real)
            ai._spend()  # a real business still has the AI
        ai.for_business(None)


class CronKeyTests(TestCase):
    def test_an_odd_key_is_simply_not_found(self):
        self.assertEqual(self.client.get(reverse("cron_tick") + "?key=%C3%A9").status_code, 404)
        self.assertEqual(self.client.get(reverse("cron_tick"), HTTP_X_CRON_KEY="é").status_code, 404)
