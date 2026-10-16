from django.test import TestCase, override_settings

from tips import ai
from tips.models import Location


@override_settings(AI_MODE="mock")
class MockRuleParserTests(TestCase):
    def test_standard_sentence(self):
        out = ai.parse_rules("Bartenders get 20%, bussers get 10% split equally, the rest goes to servers by hours worked.", Location.default())
        pools = out["rules"]["pools"]
        self.assertEqual([(p["roles"], p["percent"], p["split"]) for p in pools],
                         [(["bartender"], 20.0, "hours"), (["busser"], 10.0, "equal")])
        self.assertEqual(out["rules"]["remainder"]["roles"], ["server"])

    def test_and_between_pools_and_decimals(self):
        out = ai.parse_rules("Bar gets 12.5% and bussers 7.5%. Rest to waiters.", Location.default())
        self.assertEqual([p["percent"] for p in out["rules"]["pools"]], [12.5, 7.5])
        self.assertEqual(out["rules"]["remainder"]["roles"], ["server"])

    def test_manager_share_is_flagged(self):
        out = ai.parse_rules("Managers get 5%, the rest goes to servers", Location.default())
        self.assertTrue(any("FLSA" in w for w in out["warnings"]))

    def test_over_100_is_an_error(self):
        with self.assertRaises(ai.AIError):
            ai.parse_rules("Bartenders 60%, bussers 50%", Location.default())

    def test_photo_needs_claude(self):
        with self.assertRaises(ai.AIError):
            ai.read_schedule(b"", "image/png", ["Maria"], Location.default())
