#!/usr/bin/env python3
"""Read Microsoft Teams (new Teams for macOS) through its accessibility tree.

Read-only by design: list chats, open one, read its messages. Everything runs in
the background through cua-driver (no focus steal, no screenshots). Jev is asked
only to resolve an ambiguous chat name, and it only ever sees chat titles and the
words the person typed.

  teams.py chats [--unread] [--json]
  teams.py open QUERY [--read] [--last N] [--json]
  teams.py read [--last N] [--json]

Environment:
  CUA_DRIVER_BIN      accessibility/screen-recording driver   (default: cua-driver)
  JEV_BIN             Jev CLI                                  (default: jev)
  JEV_FLOOR           minimum confidence to accept a Jev pick (default: 0.65)
  JEV_MAX_CANDIDATES  chat titles sent to Jev                  (default: 30)
  JEV_WORK_TIMEOUT    seconds before a helper is abandoned     (default: 30)
  JEV_WORK_CHAT_HOTKEY  keys that switch Teams to Chat         (default: cmd,2)

Exit codes: 0 ok, 2 FAIL, 4 UNVERIFIED, 6 ABSTAIN, 130 interrupted.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

DRIVER = os.environ.get("CUA_DRIVER_BIN", "cua-driver")
JEV = os.environ.get("JEV_BIN", "jev")
JEV_FLOOR = float(os.environ.get("JEV_FLOOR", "0.65"))
JEV_MAX_CANDIDATES = int(os.environ.get("JEV_MAX_CANDIDATES", "30"))
TIMEOUT = int(os.environ.get("JEV_WORK_TIMEOUT", "30"))
MESSAGE_MARKER = "More message options"
CHAT_HOTKEY = os.environ.get("JEV_WORK_CHAT_HOTKEY", "cmd,2").split(",")  # Teams: switch the left rail to Chat


class Fail(Exception):
    pass


def run_json(argv: list[str], payload: dict | None, what: str) -> dict:
    """Run a helper that answers JSON on stdout. Every failure mode becomes a Fail,
    so the documented exit codes hold whatever goes wrong underneath."""
    try:
        proc = subprocess.run(
            argv, input=None if payload is None else json.dumps(payload),
            capture_output=True, text=True, timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise Fail(f"{what} timed out after {TIMEOUT}s") from exc
    except OSError as exc:
        raise Fail(f"{what} could not be run ({exc.strerror})") from exc
    if proc.returncode != 0:
        raise Fail(f"{what} failed: {(proc.stderr or proc.stdout).strip()[:300]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise Fail(f"{what} returned non-JSON output") from exc


def read_tool(tool: str, args: dict) -> dict:
    """Observe. Never changes anything."""
    return run_json([DRIVER, "call", tool, json.dumps(args)], None, f"cua-driver {tool}")


def send_input(tool: str, args: dict) -> dict:
    """Act on the screen. If the driver refuses because the window sits on another
    Space, retry once in foreground mode (fronts it briefly, then restores)."""
    result = read_tool(tool, args)
    if result.get("effect") == "refused" and result.get("escalation", {}).get("recommended") == "foreground":
        result = read_tool(tool, {**args, "delivery_mode": "foreground"})
    if result.get("effect") == "refused":
        raise Fail(f"cua-driver refused {tool}: {result.get('reason', '')[:200]}")
    return result




def teams_pid() -> int:
    proc = subprocess.run(["pgrep", "-x", "MSTeams"], capture_output=True, text=True)
    pids = [int(p) for p in proc.stdout.split()]
    if pids:
        return pids[0]
    # Not running: start it in the background (no focus steal) and wait for it.
    read_tool("launch_app", {"bundle_id": "com.microsoft.teams2"})
    for _ in range(50):
        time.sleep(0.2)
        proc = subprocess.run(["pgrep", "-x", "MSTeams"], capture_output=True, text=True)
        if proc.stdout.split():
            return int(proc.stdout.split()[0])
    raise Fail("Microsoft Teams did not start")


def main_window(pid: int, wait: float = 45.0) -> dict:
    # A freshly started Teams needs a while before its main window exists.
    deadline = time.time() + wait
    while True:
        windows = [
            w for w in read_tool("list_windows", {"pid": pid})["windows"]
            if w["title"].endswith("| Microsoft Teams")
        ]
        if windows:
            return max(windows, key=lambda w: w["bounds"]["width"] * w["bounds"]["height"])
        if time.time() > deadline:
            raise Fail("no Teams main window (is Teams signed in?)")
        time.sleep(0.5)


def snapshot(pid: int, window_id: int) -> list[dict]:
    state = read_tool("get_window_state", {"pid": pid, "window_id": window_id})
    return [e for e in state["elements"] if e.get("role") not in ("AXMenuItem", "AXMenu", "AXMenuBarItem")]


def label(e: dict) -> str:
    return (e.get("label") or e.get("value") or "").strip()


ROW_RE = re.compile(r"^(?P<unread>Unread message )?(?P<kind>Chat|Group chat|Meeting chat) (?P<rest>.*)$")
STATUS_RE = re.compile(
    r"\s+(Available|Away|Busy|Offline|Do not disturb|Be right back|Appear offline|"
    r"Out of office|External unfamiliar( \w+)?|Has external participants|Has pinned messages)\b.*$"
)


def parse_row(e: dict) -> dict | None:
    m = ROW_RE.match(label(e))
    if not m:
        return None
    rest = m.group("rest")
    head, _, last = rest.partition(" Last message ")
    name = STATUS_RE.sub("", head).strip()
    return {
        "name": name,
        "kind": m.group("kind"),
        "unread": bool(m.group("unread")),
        "last": last.strip(),
        "token": e["element_token"],
    }


def chat_rows(elements: list[dict]) -> list[dict]:
    rows = [parse_row(e) for e in elements if e.get("role") == "AXRow"]
    return [r for r in rows if r]


def ensure_chat_view(pid: int, window: dict) -> dict:
    # The window title can go stale after a restart ("Calendar" while Chat shows), so the
    # chat rows in the tree are the truth.
    def has_chats() -> bool:
        return bool(chat_rows(snapshot(pid, window["window_id"])))

    if has_chats():
        return window
    send_input("hotkey", {"pid": pid, "window_id": window["window_id"], "keys": CHAT_HOTKEY})
    for _ in range(10):
        time.sleep(0.3)
        if has_chats():
            return main_window(pid)
    raise Fail(
        "no chat list in the Teams window: it is probably minimized, hidden or off screen, "
        "where macOS hides it from accessibility. Ask the person to show the Teams window "
        "(it may sit behind others; it does not need focus)"
    )


def jev_pick(query: str, rows: list[dict]) -> tuple[dict | None, int]:
    """Ask Jev which chat title matches. Only titles leave the machine.

    Returns the pick (or None) and how many rows the cap kept out of the running.
    A dropped row is a chat the caller will never learn about, so the caller has to
    say so rather than report a clean miss."""
    total = len(rows)
    considered = rows[:JEV_MAX_CANDIDATES]
    dropped = total - len(considered)
    ids = {f"c{i}": r for i, r in enumerate(considered)}
    request = {
        "schema": "jev.action_choice_request_v1",
        "goal": f"Open the Teams chat the person means by: {query}",
        "observation_id": f"teams-{int(time.time())}",
        "regions": [
            {"id": cid, "role": "row", "label": f"{r['kind']}: {r['name']}", "interactive": True}
            for cid, r in ids.items()
        ],
        "history": [],
        "candidates": [
            *({"id": cid, "description": f"Open {r['kind'].lower()} '{r['name']}'"} for cid, r in ids.items()),
            {"id": "reobserve", "description": "Look at the chat list again without opening anything."},
            {"id": "abstain", "description": "None of these chats is the one meant; ask the person."},
        ],
    }
    choice = run_json([JEV, "choose"], request, "jev choose")
    if choice.get("confidence", 0) < JEV_FLOOR:
        return None, dropped
    return ids.get(choice.get("selected_id")), dropped


def resolve(query: str, rows: list[dict]) -> tuple[dict | None, str, int]:
    """Exact name, then unique substring, then Jev over chat titles only."""
    q = query.casefold()
    exact = [r for r in rows if r["name"].casefold() == q]
    if len(exact) == 1:
        return exact[0], "exact", 0
    partial = [r for r in rows if q in r["name"].casefold()]
    if len(partial) == 1:
        return partial[0], "substring", 0
    row, dropped = jev_pick(query, partial or rows)
    return row, "jev", dropped


TIME_RE = re.compile(r"^(\d{1,2}/\d{1,2}(/\d{2,4})?\s*)?\d{1,2}:\d{2}$|^\d{1,2}/\d{1,2}(/\d{2,4})?$|^(Yesterday|Today)\b")
BODY_ROLES = ("AXStaticText", "AXListMarker", "AXImage", "AXLink")


def parse_messages(elements: list[dict]) -> list[dict]:
    """Each message opens with an AXHeading '<preview> by <author>', then a header
    (time, author), the 'More message options' button, and the body. The body runs
    until the next message heading or the compose box."""
    by_index = {e["element_index"]: e for e in elements}

    def in_chrome(e: dict) -> bool:
        # Reaction bars, attachment toolbars and similar live under an AXToolbar.
        parent = by_index.get(e.get("parent_index"))
        while parent:
            if parent.get("role") in ("AXToolbar", "AXCheckBox"):
                return True
            parent = by_index.get(parent.get("parent_index"))
        return False

    starts = [
        i for i, e in enumerate(elements)
        if e.get("role") == "AXHeading" and " by " in label(e)
    ]
    compose = next((i for i, e in enumerate(elements) if e.get("role") == "AXTextArea"), len(elements))
    messages = []
    for n, start in enumerate(starts):
        end = min(starts[n + 1] if n + 1 < len(starts) else len(elements), compose)
        if start >= end:
            continue
        author = label(elements[start]).rsplit(" by ", 1)[1].strip()
        chunk = elements[start + 1:end]
        marker = next((k for k, e in enumerate(chunk) if e.get("role") == "AXButton" and label(e) == MESSAGE_MARKER), None)
        header = chunk[:marker] if marker is not None else []
        body_elems = chunk[marker + 1:] if marker is not None else chunk
        stamp = next((label(e) for e in header if TIME_RE.match(label(e))), "")
        body = [
            label(e) for e in body_elems
            if e.get("role") in BODY_ROLES and label(e) and not in_chrome(e) and label(e) != "Sent"
        ]
        messages.append({"time": stamp, "author": author, "text": " ".join(body).strip()})
    return messages


def cmd_chats(args) -> int:
    pid = teams_pid()
    window = ensure_chat_view(pid, main_window(pid))
    rows = chat_rows(snapshot(pid, window["window_id"]))
    if args.unread:
        rows = [r for r in rows if r["unread"]]
    for r in rows:
        r.pop("token")
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    else:
        for r in rows:
            flag = "*" if r["unread"] else " "
            print(f"{flag} [{r['kind']}] {r['name']} :: {r['last'][:100]}")
    return 0


def cmd_read(args, pid: int | None = None, window: dict | None = None) -> int:
    pid = pid or teams_pid()
    window = window or main_window(pid)
    messages = parse_messages(snapshot(pid, window["window_id"]))[-args.last:]
    # "<section> | <chat> | Microsoft Teams"; the section prefix can be stale.
    parts = window["title"].split(" | ")
    title = " | ".join(parts[1:-1]) if len(parts) >= 3 else parts[0]
    if args.json:
        print(json.dumps({"chat": title, "messages": messages}, ensure_ascii=False, indent=1))
    else:
        print(f"== {title}")
        for m in messages:
            print(f"[{m['time']}] {m['author']}: {m['text']}")
    return 0


def cmd_open(args) -> int:
    pid = teams_pid()
    window = ensure_chat_view(pid, main_window(pid))
    rows = chat_rows(snapshot(pid, window["window_id"]))
    row, how, dropped = resolve(args.query, rows)
    searched = len(rows) - dropped
    partial_note = f"; only the first {searched} of {len(rows)} chats were offered" if dropped else ""
    if not row:
        print(
            f"ABSTAIN: no chat clearly matches {args.query!r}{partial_note}; "
            f"run `chats` and pass an exact name",
            file=sys.stderr,
        )
        return 6
    before = window["title"]
    # A group chat titles the window with its members ("Ada, +2"), not the row name
    # ("Ada and Grace"), so match on the first word of the name.
    first_word = re.split(r"[\s,]+", row["name"])[0]

    def title_shows_chat() -> bool:
        return first_word in window["title"] and (window["title"] != before or first_word in before)

    def heading_shows_chat() -> bool:
        # Fallback when the title lags: a pane heading (not a message heading, which
        # reads "... by <author>") naming the chat.
        return any(
            e.get("role") == "AXHeading" and first_word in label(e) and " by " not in label(e)
            for e in snapshot(pid, window["window_id"])
        )

    # A background click occasionally does not register in the WebView: click once more.
    for attempt in range(2):
        send_input("click", {"pid": pid, "window_id": window["window_id"], "element_token": row["token"]})
        # An occluded Teams window renders slowly, so give the title up to 5 s.
        for _ in range(20):
            time.sleep(0.25)
            window = main_window(pid)
            if title_shows_chat():
                break
        else:
            if attempt == 0:
                rows = chat_rows(snapshot(pid, window["window_id"]))
                row = next((r for r in rows if r["name"] == row["name"]), row)
                continue
            if not heading_shows_chat():
                print(
                    f"UNVERIFIED: clicked {row['name']!r} ({how}) but could not confirm the chat opened",
                    file=sys.stderr,
                )
                return 4
        break
    if not args.json:
        print(f"opened {row['name']} ({how}{partial_note})")
    if args.read:
        time.sleep(0.4)
        return cmd_read(args, pid, window)
    if args.json:
        print(json.dumps(
            {"opened": row["name"], "match": how, "candidates_considered": searched,
             "candidates_dropped": dropped},
            ensure_ascii=False,
        ))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("chats", help="list chats in the sidebar")
    c.add_argument("--unread", action="store_true", help="only chats flagged unread")
    c.add_argument("--json", action="store_true")
    o = sub.add_parser("open", help="open a chat by name (exact, substring, then Jev)")
    o.add_argument("query")
    o.add_argument("--read", action="store_true", help="print its messages after opening")
    o.add_argument("--last", type=int, default=20)
    o.add_argument("--json", action="store_true")
    r = sub.add_parser("read", help="print messages of the open chat")
    r.add_argument("--last", type=int, default=20)
    r.add_argument("--json", action="store_true")
    args = p.parse_args()
    try:
        return {"chats": cmd_chats, "open": cmd_open, "read": cmd_read}[args.cmd](args)
    except Fail as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
