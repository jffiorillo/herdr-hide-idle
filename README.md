# Hide Idle Agents for Herdr

A [Herdr](https://herdr.dev) plugin that keeps a busy Agents sidebar readable without closing anything:

- **Hides idle agents** once they have been idle or done for longer than a TTL (5 minutes by default). They come back the moment they start working or get blocked.
- **Toggles** hiding on and off with one key.
- **Pins** agents that should always be visible, and puts chosen agents at the top.
- **Groups** agents in a stable order, so rows don't jump around when you click them or when states change.
- **Short names**: reports a clean name for every agent that you can show in the sidebar, with optional icons, prefix stripping and abbreviations.

The naming and grouping rules understand [firstmate](https://github.com/kunchenguid/firstmate)'s layout, where each worker gets a `└ <task> · p:<id>` workspace placed right after the agent that launched it. Workers are indented under their Director or 2ndmate. Without firstmate, agents simply sort by workspace and tab order.

```
✓ Director · claude
  fish · claude
◐ └ api-refactor · claude
⚑ backend · pi
× └ backend-migration · pi
⚑ frontend · pi
```

## Requirements

- Herdr 0.9.5 or newer
- `python3` 3.9 or newer on your `PATH` (macOS ships one). No other dependencies.
- macOS or Linux

## Install

```bash
herdr plugin install jffiorillo/herdr-hide-idle
```

Bind a key to the toggle in `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+i"
type = "plugin_action"
command = "jffiorillo.hide-idle.toggle"
description = "toggle idle agents"
```

Reload with `prefix+shift+r` or `herdr server reload-config`, then press `prefix+i`. The plugin also provides `hide` and `show` actions:

```bash
herdr plugin action invoke jffiorillo.hide-idle.hide
herdr plugin action invoke jffiorillo.hide-idle.show
```

## Show the short names

The plugin reports each agent's name as the `$short_name` pane token. To use it, set your sidebar rows, for example one line per agent with 2ndmates and the Director highlighted:

```toml
[ui.sidebar.agents]
rows = [["state_icon", { token = "$short_name", bold = true, rules = [{ starts_with = "⚑", fg = "#89b4fa" }, { equals = "Director", fg = "#89b4fa" }] }, { token = "agent", dim = true }]]
```

`status_indicators = "symbols"` under `[ui]` gives each state its own shape (× blocked, ◐ working, ✓ done, ○ idle), which helps when rows are no longer sorted by attention.

## Configure

Settings live in the plugin's config directory (`herdr plugin config-dir jffiorillo.hide-idle`), in `config.toml`. The file is created with defaults on first run and re-read on every poll, so edits apply within `poll_seconds`.

```toml
ttl_seconds = 300            # how long an agent may sit idle/done before hiding; 0 hides at once
poll_seconds = 10            # how often the watcher re-checks agents

# Always shown, matched by label with * wildcards. Keep each list on one line.
pin_workspaces = ["2ndmate-*"]
pin_tabs = ["Director"]
pin_panes = []

# Sorted first, matched the same way.
top_workspaces = []
top_tabs = ["Director"]
top_panes = []

name_icons = ["2ndmate-*=⚑"]  # "glob=symbol" put in front of matching short names
strip_prefixes = ["2ndmate-"] # removed from short names after icons match
name_aliases = []             # "word=short" abbreviations for firstmate labels
```

Agents in the tab you are looking at are always shown.

## How it works

The plugin uses Herdr's `agent.view.set` socket API, which filters and sorts the built-in Agents sidebar. Herdr's filter can't express "idle for 5 minutes", so a small watcher process polls `agent.list`, records when each agent started waiting, and adds those past the TTL to the view's hide list. A hidden agent reappears immediately when it changes state, because the hide list matches its exact state change.

The watcher starts from the plugin's startup hook (or from the first toggle, if the plugin was installed while Herdr was running) and exits when its Herdr server stops or restarts. It only talks to the local Herdr socket. Its state lives in the plugin's state directory.

## License

MIT
