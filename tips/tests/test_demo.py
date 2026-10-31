from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from tips import demo
from tips.models import Business, Location, Message, Night, Tip
from tips.tests.helpers import make_manager


@override_settings(AI_MODE="claude", CRON_SECRET="s3cret", DEMO_BUYER_EMAIL="buyer@sandbox.example", DEMO_BUYER_PASSWORD="pw123")
class DemoTests(TestCase):
    def test_try_the_demo(self):
        res = self.client.post(reverse("demo_start"))
        self.assertRedirects(res, reverse("dashboard"))
        business = Business.objects.get(is_demo=True)
        location = business.locations.get()
        nights = Night.objects.filter(location=location)
        self.assertGreater(nights.filter(status=Night.Status.PAID).count(), 10)
        self.assertEqual(nights.filter(status=Night.Status.REVIEW).count(), 1)
        self.assertEqual(nights.filter(status=Night.Status.OPEN).count(), 1)
        self.assertTrue(Message.objects.filter(location=location).exists())
        page = self.client.get(reverse("dashboard"))
        self.assertContains(page, "Demo business")
        self.assertContains(self.client.get(reverse("reports") + "?period=last_30"), "Maria")
        tip_page = self.client.get(reverse("tip_page") + "?at=" + location.slug)
        self.assertContains(tip_page, "buyer@sandbox.example")
        # every split is exact, even with fees and card/cash tips
        for night in nights.filter(status=Night.Status.PAID):
            self.assertEqual(sum(s.amount for s in night.shares.all()), night.tip_total)

    def test_demo_history_uses_no_ai_calls(self):
        from unittest import mock
        with mock.patch("tips.ai._claude_parse", side_effect=AssertionError("no AI calls")):
            demo.create_demo()

    def test_sample_data_only_for_an_empty_business(self):
        user = make_manager("owner")
        self.client.force_login(user)
        self.assertContains(self.client.get(reverse("dashboard")), "Load sample data")
        self.client.post(reverse("load_sample"))
        location = user.manager.business.locations.first()
        count = Tip.objects.filter(night__location=location).count()
        self.assertGreater(count, 0)
        self.client.post(reverse("load_sample"))  # not twice
        self.assertEqual(Tip.objects.filter(night__location=location).count(), count)

    def test_cleanup_and_tick(self):
        demo.create_demo()
        real = make_manager("real").manager.business
        Business.objects.filter(is_demo=True).update(created_at=timezone.now() - timedelta(days=8))
        self.assertEqual(self.client.get(reverse("cron_tick") + "?key=wrong").status_code, 404)
        res = self.client.get(reverse("cron_tick") + "?key=s3cret")
        self.assertEqual(res.json()["demos_removed"], 1)
        self.assertFalse(Business.objects.filter(is_demo=True).exists())
        self.assertTrue(Business.objects.filter(pk=real.pk).exists())
        self.assertFalse(get_user_model().objects.filter(username__startswith="demo-").exists())


class TourTests(TestCase):
    def test_tour_ticks_off_what_was_seen(self):
        self.client.post(reverse("demo_start"))
        page = self.client.get(reverse("dashboard"))
        self.assertContains(page, "Demo tour")
        self.assertEqual(page.context["demo_tour"]["done"], 0)
        location = Business.objects.get(is_demo=True).locations.get()
        self.client.get(reverse("rules"))
        self.client.get(reverse("reports"))  # without a question it doesn't count
        self.client.get(reverse("tip_page") + "?at=" + location.slug)
        page = self.client.get(reverse("dashboard"))
        done = [s["title"] for s in page.context["demo_tour"]["steps"] if s["done"]]
        self.assertEqual(done, ["Tip as a guest", "Change the rules in plain words"])
        self.assertTrue(page.context["demo_tour"]["steps"][0]["next"])

    def test_no_tour_for_real_businesses(self):
        self.client.force_login(make_manager("real"))
        page = self.client.get(reverse("dashboard"))
        self.assertNotContains(page, "Demo tour")


class SampleHistoryLabelTests(TestCase):
    def test_demo_history_says_no_money_was_sent(self):
        from tips import demo
        from tips.models import Night
        self.client.force_login(demo.create_demo())
        old = Night.objects.filter(status="paid").first()
        page = self.client.get(reverse("payout_status", args=[old.pk]))
        self.assertContains(page, "no money was sent")
        self.assertNotContains(page, "Check PayPal again")
        self.assertContains(self.client.get(reverse("history")), ">Sample<")


class DemoRulesTests(TestCase):
    """The demo's fixed house rules are read by the AI once and reused, so demos don't spend the AI's daily requests."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.addCleanup(cache.clear)  # the made-up rules below must not reach the other demo tests

    def test_an_ai_read_is_kept_and_reused(self):
        from django.core.cache import cache
        from tips import demo
        cache.clear()
        read = {"rules": {"pools": []}, "summary": "s", "warnings": [], "read_by": "Gemini"}
        with mock.patch("tips.demo.ai.parse_rules", return_value=read) as parse:
            demo._demo_rules(Location.default())
            demo._demo_rules(Location.default())
        self.assertEqual(parse.call_count, 1)

    def test_a_built_in_read_is_not_kept(self):
        from django.core.cache import cache
        from tips import demo
        cache.clear()
        read = {"rules": {"pools": []}, "summary": "s", "warnings": [], "read_by": "the built-in rule reader"}
        with mock.patch("tips.demo.ai.parse_rules", return_value=read) as parse:
            demo._demo_rules(Location.default())
            demo._demo_rules(Location.default())
        self.assertEqual(parse.call_count, 2)
