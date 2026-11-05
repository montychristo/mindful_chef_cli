# Working on Mindful Chef CLI

This repository contains an unofficial Python client, JSON CLI, local stdio MCP
server and reusable meal-planning skill. Keep provider facts separate from local
household preferences and inferred cooking assessments.

## Develop and verify

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[mcp,dev]'
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/ruff check src tests scripts
.venv/bin/python scripts/check_public_tree.py
```

Use synthetic fixtures. Tests must verify behavior such as form contracts,
budgets, stale-state detection, session expiry and uncertain-write handling.
Inspect current owning code before changing a contract. Public-site changes can
invalidate an adapter even when the offline tests pass.

## Account and credential boundaries

- Login belongs in the user's terminal through `mindful_chef_cli auth login`.
  Never request credentials in chat, accept password flags, import browser
  cookies, log raw responses or add plaintext credential storage.
- Keep requests on the fixed Mindful Chef HTTPS origin with certificate
  validation. Preserve the OS keyring, owner-only state, profile locks, account
  writer binding, spending ceilings and single-attempt rules.
- Reads, local proposals and account mutations are different operations. A meal
  change or skip/resume needs exact user authorization or a narrowly scoped
  standing policy. An approval hash binds reviewed content; it does not prove
  human approval.
- Do not replay an uncertain write, switch profiles to bypass a hold or assume
  rollback is always possible. Reconcile current state first. Preserve unrelated
  products, delivery settings and other household members' edits.
- Health checks use reads and offline checks only. Do not submit a live change to
  test a daily checker. A successful GET does not establish write support.

## Privacy and delivery

Keep account details, calendars, preferences, meal history, sessions, plans,
receipts, screenshots and captures outside the repository. Do not include live
data in tests, issues, logs or CI. Scan and inspect the exact source and package
contents before release; the scanner is a backstop, not proof of privacy.

The default MCP server uses stdio and exposes reads and local proposals. Account
apply requires explicit `--allow-writes` startup and the reviewed plan guard.
Do not add a network listener or public account proxy as a convenience fallback.

For agent installation, see [docs/AGENTS.md](docs/AGENTS.md). For the consumer
workflow, use [skills/mindful-chef-cli/SKILL.md](skills/mindful-chef-cli/SKILL.md).
See [docs/SECURITY.md](docs/SECURITY.md) and
[docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) for security and live-verification limits.
Repository instructions do not grant access to anyone's accounts or permission
to publish, change visibility, book, pay or send messages.
