# Comparison with mcp-hellofresh

Reviewed on 5 November 2026 against
[`markswendsen-code/mcp-hellofresh`](https://github.com/markswendsen-code/mcp-hellofresh/tree/eee05ba7340845dd774a6de9d78b2a0f62b923e0),
commit `eee05ba7340845dd774a6de9d78b2a0f62b923e0`.
This is source inspection, not a HelloFresh login or live acceptance test.
Registration of a tool does not establish that its website workflow works.

## Capability map

The reference registers twelve tools. Its contracts are in
[`src/index.ts`](https://github.com/markswendsen-code/mcp-hellofresh/blob/eee05ba7340845dd774a6de9d78b2a0f62b923e0/src/index.ts#L134).

| Capability | HelloFresh reference | Mindful Chef CLI |
| --- | --- | --- |
| Future menus | Week offsets 0–4 | Exact delivery dates from upcoming orders |
| Recipe details and images | Menu/detail readers | Menu images, ingredients, steps, nutrition and declared allergens |
| Meal selection | Direct selection tool | Proposal, price ceiling, fresh-state check, single submit and readback |
| Skip a delivery | Skip tool | Reviewed skip proposal |
| Resume a delivery | No registered tool | Conditional restore form and reviewed contents/price |
| Upcoming deliveries | Schedule reader | Upcoming orders and their selections/status |
| Change delivery date | Move tool | Not implemented |
| Provider preferences | Read/update tools | Private household preferences; provider settings are not synchronised |
| Subscription plan | Read/change tools | No general subscription read/change command |
| Past orders | Count-limited reader | Explicitly paginated orders; recommendation history states partial coverage |
| Recipe feedback | Provider rating/comment tool | Private local ratings and reheating/finishing assessments |
| Export | No dedicated export tool | JSON CLI output; no dedicated document export |

The local MCP server exposes browsing, recommendations, away planning and reviewed
changes. `preferences_update`, `recipe_rate` and `recipe_assess` now save confirmed
household feedback through MCP. These are local memory writes, distinct from
provider-account changes. Omitted preference settings and recipe assessments are
preserved. The skill explains how to show returned image URLs in a host that
supports images.

## What the reference actually implements

Several advertised inputs do not reach a corresponding operation: meal servings
are unused, preference arrays are unused, and subscription frequency is unused.
The menu tool accepts a week offset, not dietary filter arguments. Some missing
recipe/subscription values receive invented defaults. Mutation methods report
click-based success without independently checking final account state.
These are source findings, not observed failures against HelloFresh.
See the implementations of
[`selectMeals`](https://github.com/markswendsen-code/mcp-hellofresh/blob/eee05ba7340845dd774a6de9d78b2a0f62b923e0/src/browser.ts#L443),
[`updatePreferences`](https://github.com/markswendsen-code/mcp-hellofresh/blob/eee05ba7340845dd774a6de9d78b2a0f62b923e0/src/browser.ts#L632),
and [`modifySubscription`](https://github.com/markswendsen-code/mcp-hellofresh/blob/eee05ba7340845dd774a6de9d78b2a0f62b923e0/src/browser.ts#L727).

The reference starts a fresh headless browser and takes credentials from environment
variables. Mindful Chef CLI prompts in a terminal and persists only session
material in the OS credential store. It also provides private local preferences,
protein balancing, household feedback, make-ahead assessments, holiday proposals,
read-only health checks and unresolved-write holds. The reference should guide
use-case discovery, not replace provider-specific evidence or these safeguards.

## Useful next capabilities

1. **Agent-accessible household feedback — implemented.** Validated local
   preferences, ratings and recipe assessments are available through MCP without
   enabling provider-account writes. Their descriptions require user-supplied or
   confirmed feedback, and the tools do not contact Mindful Chef.
2. **Read provider preferences and plan settings.** Add a narrow, current summary
   of servings, frequency, meal plan and dietary selections after verifying the
   Mindful Chef page contract. Keep provider settings separate from local
   household judgments and exclude contact/payment fields.
3. **Delivery-date changes.** Add a reviewed move proposal only after available
   dates, fees, cutoff rules and restoration behavior are verified. Retain the
   same budget, concurrency and uncertain-write protections as meal changes.
4. **Menu filtering and sorting.** Local filters for protein, time, tags, price
   and confirmed exclusions would make large menus easier for agents to handle.
   Unknown allergen data must remain unknown.
5. **Provider ratings and global plan changes.** Treat these as separate account
   mutations. Local feedback already supports meal planning; publishing a rating
   or changing future billing/box defaults should not happen implicitly.

Local feedback closes the first general-agent gap. Provider settings readout is
the next useful addition. Delivery moves are useful but not necessary for holiday
skips or ordinary weekly meal selection. There is
no need to add payment, address, checkout or subscription cancellation operations
merely to match a different provider's tool count.
