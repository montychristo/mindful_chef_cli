# Read-only maintenance

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

## Standalone daily task prompt

Save the following prompt in an independent daily task, with the repository
checkout as its working directory and its installed virtual environment. Use
the intended local profile. Keep the task independent of its setup chat.

> Run the Mindful Chef CLI read-only maintenance check using the installed environment:
> `.venv/bin/python scripts/healthcheck.py --profile default`.
> Read its JSON result. Do not log in, ask for passwords, attempt authentication
> repair, upgrade dependencies, edit the repository, apply plans, change meals,
> skip/resume deliveries, or run any account mutation. Do not publish files or
> send messages to other people. The checker owns its private health receipt.
> If `changed` is false, remain quiet. If health changed to a failure, report the
> failing check names and safe codes with a concrete proposed next step. If it
> recovered, report recovery once. If a missing/expired login is newly detected,
> explain that interactive local `mindful_chef_cli auth login` is needed; do not
> attempt it. Report missing execution or storage access as a coverage gap.
> Keep all account data and credentials out of task output and repository files.

Choose a daily time appropriate to the machine's timezone and availability.
Verify that the saved task is standalone and has no target chat dependency.
Do not create a second active schedule for the same profile. Local machine
schedulers can run the same command directly; use an absolute interpreter and
checkout path in that machine's scheduler configuration. This repository does
not install a scheduler or create a cloud task automatically.

## Reviewing a failure

Re-run the single relevant read check manually when useful. Authentication
renewal remains an interactive user action. A changed upstream contract needs
source review, synthetic regression fixtures and the normal tests before a fix
is adopted. Never keep trying mutations to diagnose a read failure or replay an
uncertain write. A routine checker is not permission to change account state.
