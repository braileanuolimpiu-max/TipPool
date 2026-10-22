"""ChatGPT, DeepSeek, Kimi and Mistral through one OpenAI-compatible connection."""
import json
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from tips import ai, ask, openai_compat, platform
from tips.models import PlatformConfig
from tips.tests.helpers import make_manager
from tips.vault import encrypt


def reply(status=200, payload=None, text=None):
    body = payload if payload is not None else {}
    return mock.Mock(status_code=status, json=lambda: body, text=text if text is not None else json.dumps(body))


def answer_with(obj):
    return reply(payload={"choices": [{"message": {"content": json.dumps(obj)}}],
                          "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


class OpenAICompatibleTests(TestCase):
    def use(self, provider):
        cfg = PlatformConfig.load()
        cfg.ai_provider, cfg.ai_enabled = provider, True
        cfg.other_ai_keys = {provider: encrypt("sk-test")}
        cfg.other_ai_models = {provider: "test-model"}
        cfg.save()
        platform.forget()

    def call(self):
        return ai._claude_parse("system", [{"type": "text", "text": "q"}], ask.AnswerOut)

    def test_each_service_answers_with_the_schema(self):
        for provider, label in (("openai", "ChatGPT"), ("deepseek", "DeepSeek"), ("kimi", "Kimi"),
                                ("mistral", "Mistral")):
            self.use(provider)
            self.assertEqual((ai.mode(), ai.name()), ("claude", label))
            with mock.patch("tips.openai_compat.requests.post", return_value=answer_with({"answer": "ok"})) as post:
                self.assertEqual(self.call().answer, "ok")
            url = post.call_args.args[0]
            self.assertTrue(url.startswith(openai_compat.PROVIDERS[provider][1]))
            body = post.call_args.kwargs["json"]
            self.assertEqual(body["model"], "test-model")
            self.assertEqual(body["response_format"]["type"], "json_schema")
            self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer sk-test")

    def test_a_service_without_schemas_gets_json_mode(self):
        self.use("deepseek")
        refused = reply(400, text='{"error": {"message": "response_format json_schema is not supported"}}')
        with mock.patch("tips.openai_compat.requests.post", side_effect=[refused, answer_with({"answer": "ok"})]) as post:
            self.assertEqual(self.call().answer, "ok")
        last = post.call_args.kwargs["json"]
        self.assertEqual(last["response_format"], {"type": "json_object"})
        self.assertIn("JSON schema", last["messages"][0]["content"])

    def test_answers_in_a_code_block_are_read(self):
        self.use("kimi")
        fenced = reply(payload={"choices": [{"message": {"content": '```json\n{"answer": "ok"}\n```'}}]})
        with mock.patch("tips.openai_compat.requests.post", return_value=fenced):
            self.assertEqual(self.call().answer, "ok")

    def test_no_credit_is_said_plainly(self):
        self.use("openai")
        with mock.patch("tips.openai_compat.requests.post", return_value=reply(402, {"error": {}})):
            with self.assertRaisesMessage(ai.AIError, "no credit"):
                self.call()


class OtherAISetupTests(TestCase):
    def setUp(self):
        op = make_manager("op")
        op.is_superuser = True
        op.save()
        self.client.force_login(op)
        self.url = reverse("platform_settings")

    def test_saving_deepseek_picks_its_model(self):
        listing = reply(payload={"data": [{"id": "deepseek-chat"}, {"id": "deepseek-flash"}]})
        with mock.patch("tips.openai_compat.requests.get", return_value=listing), \
                mock.patch("tips.openai_compat.requests.post", return_value=answer_with({"x": 1})):
            self.client.post(self.url, {"section": "ai", "ai_provider": "deepseek", "ai_enabled": "on",
                                        "key_deepseek": "sk-deep"})
        cfg = PlatformConfig.load()
        self.assertEqual((cfg.ai_provider, cfg.ai_enabled, cfg.other_ai_models["deepseek"]),
                         ("deepseek", True, "deepseek-flash"))
        self.assertNotIn("sk-deep", json.dumps(cfg.other_ai_keys))
        self.assertEqual(platform.other_ai_key("deepseek"), "sk-deep")

    def test_a_typed_model_the_service_lacks_is_refused(self):
        listing = reply(payload={"data": [{"id": "gpt-5.4-mini"}]})
        with mock.patch("tips.openai_compat.requests.get", return_value=listing):
            page = self.client.post(self.url, {"section": "ai", "ai_provider": "openai", "ai_enabled": "on",
                                               "key_openai": "sk-x", "model_openai": "gpt-nope"}, follow=True)
        self.assertContains(page, "doesn&#x27;t offer a model called gpt-nope")
        self.assertEqual(PlatformConfig.load().ai_provider, "claude")

    def test_page_shows_one_block_per_service(self):
        page = self.client.get(self.url)
        for value in ("openai", "deepseek", "kimi", "mistral", "gemini", "claude"):
            self.assertContains(page, f'data-for="{value}"')
