# Changelog

All notable changes to decyde. Versions follow [semantic versioning](https://semver.org);
each one is a [GitHub release](https://github.com/HIGHEROPS-INC/decyde/releases), and the
installer and `decyde update` install the latest release.

## [0.3.1] - 2026-10-09

### Fixed
- Saying "decyde on" or "decyde off" mid-sentence no longer flips the session. "Is the
  version of decyde on this mac up to date?" used to turn decyde on. Toggles now count
  only as instructions: outside questions, with "decyde on" ending the phrase.

### Changed
- The installer and `decyde update` install the latest GitHub release instead of the
  tip of `main`, and the daily update check reads the latest release.

## [0.3.0] - 2026-10-09

### Added
- `decyde update` installs the latest version and re-runs setup, refreshing agent
  instructions and hooks.
- A daily update check. The web UI, the TUI header and `decyde status` say when a newer
  version is out. Turn it off with `decyde config update_check off`.
- Landing page sections on turning decyde on and on what counts as an ask.

### Changed
- The local web UI uses the system monospace font instead of loading Google Fonts, so
  the update check is decyde's only outbound request.

## [0.2.0] - 2026-10-08

### Added
- Herdr sidebar marks: an agent with an open question shows `? #12 <title>` under its
  row, and its Space shows a count. Marks clear on answer and expire if the server stops.
- decyde is off by default per agent session. "use decyde", `decyde on` or `/decyde on`
  turns it on; "decyde off" turns it off.
- A broad definition of an ask in the agent protocol: decisions, confirmations the agent
  will not proceed without, steps only you can take, and end-of-turn hand-offs.
- Hooks for Codex (`~/.codex/hooks.json`) and Grok (`~/.grok/hooks/decyde.json`)
  alongside Claude Code. A Stop check sends a session with decyde on back once when it
  ends its turn on an ask it never posted.

### Fixed
- Codex 0.161 runs commands from a background server outside the multiplexer, so pane
  variables never reached `decyde ask`. The asker's Herdr or tmux pane is now looked up,
  and only a certain match is used.
- `decyde setup` waits for launchd to release the old server before starting the new one.

### Changed
- Requires Python 3.11 or newer.

## [0.1.0] - 2026-10-07

### Added
- `decyde ask`, `check`, `wait`, `ack` and `cancel` for agents, backed by one SQLite file.
- A web UI at http://127.0.0.1:7717 and a terminal UI with mouse support, drag-to-copy,
  and a launch animation that settles into the header.
- Answer delivery through Herdr panes, tmux panes and Claude Code hooks, sent as soon as
  you answer, with polling as the fallback. A Claude session that ends its turn with an
  open question waits for the answer (up to 24 hours by default).
- `decyde setup` and `decyde uninstall`: background service (launchd or systemd), agent
  instructions for Claude Code, Codex, Grok and Gemini/Antigravity, and Claude Code hooks.
- The curl installer at https://decyde.dev/install.

[0.3.1]: https://github.com/HIGHEROPS-INC/decyde/releases/tag/v0.3.1
[0.3.0]: https://github.com/HIGHEROPS-INC/decyde/releases/tag/v0.3.0
[0.2.0]: https://github.com/HIGHEROPS-INC/decyde/releases/tag/v0.2.0
[0.1.0]: https://github.com/HIGHEROPS-INC/decyde/releases/tag/v0.1.0
