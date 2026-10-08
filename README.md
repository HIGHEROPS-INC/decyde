# decyde

Your agents ask. You decide.

When you run several coding agents at once (Claude Code, Codex, Grok, Gemini,
Antigravity), their questions scroll past in terminals you are not watching.
decyde gives them one place to ask: a local inbox you keep open in a browser
tab or a terminal pane. You answer there, and the answer goes back to the agent
that asked.

```sh
curl -fsSL https://decyde.dev/install | sh
```

Requires Python 3.11+ (standard library only, nothing else to install) on
macOS or Linux. Run the same command again to upgrade.

## What setup does

`decyde setup` (run by the installer) is safe to re-run and fully reversed by
`decyde uninstall`:

- Creates a SQLite database in `~/.decyde`.
- Installs a background server (launchd on macOS, systemd user service on
  Linux) serving the web UI at http://127.0.0.1:7717. It binds to localhost only.
- Adds the decyde protocol to each agent it finds, between
  `<!-- decyde:begin -->` markers: `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`,
  `~/.grok/AGENTS.md`, `~/.gemini/GEMINI.md`.
- Adds three Claude Code hooks to `~/.claude/settings.json` (backed up first).
- If [Herdr](https://herdr.dev) is installed, adds a sidebar row to
  `~/.config/herdr/config.toml` (between `# >>> decyde` markers) so agents
  waiting on you are marked there. If you already customise the sidebar rows,
  setup leaves them alone and prints the one token to add.

Skip any part with `--no-service`, `--no-agents`, `--no-hooks` or `--no-herdr`.

## Answering

- **Browser:** http://127.0.0.1:7717 (or `decyde open`). Live updates, a chime
  and a desktop notification when a question arrives.
- **Terminal:** `decyde`. Mouse and keyboard; scroll through questions, click an
  option to answer with it, skip the ones you are not ready for. Drag across any
  text to copy it (decyde does its own selection, since mouse mode takes over the
  terminal's).

## Herdr sidebar

While an agent has an open question, its row in Herdr's sidebar shows it in
purple (`? #12 Keep webhooks in Edge?`) and its Space shows a count
(`? 2 waiting on you`). Marks clear as soon as you answer, and expire on their
own if the decyde server stops.

## How answers reach the agent

Every agent can poll with `decyde check <id>` or `decyde wait <id>`. On top of
that, decyde pushes the answer to an agent that has gone quiet:

| Where the agent runs | Delivery |
|---|---|
| [Herdr](https://herdr.dev) pane | Typed in as a prompt once the agent is idle |
| tmux pane | Pasted in once the pane is still and no approval dialog is showing |
| Claude Code, any terminal | Hooks: when the session tries to stop with an open question it waits for the answer (10 min default, `decyde config claude_stop_wait_minutes`), and a later answer arrives with your next message |
| Anything else | Polling |

Pushed answers start with `[decyde]` and tell the agent to run `decyde ack <id>`.
Both UIs show whether each answer was delivered and acknowledged.

## For agents

```sh
decyde ask --agent codex --name "Codex, billing lane" --task "Stripe webhook refactor" \
  --title "Keep webhooks in Edge Functions?" \
  --question "Move the Stripe webhooks to a Next.js route or keep them in Supabase Edge Functions?" \
  --option "Keep in Edge Functions" --option "Move to Next.js route" \
  --recommend "Keep: no cold-start difference and one less migration" --urgency high
decyde check 12      # exit 0 answered, 2 still open, 3 dismissed
decyde wait 12       # block until answered (default 10 min)
decyde ack 12        # confirm receipt
decyde cancel 12     # withdraw
```

The project, git branch, and Herdr or tmux pane are recorded automatically.
The full protocol agents follow is in [`decyde/protocol.py`](decyde/protocol.py).

## Commands

| Command | |
|---|---|
| `decyde` | Terminal UI (`--no-splash` skips the intro) |
| `decyde open` | Web UI in your browser |
| `decyde status` | What is installed and running |
| `decyde config [key] [value]` | `name`, `claude_stop_wait_minutes`, `splash` |
| `decyde setup` / `decyde uninstall [--purge]` | Install or remove |

Environment: `DECYDE_HOME` (default `~/.decyde`), `DECYDE_PORT` (default 7717).

## License

MIT
