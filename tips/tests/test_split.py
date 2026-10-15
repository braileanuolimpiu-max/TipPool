from decimal import Decimal

from django.test import SimpleTestCase

from tips.split import RulesError, ShiftInput, calculate_split, compliance_warnings

RULES = {
    "pools": [
        {"name": "Bartenders", "percent": 20, "roles": ["bartender"], "split": "hours"},
        {"name": "Bussers", "percent": 10, "roles": ["busser"], "split": "equal"},
    ],
    "remainder": {"name": "Servers", "roles": ["server"], "split": "hours"},
    "excluded_roles": ["manager"],
}


def shift(name, role, hours):
    return ShiftInput(key=name, name=name, role=role, hours=Decimal(str(hours)))


def amounts(result):
    return {s.name: s.amount for s in result.shares}


class SplitTests(SimpleTestCase):
    def test_example_from_the_plan(self):
        """$600: bartender 20% = 120, busser 10% = 60, servers share 420 by hours."""
        shifts = [shift("Maria", "server", 6), shift("Lea", "server", 5), shift("John", "server", 4),
                  shift("Alex", "bartender", 7), shift("Sam", "busser", 6)]
        result = calculate_split(Decimal("600"), shifts, RULES)
        self.assertEqual(amounts(result), {
            "Maria": Decimal("168.00"), "Lea": Decimal("140.00"), "John": Decimal("112.00"),
            "Alex": Decimal("120.00"), "Sam": Decimal("60.00"),
        })

    def test_cents_always_add_up(self):
        shifts = [shift("A", "server", 3), shift("B", "server", 3), shift("C", "server", 3)]
        result = calculate_split(Decimal("100.00"), shifts, RULES)
        self.assertEqual(sum(amounts(result).values()), Decimal("100.00"))
        self.assertEqual(sorted(amounts(result).values()), [Decimal("33.33"), Decimal("33.33"), Decimal("33.34")])

    def test_odd_totals_many_people(self):
        shifts = [shift(f"S{i}", "server", 1 + i * 0.75) for i in range(7)] + [shift("Bar", "bartender", 5.5)]
        for total in ["0.01", "1.00", "99.99", "1234.57"]:
            result = calculate_split(Decimal(total), shifts, RULES)
            self.assertEqual(sum(amounts(result).values()), Decimal(total))

    def test_pool_with_nobody_goes_to_the_rest(self):
        shifts = [shift("Maria", "server", 5), shift("John", "server", 5)]
        result = calculate_split(Decimal("100"), shifts, RULES)
        self.assertEqual(amounts(result), {"Maria": Decimal("50.00"), "John": Decimal("50.00")})
        self.assertTrue(any("Bartenders" in n for n in result.notes))

    def test_manager_never_gets_tips(self):
        shifts = [shift("Boss", "manager", 8), shift("Maria", "server", 5)]
        result = calculate_split(Decimal("50"), shifts, RULES)
        self.assertEqual(amounts(result)["Boss"], Decimal("0.00"))
        self.assertEqual(amounts(result)["Maria"], Decimal("50.00"))

    def test_role_without_rule_gets_nothing_and_is_noted(self):
        shifts = [shift("Cook", "cook", 8), shift("Maria", "server", 5)]
        result = calculate_split(Decimal("50"), shifts, RULES)
        self.assertEqual(amounts(result)["Cook"], Decimal("0.00"))
        self.assertTrue(any("Cook" in n for n in result.notes))

    def test_no_remainder_group_shares_with_everyone(self):
        shifts = [shift("Alex", "bartender", 4), shift("Cook", "cook", 4)]
        result = calculate_split(Decimal("100"), shifts, RULES)
        self.assertEqual(sum(amounts(result).values()), Decimal("100.00"))
        self.assertEqual(amounts(result)["Alex"], Decimal("60.00"))  # 20 + half of 80

    def test_over_100_percent_is_rejected(self):
        bad = {"pools": [{"name": "A", "percent": 70, "roles": ["server"], "split": "hours"},
                         {"name": "B", "percent": 40, "roles": ["busser"], "split": "hours"}]}
        with self.assertRaises(RulesError):
            calculate_split(Decimal("10"), [shift("A", "server", 1)], bad)

    def test_manager_in_pool_gets_legal_warning(self):
        rules = {"pools": [{"name": "Manager", "percent": 5, "roles": ["manager"], "split": "equal"}],
                 "remainder": {"name": "Servers", "roles": ["server"], "split": "hours"}}
        self.assertTrue(any("FLSA" in w for w in compliance_warnings(rules)))
