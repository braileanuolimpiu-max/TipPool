from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from tips import demo
from tips.models import Business, Message, Night, Tip
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
