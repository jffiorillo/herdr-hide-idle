#!/usr/bin/env python3
"""Hide agents that have been idle or done for longer than a TTL from Herdr's
Agents sidebar, via agent.view.set.

Usage: hide_idle.py toggle|hide|show|watch

Config (HERDR_PLUGIN_CONFIG_DIR/config.toml, re-read on every poll):
    ttl_seconds = 300   # how long an agent may sit idle/done before hiding; 0 hides at once
    poll_seconds = 10   # how often the watcher re-checks agents
    pin_workspaces = ["2ndmate-*"]  # agents always shown, by workspace/tab/pane label glob
    pin_tabs = ["Director"]
    pin_panes = []
    top_workspaces = []  # agents sorted to the top of the list, same matching
    top_tabs = ["Director"]
    top_panes = []
"""

import fcntl
import fnmatch
import hashlib
import json
import os
import re
import socket
import sys
import time

SOURCE = "plugin:" + os.environ.get("HERDR_PLUGIN_ID", "jffiorillo.hide-idle")
SOCKET_PATH = os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser("~/.config/herdr/herdr.sock")
WAITING = ["idle", "done"]  # done = idle but not yet seen
DEFAULTS = {
    "ttl_seconds": 300,
    "poll_seconds": 10,
    "pin_workspaces": [],
    "pin_tabs": [],
    "pin_panes": [],
    "top_workspaces": [],
    "top_tabs": [],
    "top_panes": [],
    "name_icons": [],
    "strip_prefixes": [],
    "name_aliases": [],
}
# Pane metadata token marking agents that sort first; views can only sort on
# built-in fields or tokens, so labels are mapped onto this token.
TOP_TOKEN = "hide_idle_top"
# Pane metadata token with a short, readable agent name; show it in the sidebar
# with `$short_name` in ui.sidebar.agents.rows.
NAME_TOKEN = "short_name"
# Pane metadata token ranking a workspace's own agent ("0") before the mates
# nested in it ("1").
RANK_TOKEN = "hide_idle_rank"
# Structural order only, so rows never move when an agent is clicked (seen) or
# changes state. Firstmate places each mate's workspace right after the
# workspace of the agent that launched it, so workspace order groups mates
# under their parent.
SORT = [
    {"field": {"token": TOP_TOKEN}, "order": "asc"},
    {"field": "workspace_order", "order": "asc"},
    {"field": {"token": RANK_TOKEN}, "order": "asc"},
    {"field": "tab_order", "order": "asc"},
    {"field": "pane_order", "order": "asc"},
]
DEFAULT_CONFIG = """\
# Hide Idle Agents plugin settings. Changes apply on the next poll.

# Seconds an agent may sit idle or done before it is hidden. 0 hides at once.
ttl_seconds = 300

# Seconds between watcher checks; hiding can lag the TTL by up to this much.
poll_seconds = 10

# Agents that are always shown, matched by label with * wildcards.
# Keep each list on one line.
pin_workspaces = []
pin_tabs = []
pin_panes = []

# Agents sorted to the top of the list, matched the same way. Applies whether
# idle agents are hidden or shown.
top_workspaces = []
top_tabs = []
top_panes = []

# Symbols put in front of matching short names, as "glob=symbol"; first match
# wins. Prefer single-width symbols; emoji take two columns in most terminals.
name_icons = []

# Prefixes removed from short names, after name_icons has matched them.
strip_prefixes = []

# Abbreviations for words in firstmate labels, as "word=short", e.g. "project=pj".
name_aliases = []
"""


def plugin_dir(env, fallback):
    path = os.environ.get(env) or os.path.expanduser(fallback)
    os.makedirs(path, exist_ok=True)
    return path


STATE_DIR = plugin_dir("HERDR_PLUGIN_STATE_DIR", "~/.local/state/herdr-hide-idle")
CONFIG_DIR = plugin_dir("HERDR_PLUGIN_CONFIG_DIR", "~/.config/herdr-hide-idle")
# Tracking is per server, since pane ids and sequence numbers are per server.
SERVER_KEY = hashlib.sha1(SOCKET_PATH.encode()).hexdigest()[:12]
TRACK_PATH = os.path.join(STATE_DIR, f"tracked-{SERVER_KEY}.json")
LOCK_PATH = os.path.join(STATE_DIR, f"watch-{SERVER_KEY}.lock")
HIDDEN_PATH = os.path.join(STATE_DIR, "hidden")


def read_config():
    path = os.path.join(CONFIG_DIR, "config.toml")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(DEFAULT_CONFIG)
    config = dict(DEFAULTS)
    with open(path) as f:
        for line in f:
            m = re.match(r"\s*(\w+)\s*=\s*(.*)", line.split("#")[0])
            if not m or m.group(1) not in config:
                continue
            key, value = m.groups()
            if isinstance(DEFAULTS[key], list):
                config[key] = re.findall(r'"([^"]*)"', value)
            elif re.fullmatch(r"\s*[0-9.]+\s*", value):
                config[key] = float(value)
    config["poll_seconds"] = max(1.0, config["poll_seconds"])
    return config


def has_patterns(config, kind):
    return any(config[f"{kind}_{level}"] for level in ("workspaces", "tabs", "panes"))


def matching_panes(config, kind, snapshot):
    """Pane ids whose workspace, tab, or pane label matches a `kind` (pin/top) pattern."""
    if not has_patterns(config, kind):
        return []
    workspaces = {w["workspace_id"]: w.get("label") or "" for w in snapshot["workspaces"]}
    tabs = {t["tab_id"]: t.get("label") or "" for t in snapshot["tabs"]}

    def matches(label, level):
        return any(fnmatch.fnmatchcase(label, glob) for glob in config[f"{kind}_{level}"])

    return sorted(
        p["pane_id"]
        for p in snapshot["panes"]
        if matches(workspaces.get(p["workspace_id"], ""), "workspaces")
        or matches(tabs.get(p["tab_id"], ""), "tabs")
        or matches(p.get("label") or "", "panes")
    )


def snapshot_if_needed(config):
    if has_patterns(config, "pin") or has_patterns(config, "top"):
        return request("session.snapshot", {})["snapshot"]
    return None


def short_name(workspace, tab, aliases=()):
    """A readable agent name from its workspace and tab labels, and whether the
    agent is a firstmate mate nested under the agent that launched it.

    Firstmate labels task workspaces "└ <task> · p:<token>" and tabs "fm-<task>";
    strip that decoration. A tab whose name sits inside its workspace's name is
    the workspace's own agent (Director, a 2ndmate) and shows the workspace name.
    `aliases` are "word=short" rules that abbreviate firstmate labels.
    """
    # A projected one-task workspace always belongs to a mate.
    projected = workspace.strip().startswith("└")
    firstmate_owned = (
        projected
        or workspace.strip().lower().startswith("2ndmate-")
        or tab.strip().startswith("fm-")
    )
    workspace = re.sub(r" · p:\S+$", "", workspace.replace("└", "").strip())
    tab = re.sub(r"^fm-", "", tab.strip())
    shorts = {}
    for rule in aliases:
        word, _, short = rule.partition("=")
        if word:
            shorts[word.lower()] = short
    if firstmate_owned and shorts:
        # Abbreviate before comparing, so a renamed home such as "2ndmate-PJ"
        # still recognizes its own "fm-project" tab (with alias "project=pj").
        pattern = "|".join(re.escape(word) for word in sorted(shorts, key=len, reverse=True))

        def abbreviate(label):
            return re.sub(pattern, lambda match: shorts[match.group().lower()], label, flags=re.IGNORECASE)

        workspace, tab = abbreviate(workspace), abbreviate(tab)
    if not tab or tab.lower() in workspace.lower():
        return workspace, projected
    return tab, firstmate_owned


def with_icon(name, name_icons, strip_prefixes=()):
    """Prefix the first matching icon, then drop configured prefixes, so a rule
    like "2ndmate-*=⚑" can still match a prefix that is stripped for display."""
    icon = next(
        (icon for glob, _, icon in (rule.rpartition("=") for rule in name_icons)
         if glob and icon and fnmatch.fnmatchcase(name, glob)),
        None,
    )
    for prefix in strip_prefixes:
        if prefix and name.startswith(prefix) and len(name) > len(prefix):
            name = name[len(prefix):]
            break
    return f"{icon} {name}" if icon else name


def sync_name_tokens(config, snapshot, agents):
    """Report each agent's short name and nesting rank, only when they changed."""
    workspaces = {w["workspace_id"]: w.get("label") or "" for w in snapshot["workspaces"]}
    tabs = {t["tab_id"]: t.get("label") or "" for t in snapshot["tabs"]}
    for a in agents:
        name, nested = short_name(
            workspaces.get(a.get("workspace_id"), ""), tabs.get(a.get("tab_id"), ""), config["name_aliases"]
        )
        if not name:
            continue
        name = with_icon(name, config["name_icons"], config["strip_prefixes"])
        tokens ={NAME_TOKEN: f"└ {name}" if nested else name, RANK_TOKEN: "1" if nested else "0"}
        current = a.get("tokens") or {}
        if any(current.get(key) != value for key, value in tokens.items()):
            request("pane.report_metadata", {"pane_id": a["pane_id"], "source": SOURCE, "tokens": tokens})


def sync_top_tokens(config, snapshot, agents):
    """Tag top agents with TOP_TOKEN and untag panes that no longer match."""
    top = set(matching_panes(config, "top", snapshot)) if snapshot else set()
    tagged = {a["pane_id"] for a in agents if TOP_TOKEN in (a.get("tokens") or {})}
    for pane, value in [(p, "0") for p in top - tagged] + [(p, None) for p in tagged - top]:
        request("pane.report_metadata", {"pane_id": pane, "source": SOURCE, "tokens": {TOP_TOKEN: value}})


def read_hidden():
    try:
        with open(HIDDEN_PATH) as f:
            return f.read().strip() == "1"
    except FileNotFoundError:
        return False


def write_hidden(hidden):
    with open(HIDDEN_PATH, "w") as f:
        f.write("1\n" if hidden else "0\n")


def request(method, params):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.connect(SOCKET_PATH)
        sock.sendall((json.dumps({"id": "hide_idle", "method": method, "params": params}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    response = json.loads(buf)
    if "error" in response:
        raise RuntimeError(f"{method}: {response['error']}")
    return response["result"]


def build_view(hidden, expired, ttl, pinned):
    """The view to install.

    Hidden keeps working/blocked agents, the current tab, pinned panes, and
    waiting agents not yet expired. Both modes use the structural SORT.
    """
    if not hidden:
        return {"source": SOURCE, "filter": None, "sort": SORT}
    if ttl <= 0:
        stale = {"op": "in", "field": "status", "values": WAITING}
    elif expired:
        # Match the expired state changes too, so an agent that wakes up gets a
        # new sequence number and reappears immediately instead of on the next
        # poll. Two `in` nodes keep this within Herdr's 64-node filter limit.
        stale = {
            "op": "all",
            "filters": [
                {"op": "in", "field": "pane_id", "values": sorted({pane for pane, _ in expired})},
                {"op": "in", "field": "state_change_seq", "values": sorted({seq for _, seq in expired})},
            ],
        }
    else:
        stale = None
    keep = [
        {"op": "not", "filter": {"op": "in", "field": "status", "values": WAITING}},
        {"op": "eq", "field": "tab_id", "value": {"context": "current_tab_id"}},
    ]
    if pinned:
        keep.append({"op": "in", "field": "pane_id", "values": pinned})
    keep.append({"op": "not", "filter": stale} if stale else {"op": "exists", "field": "pane_id"})
    view = {"source": SOURCE, "label": "idle hidden", "filter": {"op": "any", "filters": keep}}
    view["sort"] = SORT
    return view


def load_expired(ttl):
    try:
        with open(TRACK_PATH) as f:
            first_seen = json.load(f)
    except (FileNotFoundError, ValueError):
        return []
    now = time.time()
    return sorted(
        (key.split("|")[0], int(key.split("|")[1]))
        for key, since in first_seen.items()
        if now - since >= ttl
    )


def current_view(hidden, config=None, snapshot=None):
    config = config or read_config()
    snapshot = snapshot or snapshot_if_needed(config)
    ttl = config["ttl_seconds"]
    pinned = matching_panes(config, "pin", snapshot) if snapshot else []
    return build_view(hidden, load_expired(ttl), ttl, pinned)


def apply(view):
    if view:
        request("agent.view.set", view)
    else:
        # Only clear our own view so we never wipe one another plugin installed.
        request("agent.view.clear", {"source": SOURCE})


def notify(title):
    try:
        request("notification.show", {"title": title})
    except Exception:
        pass  # Feedback only; the view change already succeeded.


def acquire_watch_lock():
    """Return the lock file if this process may become the watcher, else None."""
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return None
    return lock


def watch(lock):
    """Track how long each agent has waited and keep the view in sync.

    Runs until the server's socket goes away, so it never outlives its server.
    """
    first_seen = {}
    fresh = True
    last_sent = object()  # Never equal to a view, so the first poll always applies.
    # A restarted server binds a new socket file at the same path; exit then so
    # the new server's startup hook can start its own watcher.
    server_inode = os.stat(SOCKET_PATH).st_ino
    while True:
        try:
            if os.stat(SOCKET_PATH).st_ino != server_inode:
                return
        except FileNotFoundError:
            return
        config = read_config()
        ttl, now = config["ttl_seconds"], time.time()
        try:
            agents = request("agent.list", {})["agents"]
        except (ConnectionRefusedError, FileNotFoundError):
            return  # Server stopped; the lock is released as we exit.
        except (OSError, ValueError) as err:
            # A busy server or a sleep/wake can time out or cut a request;
            # the server is still there, so retry instead of dying silently.
            print(f"agent.list: {err!r}", file=sys.stderr, flush=True)
            time.sleep(config["poll_seconds"])
            continue
        waiting = {
            f"{a['pane_id']}|{a['state_change_seq']}"
            for a in agents
            if a.get("agent_status") in WAITING and "state_change_seq" in a
        }
        # Agents already waiting when the watcher starts have an unknown age;
        # treat them as old so pressing hide clears them right away.
        start = now - ttl if fresh else now
        first_seen = {key: first_seen.get(key, start) for key in waiting}
        fresh = False
        with open(TRACK_PATH + ".tmp", "w") as f:
            json.dump(first_seen, f)
        os.replace(TRACK_PATH + ".tmp", TRACK_PATH)

        try:
            snapshot = request("session.snapshot", {})["snapshot"]
            sync_name_tokens(config, snapshot, agents)
            sync_top_tokens(config, snapshot, agents)
            view = current_view(read_hidden(), config, snapshot)
            if view != last_sent:
                apply(view)
                last_sent = view
        except (ConnectionRefusedError, FileNotFoundError):
            return
        except (OSError, ValueError, RuntimeError) as err:
            # Transient failure or rejected request; retry next poll.
            print(f"{err!r}", file=sys.stderr, flush=True)
        time.sleep(config["poll_seconds"])


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "toggle"
    if cmd == "watch":
        lock = acquire_watch_lock()
        if lock:
            watch(lock)
        return
    if cmd == "toggle":
        hidden = not read_hidden()
    elif cmd in ("hide", "show"):
        hidden = cmd == "hide"
    else:
        sys.exit(f"unknown command: {cmd} (expected toggle|hide|show|watch)")
    write_hidden(hidden)
    notify("Idle agents hidden" if hidden else "Idle agents shown")
    print("hidden" if hidden else "shown", flush=True)
    lock = acquire_watch_lock()
    if lock:
        # No watcher is running for this server (e.g. plugin linked after the
        # server started), so this invocation becomes the watcher; its first
        # poll applies the view.
        watch(lock)
        return
    apply(current_view(hidden))


if __name__ == "__main__":
    main()
