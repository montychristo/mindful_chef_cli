# Acceptance and release checks

## Verified on 5 November 2026

An authorised account owner completed interactive terminal login. Authenticated
CLI status and diagnostic reads then passed using only the stored OS-keyring
session. No password or browser session was exported to this repository.

The live CLI acceptance sequence used a private baseline of an editable future
delivery. It applied one same-price meal substitution, independently read the
result, restored the original meals, then skipped and restored the delivery.
Final checks confirmed the complete delivery state, exact recipe display order,
portions, delivery settings, price and all upcoming orders matched the original
baseline, with no outstanding uncertain-write holds.

Live testing found and fixed two contract issues: recipe order must be preserved
in snapshots and rollback, and the current storefront restores through an emitted
`restores/new` confirmation page with a CSRF-protected POST to `restores`.
Synthetic regression tests cover both.

Detailed-menu acceptance also showed that signed-in recipe rendering omits
structured metadata that the same public URL supplies anonymously. Public recipe
reads use an isolated cookie jar, separate from login and account changes. If a
page genuinely lacks details, the client retains its card, leaves unavailable
facts unknown, and reports partial coverage instead of aborting the usable menu.

Private baselines, plan IDs, execution receipts, account identifiers and meal
history remain outside the checkout. Only generic test outcomes belong here.
Generated agent commands are exercised using real MCP initialization and tool
listing, with account apply absent unless explicitly enabled.

## Limits

These are point-in-time results for one account on macOS, not a guarantee for
every account or a future site version. The package has macOS/Linux filesystem
support. Hosted CI uses synthetic fixtures; no account credentials or live
mutations belong in it. Locked-keychain and deliberately expired-session paths
are tested synthetically, not by altering a real account's security settings.

There is no verified server-side optimistic lock. Fresh-state checks reduce a
concurrent-edit window but cannot make website writes transactional. Provider
audit or survey history is not erased by restoring the delivery's visible state.

## Repeating live acceptance

1. Obtain exact authorisation for a reversible test and restoration. Authenticate
   interactively, verify the account and save a private baseline outside the repo.
2. Use a delivery comfortably before its cutoff, with original recipes available
   and a verified restoration path. Keep the original price ceiling and portions.
3. Apply one reviewed change once, verify it, and restore through a fresh reviewed
   proposal. A timeout or uncertain result requires reconciliation, never replay.
4. Test skip/restore only when exact restored contents and price can be checked.
   Verify every original order and field afterwards, including recipe ordering.
5. Keep all live account data out of source, logs, examples, packages and CI.

## Publication gate

Run `scripts/check_public_tree.py`, all tests, Ruff, dependency audit and package
build. Inspect the staged diff, wheel and sdist contents. Confirm no account
output, private paths, household preferences, session material, raw authenticated
HTML, HAR files, screenshots or local automation identifiers are included.
Inspect Git history as well as the working tree. Publishing or changing repository
visibility requires the owner's separate authorisation.
