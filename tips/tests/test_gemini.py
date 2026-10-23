"""Gemini as a free alternative to Claude: same structured answers, set up on the website."""
import json
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from tips import ai, ask, gemini, platform
from tips.models import Location, PlatformConfig
from tips.tests.helpers import make_manager
from tips.vault import encrypt


def reply(status=200, payload=None, text=None):
    body = payload if payload is not None else {}
    return mock.Mock(status_code=status, json=lambda: body, text=text if text is not None else json.dumps(body))


def answer_with(obj):
    return reply(payload={"candidates": [{"content": {"parts": [{"text": json.dumps(obj)}]}}],
                          "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}})


class GeminiOnTests(TestCase):
    def setUp(self):
        cfg = PlatformConfig.load()
        cfg.ai_provider, cfg.ai_enabled, cfg.gemini_model = "gemini", True, "gemini-flash-latest"
        cfg.gemini_key_encrypted = encrypt("AIza-test")
        cfg.save()
        platform.forget()

    def test_the_site_runs_on_gemini(self):
        self.assertEqual((ai.mode(), ai.name()), ("claude", "Gemini"))

    def test_schema_has_no_refs(self):
        schema = gemini.inline_schema(ask.AnswerOut.model_json_schema())
        self.assertNotIn("$defs", json.dumps(schema))
        self.assertNotIn("$ref", json.dumps(schema))

    def test_structured_answer(self):
        with mock.patch("tips.gemini.requests.post", return_value=answer_with({"answer": "Lea earned the most."})) as post:
            out = ai._claude_parse("system", [{"type": "text", "text": "who earned most?"}], ask.AnswerOut)
        self.assertEqual(out.answer, "Lea earned the most.")
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["systemInstruction"]["parts"][0]["text"], "system")
        self.assertIn("responseFormat", body["generationConfig"])
        self.assertEqual(post.call_args.kwargs["headers"]["x-goog-api-key"], "AIza-test")

    def test_falls_back_to_the_older_request_shape(self):
        old = reply(400, text='{"error": {"message": "Unknown name \\"responseFormat\\""}}')
        with mock.patch("tips.gemini.requests.post", side_effect=[old, answer_with({"answer": "ok"})]) as post:
            out = ai._claude_parse("s", [{"type": "text", "text": "q"}], ask.AnswerOut)
        self.assertEqual(out.answer, "ok")
        self.assertIn("responseJsonSchema", post.call_args.kwargs["json"]["generationConfig"])

    def test_images_are_sent_inline(self):
        with mock.patch("tips.gemini.requests.post", return_value=answer_with({"answer": "x"})) as post:
            ai._claude_parse("s", [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                                "data": "AAAA"}}], ask.AnswerOut)
        part = post.call_args.kwargs["json"]["contents"][0]["parts"][0]
        self.assertEqual(part, {"inline_data": {"mime_type": "image/png", "data": "AAAA"}})

    def test_free_limit_reached_falls_back_to_the_built_in_assistant(self):
        location = Location.default()
        boss = make_manager("boss")
        with mock.patch("tips.gemini.requests.post", return_value=reply(429, {"error": {}})):
            result = ask.answer("What were our total tips this year?", [location], __import__("datetime").date.today(),
                                location=location, user=boss)
        self.assertEqual(result["by"], "offline")
        self.assertIn("couldn't answer just now", result["answer"])

    def test_labels_say_gemini(self):
        self.client.force_login(make_manager("owner"))
        self.assertContains(self.client.get(reverse("rules")), "Read by Gemini")


class GeminiSetupTests(TestCase):
    def setUp(self):
        op = make_manager("op")
        op.is_superuser = True
        op.save()
        self.client.force_login(op)
        self.url = reverse("platform_settings")

    def models_list(self, names):
        return reply(payload={"models": [{"name": f"models/{n}", "supportedGenerationMethods": ["generateContent"]}
                                         for n in names]})

    def test_saving_a_key_picks_a_flash_model_and_tests_it(self):
        listing = self.models_list(["gemini-3.6-flash", "gemini-3.8-flash", "gemini-3.8-flash-lite", "gemini-4-pro"])
        with mock.patch("tips.gemini.requests.get", return_value=listing), \
                mock.patch("tips.gemini.requests.post", return_value=reply(payload={"candidates": []})):
            self.client.post(self.url, {"section": "ai", "ai_provider": "gemini", "ai_enabled": "on",
                                        "gemini_key": "AIza-new"})
        cfg = PlatformConfig.load()
        self.assertEqual((cfg.ai_provider, cfg.gemini_model, cfg.ai_enabled), ("gemini", "gemini-3.8-flash", True))
        self.assertEqual(cfg.gemini_key, "AIza-new")
        self.assertNotIn("AIza-new", cfg.gemini_key_encrypted)
        self.assertEqual(platform.ai_name(), "Gemini")

    def test_a_bad_key_is_refused(self):
        with mock.patch("tips.gemini.requests.get", return_value=reply(400, {"error": {}})):
            page = self.client.post(self.url, {"section": "ai", "ai_provider": "gemini", "ai_enabled": "on",
                                               "gemini_key": "wrong"}, follow=True)
        self.assertContains(page, "didn&#x27;t accept that key")
        self.assertEqual(PlatformConfig.load().ai_provider, "claude")

    def test_latest_alias_is_preferred(self):
        with mock.patch("tips.gemini.requests.get",
                        return_value=self.models_list(["gemini-3.8-flash", "gemini-flash-latest"])):
            self.assertEqual(gemini.best_model("k"), ("gemini-flash-latest", ""))


from django.core.cache import cache  # noqa: E402
from django.test import override_settings  # noqa: E402


@override_settings(RATELIMIT_ENABLED=True)
class AIAllowanceTests(TestCase):
    """Accounts made just to use the AI can't run up costs or use up the free quota."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        cfg = PlatformConfig.load()
        cfg.ai_provider, cfg.ai_enabled, cfg.gemini_model = "gemini", True, "gemini-flash-latest"
        cfg.gemini_key_encrypted = encrypt("AIza-test")
        cfg.ai_daily_limit = 3
        cfg.save()
        platform.forget()
        self.location = Location.default()

    def call(self):
        return ai._claude_parse("s", [{"type": "text", "text": "q"}], ask.AnswerOut)

    def test_the_site_limit_stops_ai_calls(self):
        with mock.patch("tips.gemini.requests.post", return_value=answer_with({"answer": "ok"})) as post:
            for _ in range(3):
                self.call()
            with self.assertRaisesMessage(ai.AIError, "allowance for this site"):
                self.call()
        self.assertEqual(post.call_count, 3)
        self.assertEqual(ai.used_today(), 4)

    def test_each_business_has_its_own_share(self):
        cfg = PlatformConfig.load()
        cfg.ai_daily_limit = 0  # no site limit: only the business's own share
        cfg.save()
        business = self.location.business
        business.is_demo = True
        ai.for_business(business)
        self.addCleanup(ai.for_business, None)
        with mock.patch("tips.gemini.requests.post", return_value=answer_with({"answer": "ok"})):
            for _ in range(ai.DEMO_DAILY):
                self.call()
            with self.assertRaisesMessage(ai.AIError, "allowance for this business"):
                self.call()

    def test_rules_fall_back_to_the_built_in_reader(self):
        from tips.tests.helpers import hire
        hire("Ana", "server", self.location)
        with mock.patch("tips.gemini.requests.post", return_value=reply(429, {"error": {}})):
            parsed = ai.parse_rules("Everyone shares the tips by hours worked.", self.location)
        self.assertTrue(parsed["rules"]["remainder"])
        self.assertIn("built-in rule reader read these rules instead", parsed["warnings"][0])

    def test_ask_is_limited_per_person(self):
        self.client.force_login(make_manager("boss"))
        with mock.patch("tips.gemini.requests.post", return_value=answer_with({"answer": "ok"})):
            for _ in range(20):
                self.client.get(reverse("reports"), {"q": "who earned the most?"})
            page = self.client.get(reverse("reports"), {"q": "who earned the most?"})
        self.assertContains(page, "asked a lot")


class GeminiBusyTests(TestCase):
    def setUp(self):
        cfg = PlatformConfig.load()
        cfg.ai_provider, cfg.ai_enabled, cfg.gemini_model = "gemini", True, "gemini-flash-latest"
        cfg.gemini_key_encrypted = encrypt("AIza-test")
        cfg.save()
        platform.forget()
        patcher = mock.patch("tips.gemini.BUSY_WAIT", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_busy_once_then_answers(self):
        busy = reply(503, {"error": {"message": "high demand"}})
        with mock.patch("tips.gemini.requests.post", side_effect=[busy, answer_with({"answer": "ok"})]):
            out = ai._claude_parse("s", [{"type": "text", "text": "q"}], ask.AnswerOut)
        self.assertEqual(out.answer, "ok")

    def test_still_busy_says_so(self):
        busy = reply(503, {"error": {"message": "high demand"}})
        with mock.patch("tips.gemini.requests.post", return_value=busy):
            with self.assertRaisesMessage(ai.AIError, "very busy"):
                ai._claude_parse("s", [{"type": "text", "text": "q"}], ask.AnswerOut)

    def test_a_busy_model_still_saves_the_key(self):
        with mock.patch("tips.gemini.requests.post", return_value=reply(503, {"error": {}})):
            self.assertEqual(gemini.check("k", "gemini-flash-latest"), "")


class FallbackWhenAIUnavailableTests(TestCase):
    """When Gemini can't answer (limit, busy, network), actions still work and labels stay honest."""

    def setUp(self):
        cfg = PlatformConfig.load()
        cfg.ai_provider, cfg.ai_enabled, cfg.gemini_model = "gemini", True, "gemini-flash-latest"
        cfg.gemini_key_encrypted = encrypt("AIza-test")
        cfg.save()
        platform.forget()
        self.location = Location.default()
        self.boss = make_manager("boss")
        patcher = mock.patch("tips.gemini.requests.post", return_value=reply(429, {"error": {}}))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_actions_still_proposed_by_the_built_in_reader(self):
        from datetime import date
        from tips.tests.helpers import hire
        hire("Lea", "server", self.location)
        result = ask.answer("set Lea to Tue-Sat 7 hours", [self.location], date.today(), location=self.location,
                            user=self.boss)
        self.assertIn("proposal", result)
        self.assertIn("Lea", result["proposal"]["text"])

    def test_the_note_names_the_ai_in_use(self):
        from datetime import date
        result = ask.answer("What were our total tips this year?", [self.location], date.today(),
                            location=self.location, user=self.boss)
        self.assertTrue(result["answer"].startswith("Gemini couldn't answer just now"))

    def test_rules_page_says_who_really_read_them(self):
        from tips.tests.helpers import hire
        hire("Ana", "server", self.location)
        self.client.force_login(self.boss)
        page = self.client.post(reverse("rules"), {"text": "Everyone shares by hours worked.", "parse": "1"})
        self.assertContains(page, "Read by the built-in rule reader")
        self.assertNotContains(page, "Read by Gemini")


class GeminiRetryAfterTests(TestCase):
    def test_reads_googles_retry_delay(self):
        r = reply(429, {"error": {"details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo",
                                               "retryDelay": "7s"}]}})
        self.assertEqual(gemini.retry_after(r), 7.0)
        self.assertIsNone(gemini.retry_after(reply(429, {"error": {}})))


class GeminiTimeBudgetTests(TestCase):
    """A slow AI never holds a page: past the time budget the built-in assistant answers."""

    def setUp(self):
        cfg = PlatformConfig.load()
        cfg.ai_provider, cfg.ai_enabled, cfg.gemini_model = "gemini", True, "gemini-flash-latest"
        cfg.gemini_key_encrypted = encrypt("AIza-test")
        cfg.save()
        platform.forget()

    def test_a_hanging_gemini_falls_back(self):
        import requests
        from datetime import date
        location = Location.default()
        with mock.patch("tips.gemini.requests.post", side_effect=requests.Timeout("slow")) as post:
            result = ask.answer("Who earned the most this month?", [location], date.today(), location=location,
                                user=make_manager("boss"))
        self.assertEqual(result["by"], "offline")
        self.assertLessEqual(post.call_args.kwargs["timeout"][1], ai.TIME_BUDGET)

    def test_asks_for_little_thinking_and_drops_it_if_refused(self):
        refused = reply(400, text='{"error": {"message": "Unknown name \\"thinkingConfig\\""}}')
        with mock.patch("tips.gemini.requests.post", side_effect=[refused, answer_with({"answer": "ok"})]) as post:
            out = ai._claude_parse("s", [{"type": "text", "text": "q"}], ask.AnswerOut)
        self.assertEqual(out.answer, "ok")
        first, second = (c.kwargs["json"]["generationConfig"] for c in post.call_args_list)
        self.assertIn("thinkingConfig", first)
        self.assertNotIn("thinkingConfig", second)
