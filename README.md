# Mindful Chef CLI

An unofficial, local-first Mindful Chef CLI and meal-planning toolkit for agents.
Browse the real menu with images and recipe details, learn from explicit
household feedback, propose balanced meals, and review delivery changes before
they are submitted.

**Status: alpha.** CLI login, meal changes, rollback, skipping and restoring a
delivery have passed an authorised live acceptance run. The complete original
delivery state was restored afterwards. Synthetic tests cover contracts and
safety guards; the unofficial website integration can still change without
notice. This project is not affiliated with or supported by Mindful Chef.

## Install

Requires Python 3.11+ on macOS or Linux and a supported OS credential store.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[mcp,dev]'
source .venv/bin/activate
mindful_chef_cli --help
```

The executable is `mindful_chef_cli`. Installation is local; account data is not
uploaded as part of installation. The `mcp` extra is optional. Linux requires an unlocked
Secret Service/libsecret backend; plaintext and chained keyring backends are
refused. Filesystem state is supported on macOS/Linux, not Windows yet.

## Sign in

```sh
mindful_chef_cli auth login
mindful_chef_cli auth status
mindful_chef_cli auth refresh
mindful_chef_cli auth login --reauth
mindful_chef_cli auth logout
```

Login prompts for an email and a hidden password in a real terminal, reads the
current web login form and CSRF token, authenticates over HTTPS, verifies account
access, then stores session cookies in the OS credential store. Passwords are
never saved. Password arguments, environment variables, piped passwords, cookie
imports and browser-profile scraping are unsupported.

`auth refresh` verifies the current session and saves cookies the website rotates.
There is no verified refresh-token API: if the session expires, log in again.
Logout removes the CLI's local credential-store entry; it does not claim to revoke
the server session or log other browsers out. `--profile NAME` selects another
private local profile. Each account is bound to one writer profile to prevent
uncertain changes being replayed through a different profile.

## Browse meals

```sh
mindful_chef_cli orders list
mindful_chef_cli orders list --past --page 1
mindful_chef_cli menu YYYY-MM-DD
mindful_chef_cli menu YYYY-MM-DD --details
mindful_chef_cli recipe chicken-rendeng
```

Commands emit JSON: `{ "ok": true, "data": ... }`. Failures emit a safe error
code/message and exit nonzero. Orders contain meal/delivery information, not
contact, address or payment details. Hidden forms and CSRF tokens stay internal.
Past orders are paginated; a page is not complete history.

Menu data includes recipe IDs, names, image URLs, portions, prices, food groups,
cooking time and tags when supplied. `--details` also reads ingredients, cooking
steps, nutrition and declared allergens. Detail links retain the relevant menu
availability ID. Missing provider fields remain unknown. Images remain provider
URLs; the tool does not copy or redistribute image files.

If a recipe has no structured detail metadata, detailed menus and recommendations
retain its menu card, mark the missing ingredients/allergens/instructions unknown,
and report `detail_coverage`. Other recipes remain usable. Network, authentication
and rate-limit failures still stop the operation; they are not hidden as missing
recipe data.

## Household preferences

```sh
mindful_chef_cli prefs init
mindful_chef_cli prefs show
mindful_chef_cli prefs set --file /path/to/private-preferences.json
mindful_chef_cli prefs rate RECIPE_ID 5
mindful_chef_cli prefs annotate RECIPE_ID --reheat good --finishing low
mindful_chef_cli recommend YYYY-MM-DD --count 3
```

Preferences are neutral by default. A generic preferences input is in
[`examples/preferences.example.json`](examples/preferences.example.json). Keep
your actual file outside this repository. Supported choices include likes,
dislikes, excluded ingredients, allergens, desired counts by protein, cooking time,
reheating preference and finishing effort.

The planner selects a combination with the desired protein balance and explains
its reasons and uncertainty. Explicit ratings matter more than repeated customer
selections. Provider-curated meals are not treated as favourites; a shared
account cannot reveal which household member chose a meal. Recommendations use
one past-orders page and label that partial coverage. Additional private history
can be supplied in `history.json` as `[{"recipe_id":"101","selected":true}]`.

Reheating quality and finishing effort are household assessments, not verified
provider facts. They are never inferred as facts from a meal title. Review the
cooking steps, then annotate recipes you know. Hard constraints reject unknown
required data. Allergen checks include declared `may_contain` data but are
conservative text matching, not a complete allergen taxonomy or safety guarantee.
Always check the provider's current declarations and delivered ingredients.

## Review account changes

```sh
mindful_chef_cli changes select YYYY-MM-DD ID ID ID --max-total-cents 10000
mindful_chef_cli changes skip YYYY-MM-DD
mindful_chef_cli changes resume YYYY-MM-DD --max-total-cents 10000
mindful_chef_cli changes show PLAN_ID
mindful_chef_cli changes apply PLAN_ID --approve APPROVAL_HASH
mindful_chef_cli changes reconcile PLAN_ID
mindful_chef_cli changes rollback PLAN_ID
```

The first three commands save proposals locally. They do not alter your order.
Review the exact date, meals, portions and price before applying. A hash binds the
reviewed content; it is not authentication or proof of human approval. An agent
must obtain permission for the particular change or follow a separate, narrowly
defined standing permission.

Plans expire after 15 minutes and are bound to the account, current selection,
portion count and preserved form state. Apply reads the live state again, checks
prices and a required spending ceiling, submits once, then verifies readback.
The estimate uses undiscounted recipe prices plus delivery. Orders with extras
or pending product cancellations are refused, avoiding unintended changes to
recurring products. Global subscription, payment, address, cancellation and
checkout operations are intentionally absent.

An uncertain result places the delivery on hold. Neither the same plan nor a
new plan can replay that write. Reconciliation is read-only; if the exact requested
state is observed, it releases the hold, recording that state without claiming
which actor caused it. Otherwise it remains held for manual investigation.

Rollback creates a new proposal for the original meals and requires another
review. It refuses to overwrite a later edit. Availability and deadlines may
prevent restoration. Skip/resume cannot be assumed reversible: resume is enabled
only when the current site emits a recognised restore form **and** exposes the
restored meals and price for review. Otherwise it fails closed and directs you to
the website. The current Restore confirmation workflow has passed live
skip/resume acceptance with exact before/after checks.

There is no verified server-side optimistic-lock guarantee. Someone could edit
after the final read and before submission; the client minimises that window and
verifies afterwards, but cannot make website writes transactional.

## Holiday planning

```sh
mindful_chef_cli away --deliveries /path/to/deliveries.json --events /path/to/away-events.json
```

See the synthetic files in [`examples/`](examples/). Events use start-inclusive,
end-exclusive dates and explicit `away` and `confidence` fields. Confirmed absence
covering the whole consumption window proposes a skip. Partial or uncertain trips
request review. This operation never reads a calendar or changes an order.

The [agent skill](skills/mindful-chef-cli/SKILL.md) explains how to use an independently
authorised calendar connector, pass minimal date intervals, and obtain approval.
Calendar titles, attendees and locations need not enter this tool. There is no
default holiday or meal-selection automation and no new standing purchase authority.

## Agent and ChatGPT integration

The repository includes an agent skill and optional MCP server:

```sh
mindful_chef_cli mcp
mindful_chef_cli mcp --allow-writes
mindful_chef_cli setup --client codex --format command
mindful_chef_cli setup --client claude-code --format command
mindful_chef_cli setup --client claude-desktop --format config
```

The MCP adapter uses **stdio only**, opens no network listener, and does not
expose login, passwords, tokens, arbitrary files or raw HTTP. Reads, proposals
and confirmed local household feedback tools are available by default; the account apply tool requires `--allow-writes` and the
exact plan review guard. Configure the executable as a local MCP server in a
compatible agent client. `setup` prints the exact settings for this installation
without reading credentials or changing configuration. Maintainer instructions
are in [`AGENTS.md`](AGENTS.md) and [`CLAUDE.md`](CLAUDE.md); the reusable skill
and client templates are covered in [`docs/AGENTS.md`](docs/AGENTS.md).

This does not itself install a ChatGPT connector or make the service reachable
from a cloud chat. Remote access needs a separately designed, authenticated
connection or supported secure tunnel. No shared public account proxy is included.
OpenAI's [MCP connection guide](https://developers.openai.com/api/docs/guides/tools-connectors-mcp)
describes its current remote and private-server connection options.

## Keeping it working

```sh
mindful_chef_cli doctor
mindful_chef_cli doctor --authenticated
python scripts/healthcheck.py
python scripts/check_public_tree.py
python -m unittest discover -s tests -q
ruff check src tests scripts
```

`doctor` performs safe GETs and checks contracts, not just HTTP 200 responses.
The authenticated mode verifies orders and an editable cart when available.
Health checks never submit orders, skip/resume deliveries, log in automatically,
or probe write routes. A healthy read check does not prove mutation endpoints work.

The daily runner records a small private health fingerprint, reports changed
failures/recovery, and checks an existing CLI session when available. Its saved
prompt template is in [`docs/MAINTENANCE.md`](docs/MAINTENANCE.md). A GitHub Actions
workflow checks synthetic tests, lint, privacy and dependencies without account
secrets. Live account checks stay local and are never run in hosted CI.

See the source-backed [HelloFresh capability comparison](docs/CAPABILITY_COMPARISON.md)
for implemented differences and remaining opportunities, including provider
subscription readout, delivery-date moves and menu filtering.

## Privacy and release

Sessions live in the OS credential store. Preferences, ratings, annotations,
plans, receipts and health state live under `$XDG_STATE_HOME/mindful_chef_cli` or
`~/.local/state/mindful_chef_cli`, with private directory/file permissions. They are not
package data. There is no telemetry. Account outputs are private and must not be
committed, posted in issues, or attached to CI.

Before publishing, run the privacy check, inspect the actual Git diff and package
contents, and complete an interactive acceptance run. A scanner is a backstop,
not proof that arbitrary files are safe to publish. See
[`docs/SECURITY.md`](docs/SECURITY.md),
[`docs/API_PUBLIC_RESEARCH.md`](docs/API_PUBLIC_RESEARCH.md), and
[`docs/ACCEPTANCE.md`](docs/ACCEPTANCE.md).

MIT licensed. Mindful Chef names and recipes belong to their respective owners.
