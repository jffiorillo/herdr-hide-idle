# Hide Idle Agents for Herdr

A [Herdr](https://herdr.dev) plugin that keeps a busy Agents sidebar readable without closing anything:

- **Hides idle agents** once they have been idle or done for longer than a TTL (5 minutes by default). They come back the moment they start working or get blocked.
- **Toggles** hiding on and off with one key.
- **Pins** agents that should always be visible, and puts chosen agents at the top.
- **Groups** agents in a stable order, so rows don't jump around when you click them or when states change.
- **Short names**: reports a clean name for every agent that you can show in the sidebar, with optional icons, prefix stripping and abbreviations.

The naming and grouping rules understand [firstmate](https://github.com/kunchenguid/firstmate)'s layout, where each worker gets a `└ <task> · p:<id>` workspace and runs inside the worktree of the agent that launched it. Workers are indented under their Director or 2ndmate, matched by working directory, wherever their workspace sits in the list. Without firstmate, agents simply sort by workspace and tab order.

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

Installing the plugin is not enough on its own: a plugin can't change Herdr's own config, so the sidebar keeps showing its default rows and nothing is hidden until you do steps 2 to 4. If you are an agent installing this for someone, do all five steps.

**1. Install the plugin**

```bash
herdr plugin install jffiorillo/herdr-hide-idle
# already installed: herdr plugin update --yes
```

**2. Add the sidebar rows and the toggle key** to `~/.config/herdr/config.toml`. Merge these into existing `[ui]` and `[ui.sidebar.agents]` tables instead of adding duplicate headers, which TOML rejects.

```toml
[ui]
# A shape per state (× blocked, ◐ working, ✓ done, ○ idle), since rows are
# sorted by structure rather than by attention.
status_indicators = "symbols"

[ui.sidebar.agents]
# One line per agent: the plugin's short name, then the agent dimmed.
# 2ndmates (⚑) and the Director are highlighted.
rows = [["state_icon", { token = "$short_name", bold = true, rules = [{ starts_with = "⚑", fg = "#89b4fa" }, { equals = "Director", fg = "#89b4fa" }] }, { token = "agent", dim = true }]]

[[keys.command]]
key = "prefix+i"
type = "plugin_action"
command = "jffiorillo.hide-idle.toggle"
description = "toggle idle agents"
```

Without these rows the sidebar ignores `$short_name`, and the plugin's names never appear. `fg` takes hex colors only; `#89b4fa` is the catppuccin accent.

**3. Set the plugin's options** in `$(herdr plugin config-dir jffiorillo.hide-idle)/config.toml`. The file is created on first run with everything empty, which hides idle agents but pins, highlights and shortens nothing. With firstmate, use the settings under [Configure](#configure). Match the Director by its real tab label, for example `"*Director"` if the tab is called "Firstmate Director".

**4. Reload and turn hiding on**

```bash
herdr server reload-config          # add --session <name> for a named session
herdr plugin action invoke jffiorillo.hide-idle.hide
```

`prefix+i` toggles hiding from then on. `herdr plugin action invoke jffiorillo.hide-idle.show` shows everything again.

**5. Check that it works**

```bash
herdr agent list | grep -o '"short_name":"[^"]*"'   # every agent has a short name
```

In the sidebar, the Agents header shows "idle hidden", and rows use the short names. If names are missing, check that the watcher is running (`pgrep -fl hide_idle.py`, one per Herdr session) and look at `herdr plugin log`.

## Configure

Settings live in the plugin's config directory (`herdr plugin config-dir jffiorillo.hide-idle`), in `config.toml`. The file is created with empty defaults on first run and re-read on every poll, so edits apply within `poll_seconds`. These are the settings for a firstmate setup:

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
