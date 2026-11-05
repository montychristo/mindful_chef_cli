# Connect your agent

Every user installs the tool locally and signs in to their own Mindful Chef
account. The agent never needs the password or session cookie in its settings.
The setup generator prints configuration; it does not edit your agent or start it.

## Install and sign in

```sh
git clone https://github.com/montychristo/mindful_chef_cli.git
cd mindful_chef_cli
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[mcp]'
.venv/bin/mindful_chef_cli auth login
.venv/bin/mindful_chef_cli auth status
```

Cloning a private repository requires your own GitHub access. Local state and
credentials stay outside the checkout. A different household/account can use
`--profile NAME`; each account is bound to one writer profile.

## Generate settings for this installation

```sh
.venv/bin/mindful_chef_cli setup --client codex --format command
.venv/bin/mindful_chef_cli setup --client codex --format config
.venv/bin/mindful_chef_cli setup --client claude-code --format command
.venv/bin/mindful_chef_cli setup --client claude-desktop --format config
.venv/bin/mindful_chef_cli setup --client generic --format config
```

The repository wrapper is equivalent:

```sh
.venv/bin/python scripts/agent_setup.py --client codex --format config
```

The generator uses the current Python environment with `-m mindful_chef_cli` and
prints an absolute launch path. That avoids dependence on your shell's activated
environment. It reads no account state and writes no settings. Review and run the
printed registration command or merge the printed settings yourself. Treat the
generated path as machine-local; do not commit your generated configuration.

For a named profile use `mindful_chef_cli --profile household setup --client codex`.
The repository wrapper accepts `--profile household` directly. Default settings expose reads and
local proposals, with no account apply tool. Add `--allow-writes` only when you
want account apply available; exact user authorization and the plan guard still
apply to each change.

Checked-in, machine-neutral templates are in [integrations/](../integrations/):

- [Codex TOML](../integrations/codex.toml.example)
- [Claude Code JSON](../integrations/claude-code.json.example)
- [Claude Desktop JSON](../integrations/claude-desktop.json.example)
- [Generic MCP JSON](../integrations/mcp.json.example)

Codex uses `mcp_servers` in TOML and supports registration with `codex mcp add`.
See [official Codex MCP setup](https://developers.openai.com/codex/mcp).
Claude Code supports a local stdio server through `claude mcp add --transport
stdio`; see its [official MCP guide](https://code.claude.com/docs/en/mcp).
These are setup instructions only; this repository does not operate an agent
application or change its configuration automatically.

## Install the reusable skill

Copy the entire `skills/mindful-chef-cli` directory into the skill directory your
agent supports. For example, Codex can use a project-local
`.agents/skills/mindful-chef-cli` folder, while Claude Code can use
`.claude/skills/mindful-chef-cli`. Follow your client's current skill-discovery
instructions and keep the `SKILL.md` file with its supporting files. See the
[Codex skill locations](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills)
and [Claude Code skill locations](https://code.claude.com/docs/en/skills#choose-where-skills-load).

The generator's JSON output includes `skill_source` for either this checkout or
the installed package's shared data. The skill can also be used with CLI access
without MCP. Root `AGENTS.md` guides maintainers; `CLAUDE.md` imports it so both
coding agents share one maintained source.

## Verify in the agent

Restart or reload the client's MCP configuration if required. Confirm that it
discovers the server's tools, then request `auth_status`, `orders_list`, `menu`
and `recipe`. Review a proposal and verify that no account change occurs before
apply. Never test connectivity by submitting an order or skip.

The server provides meal and delivery reads, preferences, recommendations,
holiday proposals, diagnostics, reviewed selection/skip/resume plans, rollback
proposals and reconciliation. Current capabilities are described by the actual
MCP tool listing and CLI help. Login and raw credentials are never MCP tools.
`preferences_update`, `recipe_rate` and `recipe_assess` persist local household feedback;
they do not submit ratings or preferences to the provider. Use them only for the
user's stated or approved feedback. Their availability does not enable account
apply, which remains behind `--allow-writes` and the exact plan review workflow.

## Cloud chats

Stdio starts a local process; it is not a remotely reachable URL. This setup does
not automatically create a cloud ChatGPT or Claude connector. A remote deployment
would need individual authentication, isolated account state, protected
transport, revocation and an approval experience. Do not expose the local server
or forward a credential store to a shared public proxy. See OpenAI's
[current MCP connection options](https://developers.openai.com/api/docs/guides/tools-connectors-mcp).
