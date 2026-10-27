"""The bot check on guest tip pages (Cloudflare Turnstile): off until set up, and it fails open."""
import json
from unittest import mock

import requests
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from tips.models import Location, PlatformConfig, Tip
from tips.tests.helpers import make_manager, make_operator
from tips.tests.test_money import FakePayPal
from tips.vault import encrypt


def cloudflare(**answer):
    response = mock.Mock()
    response.json.return_value = answer
    return response


class BotCheckTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.location = Location.default()
        make_manager("owner")
        patcher = mock.patch("tips.services.get_client", return_value=FakePayPal())
        patcher.start()
        self.addCleanup(patcher.stop)

    def turn_on(self):
        cfg = PlatformConfig.load()
        cfg.turnstile_site_key, cfg.turnstile_secret_encrypted = "SITE-KEY", encrypt("SECRET")
        cfg.save()

    def order(self, token=None):
        body = {"amount": "5", "at": self.location.slug}
        if token is not None:
            body["botcheck"] = token
        return self.client.post(reverse("api_create_order"), data=json.dumps(body), content_type="application/json")

    def test_off_until_set_up(self):
        with mock.patch("tips.botcheck.requests.post") as post:
            self.assertEqual(self.order().status_code, 200)
        post.assert_not_called()
        self.assertNotContains(self.client.get(reverse("tip_page") + f"?at={self.location.slug}"), "challenges.cloudflare.com")

    def test_tip_page_loads_the_check_when_on(self):
        self.turn_on()
        page = self.client.get(reverse("tip_page") + f"?at={self.location.slug}")
        self.assertContains(page, "challenges.cloudflare.com/turnstile")
        self.assertContains(page, 'id="botcheck"')

    def test_a_passed_check_tips_and_a_failed_one_does_not(self):
        self.turn_on()
        with mock.patch("tips.botcheck.requests.post", return_value=cloudflare(success=True)) as post:
            self.assertEqual(self.order("good-token").status_code, 200)
        self.assertEqual(post.call_args.kwargs["data"]["secret"], "SECRET")
        with mock.patch("tips.botcheck.requests.post",
                        return_value=cloudflare(success=False, **{"error-codes": ["invalid-input-response"]})):
            res = self.order("forged-token")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(Tip.objects.count(), 1)  # no order for the failed check

    def test_fails_open_when_cloudflare_is_down_or_our_keys_are_wrong(self):
        self.turn_on()
        with mock.patch("tips.botcheck.requests.post", side_effect=requests.ConnectionError("down")):
            self.assertEqual(self.order("token").status_code, 200)
        with mock.patch("tips.botcheck.requests.post",
                        return_value=cloudflare(success=False, **{"error-codes": ["invalid-input-secret"]})):
            self.assertEqual(self.order("token").status_code, 200)

    @override_settings(RATELIMIT_ENABLED=True)
    def test_no_check_at_all_is_allowed_only_a_few_times(self):
        # the script didn't load in this browser: a real guest can still tip, a script skipping it can't flood
        self.turn_on()
        with mock.patch("tips.botcheck.requests.post") as post:
            codes = [self.order().status_code for _ in range(4)]
        self.assertEqual(codes, [200, 200, 200, 403])
        post.assert_not_called()


class BotCheckSettingsTests(TestCase):
    def setUp(self):
        self.client.force_login(make_operator("op"))
        self.url = reverse("platform_settings")

    def save(self, secret):
        return self.client.post(self.url, {"section": "botcheck", "turnstile_site_key": "SITE-KEY",
                                           "turnstile_secret": secret})

    def test_a_wrong_secret_is_refused_and_a_right_one_is_saved_hidden(self):
        with mock.patch("tips.botcheck.requests.post",
                        return_value=cloudflare(success=False, **{"error-codes": ["invalid-input-secret"]})):
            self.save("WRONG")
        self.assertEqual(PlatformConfig.load().turnstile_site_key, "")
        with mock.patch("tips.botcheck.requests.post",
                        return_value=cloudflare(success=False, **{"error-codes": ["invalid-input-response"]})):
            self.save("RIGHT")
        cfg = PlatformConfig.load()
        self.assertEqual((cfg.turnstile_site_key, cfg.turnstile_secret), ("SITE-KEY", "RIGHT"))
        self.assertNotIn("RIGHT", cfg.turnstile_secret_encrypted)
        page = self.client.get(self.url)
        self.assertContains(page, "Saved and hidden")
        self.assertNotContains(page, "RIGHT")
        self.client.post(self.url, {"section": "botcheck_off"})
        self.assertEqual(PlatformConfig.load().turnstile_secret_encrypted, "")
