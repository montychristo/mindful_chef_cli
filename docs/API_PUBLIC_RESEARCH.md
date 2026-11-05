# Storefront contract research

Verified against Mindful Chef's publicly served website on 5 November 2026.
This is an unofficial client of the website, not a documented or supported
Mindful Chef API. The observed frontend is Rails, Turbo, and Stimulus, with
server-rendered HTML and ordinary form submissions. There is no verified OAuth,
JWT, or refresh-token contract in these public sources.

## Public reads

| Method | Path | Observed contract |
| --- | --- | --- |
| GET | `/session/new` | Sign-in form and live CSRF metadata |
| GET | `/choose-recipes` | Current public menu, complete recipe cards, dynamic cart constraints |
| GET | `/healthy-recipes` | Recipe archive and pagination links |
| GET | `/healthy-recipes/<slug>` | Recipe JSON-LD, ingredient details, explicit allergen section |

All these reads returned HTML successfully during inspection. Public menu output
contained 28 unique recipe IDs, each with a detail link and image. This is a
point-in-time check, not a guarantee about future menu sizes or availability.

The archive exposes pagination using `?page=2` and observed filters including
`rating=4%2B`, `season=Autumn`, and `cooking_time=0-15`. Follow current links rather
than treating those examples as a permanent set of filter options.

## Authentication

The [public sign-in form](https://www.mindfulchef.com/session/new) uses `POST
/session` and form-encoded `email`, `password`, and `authenticity_token`. The
password input currently advertises a maximum length of 72. Read the form and
its CSRF token at runtime; do not bake in token values or assume that a successful
HTTP response proves successful authentication. Persist the resulting session
only in the user's protected credential store and verify the signed-in account
through an authenticated read. Do not copy credentials from a personal browser.

Expired sessions should require a new interactive login unless a separately
verified refresh mechanism is added. A password is not needed after successful
login and should not be retained for unattended retries.

## Menu data and delivery-form inputs

Menu cards expose `data-cart-item-*` attributes for recipe ID, title, food group,
cuisine, portions, price in cents, image URL, gluten flag, and whether the recipe
is within the account's preferences. A card's recipe link can include an
`availability_id`; preserve this context when requesting the detail page.
Cooking time, calories, and provider tags also appear in the card's HTML.

The public menu's `form#web_cart_form` uses `POST /choose-recipes`. That is the
onboarding form; it must not be confused with an existing account's delivery
update form. Account-specific actions must be read from the authenticated page.

The [public cart controller](https://www.mindfulchef.com/assets/controllers/cart_controller-0e433aff.js)
confirms that adding or removing a recipe updates hidden fields locally until
the cart form is submitted. It does not make a separate HTTP request for each
add button. Recipe selection fields use:

```text
delivery[contents_attributes][unique_ref][recipe_id]
delivery[contents_attributes][unique_ref][portions]
```

Repeated recipe IDs are permitted with different fieldset references. Form
controls also include delivery date, portions, selected delivery option, and a
`recurring_items_loaded_at` value. The form carries dynamic minimum/maximum
recipe counts, order value, and shipping amount as `data-cart-*` attributes.
Do not hard-code prices, portion choices, limits, or delivery charges.

Selection order is meaningful: the provider preserves submission order in the
account's meal display. Snapshots, proposals, form fields and rollback plans must
retain the original ordered sequence, including repeated recipe IDs. Comparing
only a set or sorted list of recipes can miss a visible account change.

When changing recipe selections, preserve unrelated product fields and product
subscription state. These appear under
`delivery[expanded_product_contents_attributes][ref]` and include `product_id`,
`quantity`, `purchase_type`, `delivery_frequency_weeks`, and optional `start_date`.
The form can also carry `delivery[cancelled_product_ids][]`; a recipe update
must not accidentally add or cancel recurring products.

## Recipe details

A [public recipe detail page](https://www.mindfulchef.com/healthy-recipes/chicken-rendeng)
exposes `application/ld+json` with a `Recipe` node, sometimes within `@graph`.
Observed fields include name, description, image array, ISO duration values,
yield, cuisine, keywords, ingredient strings, ordered cooking steps, and
nutrition. The parser returns provider text as data and does not treat it as
instructions for the agent.

Allergens are separately displayed under an explicit HTML `Allergens` section
with `Contains:` and potentially `May contain:` labels. They are not present in
the observed JSON-LD. A missing declaration remains unknown; an ingredient list
does not prove the absence of allergens. The provider does not supply verified
reheating suitability or finishing-effort fields in this sample. Those remain
unknown until separately assessed, with any inference labelled as such.

## Verification boundaries

The parser tests use synthetic markup only. Read-only parsing was also checked
against freshly downloaded public menu and recipe pages, held outside the repo.
No credentials, session cookies, account HTML, customer identifiers, or personal
history belong in fixtures, source control, examples, or this document.

Public reads alone do not verify account login, saving recipes, skipping a
delivery, or restoring one. These require current authenticated forms and
readback. Site changes should cause explicit contract errors rather than broad
endpoint guessing or a retry of an uncertain write.

## Authenticated routes and acceptance

An authenticated browser inspection established the following generic website
routes and form shapes. That discovery pass was read-only. A later explicitly
authorised CLI acceptance run verified login, authenticated reads, a meal change
and restoration of the original ordered selection, and a delivery skip followed
by resume. Fresh readback verified restored delivery status, exact recipe order,
portions and price, with no unresolved write holds. The upcoming-order state was
also compared with its original baseline and verified restored.
No account identifiers, selected meals, customer details, account-specific
dates, prices, or captured account pages are included in this repository.

| Method | Path | Evidence and limits |
| --- | --- | --- |
| GET | `/my-account/orders` | Delivery cards and their current action links |
| GET | `/my-account/orders/past` | Past-delivery history page |
| GET | `/my-account/deliveries/<YYYY-MM-DD>` | Delivery-specific recipe menu and cart form |
| POST | `/my-account/deliveries/<YYYY-MM-DD>` | Live update form with `_method=patch`; meal change and rollback verified by readback |
| GET | `/my-account/deliveries/<YYYY-MM-DD>/skips/new?step=away` | Holiday-skip form with live reason and CSRF fields |
| POST | `/my-account/deliveries/<YYYY-MM-DD>/skips` | Live skip form submitted in a controlled acceptance run; skipped state verified |
| GET | `/my-account/deliveries/<YYYY-MM-DD>/restores/new` | Provider-emitted restore link from the skipped order; confirmation page inspected |
| POST | `/my-account/deliveries/<YYYY-MM-DD>/restores` | Live restore form has one `authenticity_token` and no `_method` override; resume and original-state restoration verified by fresh readback |

The initial browser inspection redacted hidden delivery-update values; the later
successful CLI write established the permitted `patch` contract. The client still
fails closed if the runtime form emits a different override. Do not treat a form,
redirect, or HTTP success alone as proof that a mutation succeeded.

Delivery-card headers and their details disclosures can be alternating sibling
containers. The parser associates a preceding header only when its details link
contains the same delivery date. The website labels selection provenance
`Chosen by you` or `Curated by us`; neither label establishes whether a household
liked the meal.

The observed current resume flow is the provider-emitted `/restores/new` link and
its `POST /restores` form. The client must discover that link in the current
order page, constrain it to the exact same-origin delivery, and read the live
confirmation form rather than inventing a restore endpoint. The old assumed
`DELETE /skips` shape is not the current live contract; it is usable only if a
future page actually emits a recognised form of that shape.

The skipped delivery's canonical menu remains readable in the observed flow and
provides the ordered recipes, portions and current prices needed for a reviewed
restore proposal. Resume must still reject unknown contents or prices, extras,
changed state and an insufficient explicit spending ceiling. The confirmation
form must have exactly one nonempty CSRF control and the expected HTML method,
action and override rules. A current preview is not a guarantee that a later
resume will be available or will succeed. Final readback must verify active
status and the exact reviewed ordered selection and portions.
