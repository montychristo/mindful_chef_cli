# Contributing

Use Python 3.11+ and synthetic data. Install `.[mcp,dev]`, then run the unittest
suite, Ruff and `scripts/check_public_tree.py`. Add a meaningful contract test for
changes to login, form parsing, budgets, state drift or write handling.

Provider contracts are unofficial. Investigate a failure using authorised source
access and document which behavior was actually verified. Never guess mutation
routes, relax a guard simply to get a request through, or retry uncertain writes.
Daily checks must remain read-only.

Do not submit real names, addresses, account email addresses, order identifiers,
preferences, calendars, cookies, HAR files, raw authenticated HTML or screenshots
in changes or issues. Reports should contain safe error codes, the tool version,
which contract failed, and a minimal synthetic reproduction.

Before release, review the source diff, Git history, package contents, dependency
audit and acceptance checklist. Publishing or changing repository visibility
requires the owner's explicit authorisation.
