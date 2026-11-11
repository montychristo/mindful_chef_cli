# Daily maintenance

The upstream GitHub workflow runs at 06:43 Europe/London and commits the
[public maintenance record](HEALTH.md) to `main` once per London calendar day.
It checks the anonymous login form, menu and a sample recipe. It runs the offline
tests, Ruff, the dependency audit and the public-tree privacy scan. The record
shows failures and unavailable checks, including a dependency installation
failure. A second run on the same date leaves the record in place.

The workflow also checks official terms and Help Centre articles on signing in,
recipe selection, portions, delivery deadlines, skips and past orders. It
compares relevant page content with the last successful check. A changed source
is a prompt to inspect what changed and whether it affects the CLI or skill.
Source fingerprints and status labels appear in the record. The record's Git
history preserves previous days.

The check job runs with read permissions. A separate publishing job stages only
the generated record. A concurrent push can cause publication to fail. In
that case, run the workflow again on the current `main`. The workflow can also
be run manually from GitHub Actions when a scheduled run is delayed or missed.

## Local signed-in checks

Run the checker with the same installed environment and profile used by the CLI:

```sh
.venv/bin/python scripts/healthcheck.py --profile default
```

It checks the public login form, menu and one recipe. If an OS-keyring session
exists, it also checks authenticated orders and an editable delivery's cart
contract. These are reads. It never logs in, changes meals, skips or resumes a
delivery, applies a plan, places an order, or tests a mutation. Read checks cannot
prove that a future write will succeed.

`--public-only` avoids opening the credential store and skips account endpoints.
A missing session is an expected setup state: `requires_login` is true,
`authenticated_checked` is false, and a healthy public check exits with code 0.
An expired session or a real endpoint/schema/storage failure exits with code 1.
An account with no editable delivery has incomplete cart-contract coverage; the
checker reports the source result without inventing success.

Output is a JSON envelope with `healthy`, `requires_login`, `changed`, `public`
and `authenticated` results. `changed` compares statuses, check names, booleans
and safe error codes. Timestamps, menu counts and provider response bodies do not
affect it. A repeated unchanged failure stays `changed: false`.

The checker saves a small `health-last.json` receipt through `SecureStore`, in
the profile's owner-only private state directory. It contains sanitized health
facts and a fingerprint. It never contains credentials, cookies, meal choices,
calendar details or provider response bodies. An invalid receipt is reported
and left intact for review. A blocked credential store is a coverage failure,
not evidence that the public endpoints failed.

## Reviewing changes and failures

The local maintenance task checks the hosted record, then reviews relevant
website, Help Centre or Journal announcements and changed health results. A
source change needs verification against the current CLI or skill before it
justifies a repair. Recipe rotations and promotions are routine website changes.

Use a separate worktree for targeted fixes, dependency updates and project
guidance changes. Run the relevant tests and the privacy scan before publishing
a repair. Credentials and signed-in health results stay in local storage.
Authentication renewal requires the user to log in interactively. Investigate
provider write behavior with read-only evidence and synthetic fixtures.
