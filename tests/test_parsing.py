"""Synthetic provider-contract tests; no captured account fixtures."""

import json
import unittest

from mindful_chef_cli.parsing import csrf, parse_forms, parse_menu, parse_orders, parse_recipe


class ParsingTests(unittest.TestCase):
    def test_forms_keep_successful_controls_and_duplicate_names(self):
        html = '''<input form="cart" name="outside" value="yes">
        <form id="cart" action="/delivery/example" method="post">
          <input type="hidden" name="_method" value="patch">
          <input name="same[]" value="a"><input name="same[]" value="b">
          <input type="checkbox" name="off" value="1">
          <input type="checkbox" name="on" checked>
          <input type="radio" name="choice" value="a">
          <input type="radio" name="choice" value="b" checked>
          <input name="disabled" value="no" disabled>
          <fieldset disabled><input name="also_disabled" value="no"></fieldset>
          <select name="select"><option value="a">A</option><option selected value="b">B</option></select>
          <select name="multi[]" multiple><option selected value="x">X</option><option selected value="y">Y</option></select>
          <textarea name="note">text</textarea>
          <input type="submit" name="commit" value="Save">
        </form><form method="dialog"><input name="not_transport"></form>'''
        forms = parse_forms(html)
        self.assertEqual(len(forms), 1)
        self.assertEqual(forms[0]["fields"], [("outside", "yes"), ("_method", "patch"), ("same[]", "a"), ("same[]", "b"), ("on", "on"), ("choice", "b"), ("select", "b"), ("multi[]", "x"), ("multi[]", "y"), ("note", "text")])

    def test_csrf_requires_current_value(self):
        self.assertEqual(csrf('<meta name="csrf-token" content="synthetic-token">'), "synthetic-token")
        with self.assertRaises(ValueError):
            csrf("<html></html>")

    def test_menu_keeps_products_and_repeated_selection(self):
        html = '''<div data-controller="cart-item" data-cart-item-type-value="recipe" data-cart-item-id-value="r1" data-cart-item-title-value="Sample stew" data-cart-item-food-group-value="Beef" data-cart-item-price-cents-value="1200" data-cart-item-portions-value="2" data-cart-item-image-url-value="https://images.example/stew.jpg" data-cart-item-contains-gluten-value="false">
          <a href="/healthy-recipes/sample-stew?availability_id=sample">Sample stew</a><p>30 min · 500 kcal</p><p class="bg-(--recipe-tag-bg)">Family</p>
        </div>
        <form id="web_cart_form" method="post" action="/my-account/deliveries/2030-01-07" data-cart-max-recipe-count-value="5" data-cart-min-recipe-count-value="2" data-cart-min-order-amount-value="2500">
          <input name="delivery[delivery_date]" value="2030-01-07"><input name="delivery[portion_count_per_meal]" value="2">
          <input name="delivery[contents_attributes][a][recipe_id]" value="r1"><input name="delivery[contents_attributes][a][portions]" value="2">
          <input name="delivery[contents_attributes][b][recipe_id]" value="r1"><input name="delivery[contents_attributes][b][portions]" value="2">
          <input name="delivery[expanded_product_contents_attributes][c][product_id]" value="p1"><input name="delivery[expanded_product_contents_attributes][c][quantity]" value="1">
        </form>'''
        menu = parse_menu(html)
        self.assertEqual(menu["selection"], [{"recipe_id": "r1", "portions": 2}, {"recipe_id": "r1", "portions": 2}])
        self.assertEqual(menu["products_preserved"], [{"product_id": "p1", "quantity": "1"}])
        self.assertEqual(menu["recipes"][0]["cook_time_minutes"], 30)
        self.assertEqual(menu["recipes"][0]["tags"], ["Family"])
        self.assertIsNone(menu["recipes"][0]["allergens"])
        self.assertEqual(menu["portion_count"], 2)
        self.assertEqual(menu["max_recipes"], 5)

    def test_recipe_uses_jsonld_and_explicit_allergens(self):
        data = {"@graph": [{"@type": "Recipe", "name": "Example bowl", "image": ["https://images.example/bowl.jpg"], "totalTime": "PT1H5M", "recipeIngredient": ["1 bowl ingredient"], "recipeInstructions": [{"@type": "HowToSection", "itemListElement": [{"@type": "HowToStep", "text": "Cook the ingredient."}]}], "keywords": "Vegan, Family", "nutrition": {"calories": "500 calories"}}]}
        html = '<script type="application/ld+json">' + json.dumps(data) + '''</script><link rel="canonical" href="https://www.mindfulchef.com/healthy-recipes/example-bowl">
        <div><span>Allergens</span><div><div><span>Contains:</span><span>Mustard, Soy</span></div><div><span>May contain:</span><span>Peanuts</span></div></div></div>'''
        recipe = parse_recipe(html)
        self.assertEqual(recipe["cook_time_minutes"], 65)
        self.assertEqual(recipe["allergens"], ["Mustard", "Soy"])
        self.assertEqual(recipe["may_contain"], ["Peanuts"])
        self.assertEqual(recipe["steps"], ["Cook the ingredient."])
        self.assertIsNone(recipe["reheat_suitability"])
        self.assertIsNone(recipe["finishing_effort"])

    def test_missing_allergens_are_unknown_and_drift_is_detected(self):
        recipe = parse_recipe('<script type="application/ld+json">{"@type":"Recipe","name":"Example"}</script>')
        self.assertIsNone(recipe["allergens"])
        with self.assertRaises(ValueError):
            parse_recipe("<h1>Unexpected login page</h1>")

    def test_orders_ignore_account_contact_data_and_unknown_authorship(self):
        html = '''<header>Private Person private@example.invalid</header>
        <section aria-label="Delivery details 2030-01-07"><p>Recipes chosen by you</p><a href="/my-account/deliveries/2030-01-07/edit">Edit recipes</a><a href="/my-account/deliveries/2030-01-07/skip">Skip week</a><a href="/healthy-recipes/sample"><img src="https://images.example/sample.jpg" alt="Sample meal"></a></section>
        <section aria-label="Delivery details 2030-01-14"><p>Skipped</p><form method="post" action="/my-account/deliveries/2030-01-14/unskip"><input name="authenticity_token" value="synthetic"><button>Unskip</button></form></section>'''
        orders = parse_orders(html)
        self.assertEqual(orders[0]["chosen_by"], "customer")
        self.assertEqual(orders[0]["recipes"][0]["name"], "Sample meal")
        self.assertEqual(orders[1]["status"], "skipped")
        self.assertEqual(orders[1]["chosen_by"], "unknown")
        self.assertNotIn("private@example.invalid", json.dumps(orders))
        self.assertIsNotNone(orders[1]["unskip_form"])

    def test_order_sibling_headers_are_scoped_per_delivery(self):
        html = '''<main><article><div><h2>Monday 7 January</h2><p>Curated by us</p><a href="/my-account/deliveries/2030-01-07/edit">Edit</a><a href="/my-account/deliveries/2030-01-07/skips/new">Skip</a></div><div><details aria-label="Delivery details Monday 7 January"><a href="/healthy-recipes/one">Sample one</a></details></div></article>
        <article><div><h2>Monday 14 January</h2><p>Chosen by you</p><a href="/my-account/deliveries/2030-01-14/edit">Edit</a></div><div><details aria-label="Delivery details Monday 14 January"><a href="/healthy-recipes/two">Sample two</a></details></div></article></main>'''
        orders = parse_orders(html)
        self.assertEqual(len(orders), 2)
        self.assertEqual(orders[0]["delivery_date"], "2030-01-07")
        self.assertEqual(orders[0]["chosen_by"], "provider")
        self.assertEqual(orders[0]["recipes"], [{"name": "Sample one", "url": "/healthy-recipes/one", "image_url": None}])
        self.assertEqual(orders[1]["chosen_by"], "customer")

    def test_alternating_order_header_and_details_siblings(self):
        html = '''<main><div><h2>Monday 7 January</h2><p>Scheduled</p><a href="/my-account/deliveries/2030-01-07/details">Order details</a><a href="/my-account/deliveries/2030-01-07">Edit</a><a href="/my-account/deliveries/2030-01-07/skips/new">Skip</a></div>
        <div class="flex"><img src="https://images.example/one.jpg"><details aria-label="Delivery details 2030-01-07"><p>Curated by us</p><a href="/healthy-recipes/one">Sample one</a></details></div>
        <div><h2>Monday 14 January</h2><p>Skipped</p><a href="/my-account/deliveries/2030-01-14/details">Order details</a></div>
        <div class="flex"><details aria-label="Delivery details 2030-01-14"><p>Chosen by you</p></details></div></main>'''
        orders = parse_orders(html)
        self.assertEqual([o["delivery_date"] for o in orders], ["2030-01-07", "2030-01-14"])
        self.assertEqual(orders[0]["edit_url"], "/my-account/deliveries/2030-01-07")
        self.assertEqual(orders[0]["status"], "scheduled")
        self.assertEqual(orders[1]["status"], "skipped")
        self.assertIsNone(orders[1]["edit_url"])


if __name__ == "__main__":
    unittest.main()
