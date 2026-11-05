import unittest

from mindful_chef_cli.planner import plan_away_weeks, recommend_meals
from mindful_chef_cli.preferences import default_preferences, validate_preferences


def recipe(identifier, protein="chicken", **changes):
    value = {"id": identifier, "name": f"Meal {identifier}", "protein": protein,
             "ingredients": ["rice", "tomato"], "allergens": [], "may_contain": [], "cook_time_minutes": 25,
             "reheat_suitability": "good", "finishing_effort": "low"}
    value.update(changes)
    return value


class PreferencesTests(unittest.TestCase):
    def test_fresh_neutral_defaults(self):
        a, b = default_preferences(), default_preferences()
        a["likes"].append("rice")
        self.assertEqual(b["likes"], [])
        self.assertEqual(validate_preferences(None), b)

    def test_typo_and_invalid_settings_fail(self):
        for value in ({"allergy": ["milk"]}, {"protein_targets": {"beef": True}},
                      {"max_cook_time_minutes": float("nan")}, {"likes": "rice"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_preferences(value)


class MealPlannerTests(unittest.TestCase):
    def test_balances_proteins(self):
        result = recommend_meals([recipe("a"), recipe("b"), recipe("c", "beef"), recipe("d", "pork")],
                                 {"protein_targets": {"chicken": 1, "beef": 1, "pork": 1}}, count=3)
        self.assertEqual(result["protein_counts"], {"beef": 1, "chicken": 1, "pork": 1})
        self.assertEqual(len({x["id"] for x in result["selected"]}), 3)

    def test_poultry_species_requires_one_explicit_species_in_title(self):
        result = recommend_meals([
            recipe("chicken", "Poultry", name="Chicken and rice"),
            recipe("duck", "Poultry", name="Roast duck with greens"),
            recipe("generic", "Poultry", name="Poultry bowl"),
        ], {"protein_targets": {"chicken": 1}}, count=1)
        self.assertEqual(result["selected"][0]["id"], "chicken")
        ranked = {item["id"]: item for item in result["ranked"]}
        self.assertEqual(ranked["chicken"]["proteins"], ["chicken"])
        self.assertEqual(ranked["duck"]["proteins"], ["duck"])
        self.assertEqual(ranked["chicken"]["protein_source"], "recipe_title")
        self.assertEqual(ranked["generic"]["proteins"], ["poultry"])
        self.assertEqual(ranked["generic"]["protein_source"], "provider")
        self.assertTrue(any("species unknown" in warning for warning in ranked["generic"]["warnings"]))

    def test_ambiguous_title_does_not_guess_poultry_species(self):
        result = recommend_meals([recipe("mixed", "Poultry", name="Chicken and duck bowl")],
                                 {"protein_targets": {"chicken": 1}}, count=1)
        self.assertEqual(result["selected"][0]["proteins"], ["poultry"])
        self.assertTrue(any("target not met" in warning for warning in result["warnings"]))

    def test_poultry_title_species_uses_word_boundaries(self):
        result = recommend_meals([recipe("generic", "Poultry", name="Duckweed and poultry bowl")], count=1)
        self.assertEqual(result["selected"][0]["proteins"], ["poultry"])

    def test_existing_protein_source_is_preserved(self):
        result = recommend_meals([recipe("known", "chicken", protein_source="household")], count=1)
        self.assertEqual(result["selected"][0]["protein_source"], "household")

    def test_allergen_and_unknown_lists_fail_closed(self):
        missing = recipe("missing")
        missing.pop("allergens")
        result = recommend_meals([recipe("safe"), recipe("milk", allergens=["milk"]), missing],
                                 {"allergens": ["milk"]}, count=2)
        self.assertEqual([x["id"] for x in result["selected"]], ["safe"])
        self.assertEqual(len(result["excluded"]), 2)
        self.assertTrue(any("Only 1" in x for x in result["warnings"]))

    def test_unknown_ingredients_do_not_satisfy_exclusion(self):
        missing = recipe("missing")
        missing.pop("ingredients")
        result = recommend_meals([recipe("match", ingredients=["mushrooms"]), missing, recipe("clear")],
                                 {"excluded_ingredients": ["mushrooms"]})
        self.assertEqual([x["id"] for x in result["selected"]], ["clear"])

    def test_malformed_source_lists_are_unknown(self):
        result = recommend_meals([recipe("malformed", allergens=[{"code": "unmapped"}]), recipe("clear")],
                                 {"allergens": ["milk"]}, count=1)
        self.assertEqual([x["id"] for x in result["selected"]], ["clear"])
        self.assertTrue(any("Cannot verify allergen" in reason for reason in result["excluded"][0]["reasons"]))

    def test_trace_allergens_and_unknown_trace_data_do_not_pass_hard_exclusions(self):
        unknown = recipe("unknown")
        unknown.pop("may_contain")
        result = recommend_meals([recipe("clear"), recipe("trace", may_contain=["milk"]), unknown],
                                 {"allergens": ["milk"]}, count=1)
        self.assertEqual([item["id"] for item in result["selected"]], ["clear"])
        excluded = {item["id"]: item["reasons"] for item in result["excluded"]}
        self.assertTrue(any("may-contain allergen" in reason for reason in excluded["trace"]))
        self.assertTrue(any("may-contain data" in reason for reason in excluded["unknown"]))

    def test_unknown_trace_data_warns_and_source_metadata_is_preserved(self):
        value = recipe("a", url="https://www.mindfulchef.com/recipes/synthetic", nutrition={"calories": "500 kcal"})
        value.pop("may_contain")
        selected = recommend_meals([value], count=1)["selected"][0]
        self.assertTrue(any("May-contain" in warning for warning in selected["warnings"]))
        self.assertEqual(selected["url"], value["url"])
        self.assertEqual(selected["nutrition"], value["nutrition"])

    def test_hard_reheat_and_finishing_constraints_need_evidence(self):
        unknown = recipe("unknown", reheat_suitability=None, finishing_effort=None)
        result = recommend_meals([unknown, recipe("poor", reheat_suitability="poor"),
                                  recipe("hard", finishing_effort="high"), recipe("good")],
                                 {"require_reheatable": True, "max_finishing_effort": "low"})
        self.assertEqual([x["id"] for x in result["selected"]], ["good"])

    def test_titles_are_not_reheat_evidence(self):
        result = recommend_meals([recipe("a", name="Saucy stew", reheat_suitability=None)],
                                 {"prefer_reheatable": True}, count=1)
        self.assertEqual(result["selected"][0]["reheat_suitability"], "unknown")
        self.assertTrue(any("Reheating quality unknown" in x for x in result["selected"][0]["warnings"]))

    def test_ratings_outweigh_many_selections(self):
        history = [{"recipe_id": "frequent", "selected": True}] * 100
        history += [{"recipe_id": "rated", "rating": 5}, {"recipe_id": "disliked", "rating": 1, "selected": True}]
        result = recommend_meals([recipe("frequent"), recipe("rated"), recipe("disliked")], history=history, count=1)
        self.assertEqual(result["selected"][0]["id"], "rated")
        frequent = next(item for item in result["ranked"] if item["id"] == "frequent")
        self.assertTrue(any("not evidence" in reason for reason in frequent["reasons"]))

    def test_deterministic_and_input_unchanged(self):
        recipes = [recipe("z"), recipe("a", "beef")]
        self.assertEqual(recommend_meals(recipes, count=1), recommend_meals(list(reversed(recipes)), count=1))
        self.assertNotIn("score", recipes[0])

    def test_duplicate_ids_rejected(self):
        with self.assertRaises(ValueError):
            recommend_meals([recipe("a"), recipe("a")])


class AwayPlannerTests(unittest.TestCase):
    def test_confirmed_whole_window_is_only_a_proposal(self):
        result = plan_away_weeks(["2030-08-01"], [{"start": "2030-08-01", "end": "2030-08-08",
                                                  "away": True, "confidence": "confirmed"}])
        self.assertEqual(result[0]["action"], "propose_skip")
        self.assertFalse(result[0]["account_changed"])

    def test_partial_and_uncertain_overlap_need_review(self):
        for event in ({"start": "2030-08-01", "end": "2030-08-03", "away": True, "confidence": "confirmed"},
                      {"start": "2030-08-01", "end": "2030-08-08", "away": True, "confidence": "uncertain"}):
            self.assertEqual(plan_away_weeks(["2030-08-01"], [event])[0]["action"], "review")

    def test_exclusive_end_and_not_away_are_ignored(self):
        events = [{"start": "2030-07-25", "end": "2030-08-01", "away": True, "confidence": "confirmed"},
                  {"start": "2030-08-01", "end": "2030-08-08", "away": False}]
        result = plan_away_weeks([{"id": "delivery-test", "date": "2030-08-01"}], events)
        self.assertEqual(result[0]["action"], "keep")
        self.assertEqual(result[0]["delivery_id"], "delivery-test")

    def test_combines_confirmed_intervals_without_double_counting(self):
        events = [{"start": "2030-08-01", "end": "2030-08-05", "away": True, "confidence": "confirmed"},
                  {"start": "2030-08-04", "end": "2030-08-08", "away": True, "confidence": "confirmed"}]
        self.assertEqual(plan_away_weeks(["2030-08-01"], events)[0]["confirmed_away_days"], 7)

    def test_invalid_interval_fails(self):
        with self.assertRaises(ValueError):
            plan_away_weeks(["2030-08-01"], [{"start": "2030-08-08", "end": "2030-08-01"}])


if __name__ == "__main__":
    unittest.main()
