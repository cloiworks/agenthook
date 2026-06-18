#!/usr/bin/env python3
"""
agenthook_tui.py — a small curses config editor for agenthook's routes.json.

Lets you add/edit/delete webhook endpoints (routes), write the prompt that
describes what the agent should do, choose the run mode, and tweak the global
+ per-route agent settings — all without hand-editing JSON.

stdlib only (curses) — no pip installs, same as agenthook itself.

Run:
    python3 agenthook_tui.py [routes.json]

Keys (route list):  ↑/↓ move · a add · e/⏎ edit · d delete · g global · w save · q quit
Keys (a form):      ↑/↓ field · ⏎ edit field · ←/→ toggle choice · ^S save · Esc cancel
The prompt field opens $EDITOR (nano/vi) for comfortable multi-line editing.
"""

from __future__ import annotations

import curses
import json
import os
import secrets
import subprocess
import sys
import tempfile

# --------------------------------------------------------------------------
# Data layer (no curses — independently testable)
# --------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = os.environ.get(
    "AGENTHOOK_CONFIG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "routes.json"),
)


def load_config(path: str) -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    else:
        cfg = {}
    cfg.setdefault("host", "127.0.0.1")
    cfg.setdefault("port", 8644)
    cfg.setdefault("agent", {})
    cfg["agent"].setdefault("bin", os.path.expanduser("~/.local/bin/claude"))
    cfg["agent"].setdefault("workdir", os.path.expanduser("~"))
    cfg["agent"].setdefault("timeout", 1800)
    cfg.setdefault("routes", {})
    return cfg


def save_config(path: str, cfg: dict) -> None:
    """Pretty-write the config and lock it to 0600 (it holds secrets)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def gen_secret() -> str:
    """A fresh 32-hex-char HMAC secret."""
    return secrets.token_hex(16)


def new_route() -> dict:
    return {
        "secret": gen_secret(),
        "mode": "agent",
        "prompt": "",
    }


def validate_route(name: str, route: dict, existing: list[str]) -> list[str]:
    errs: list[str] = []
    if not name.strip():
        errs.append("엔드포인트 이름이 비었습니다.")
    if "/" in name or " " in name:
        errs.append("이름에 공백/슬래시는 쓸 수 없습니다.")
    if name in existing:
        errs.append(f"'{name}' 라우트가 이미 있습니다.")
    if route.get("mode") not in ("agent", "log"):
        errs.append("mode 는 agent 또는 log 여야 합니다.")
    if not str(route.get("secret", "")).strip():
        errs.append("secret 이 비었습니다 (테스트는 INSECURE_NO_AUTH).")
    if not str(route.get("prompt", "")).strip():
        errs.append("prompt 가 비었습니다.")
    return errs


def csv_to_list(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


def list_to_csv(v) -> str:
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return str(v or "")


# --------------------------------------------------------------------------
# curses helpers
# --------------------------------------------------------------------------

def _addstr(win, y, x, text, attr=0):
    """Safe addstr that never crashes on the bottom-right cell / overflow."""
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    win.addnstr(y, x, text, max(0, w - x - 1), attr)


def line_editor(stdscr, label: str, initial: str = "") -> str | None:
    """Single-line editor on the bottom row. Returns text, or None on Esc.

    Uses get_wch so Korean / multibyte input works. Append + backspace editing.
    """
    buf = list(initial)
    h, w = stdscr.getmaxyx()
    curses.curs_set(1)
    try:
        while True:
            y = h - 1
            stdscr.move(y, 0)
            stdscr.clrtoeol()
            prefix = f"{label}: "
            shown = "".join(buf)
            # keep the tail visible if it's longer than the row
            avail = w - len(prefix) - 1
            if len(shown) > avail:
                shown = shown[-avail:]
            _addstr(stdscr, y, 0, prefix + shown, curses.A_REVERSE)
            stdscr.refresh()
            try:
                ch = stdscr.get_wch()
            except curses.error:
                continue
            if isinstance(ch, str):
                if ch in ("\n", "\r"):
                    return "".join(buf)
                if ch == "\x1b":            # Esc
                    return None
                if ch in ("\x7f", "\b", "\x08"):
                    if buf:
                        buf.pop()
                    continue
                if ch.isprintable():
                    buf.append(ch)
            else:
                if ch in (curses.KEY_BACKSPACE, curses.KEY_DC):
                    if buf:
                        buf.pop()
                elif ch == curses.KEY_ENTER:
                    return "".join(buf)
    finally:
        curses.curs_set(0)


def edit_in_editor(initial: str) -> str:
    """Open $EDITOR on a temp file for multi-line prompt editing."""
    editor = os.environ.get("EDITOR") or _first_available(["nano", "vim", "vi"])
    fd, path = tempfile.mkstemp(prefix="agenthook_prompt_", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(initial)
        curses.endwin()
        subprocess.call([editor, path])
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().rstrip("\n")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _first_available(cands: list[str]) -> str:
    from shutil import which
    for c in cands:
        if which(c):
            return c
    return "vi"


def confirm(stdscr, question: str) -> bool:
    h, _ = stdscr.getmaxyx()
    stdscr.move(h - 1, 0)
    stdscr.clrtoeol()
    _addstr(stdscr, h - 1, 0, f"{question} (y/N)", curses.A_REVERSE)
    stdscr.refresh()
    try:
        ch = stdscr.get_wch()
    except curses.error:
        return False
    return isinstance(ch, str) and ch.lower() == "y"


def flash(stdscr, msg: str, attr=0):
    h, _ = stdscr.getmaxyx()
    stdscr.move(h - 1, 0)
    stdscr.clrtoeol()
    _addstr(stdscr, h - 1, 0, msg, attr or curses.A_BOLD)
    stdscr.refresh()


# --------------------------------------------------------------------------
# Forms
# --------------------------------------------------------------------------

def route_form(stdscr, name: str, route: dict, existing_names: list[str]):
    """Edit one route. Returns (name, route_dict) on save, or None on cancel.

    `existing_names` = other route names (for uniqueness), excluding this one.
    """
    route = json.loads(json.dumps(route))           # deep copy
    agent = route.setdefault("agent", {}) if route.get("agent") else {}

    # field model: (key, label, kind) where kind in text/choice/prompt/csv/agent
    fields = [
        ("__name__", "엔드포인트 이름  (POST /webhooks/<이름>)", "name"),
        ("mode", "실행 모드", "choice"),
        ("secret", "시크릿 (HMAC)", "secret"),
        ("events", "이벤트 필터 (콤마, 선택)", "csv"),
        ("prompt", "프롬프트 — 어떤 작업을 할지", "prompt"),
        ("a:model", "agent.model (선택)", "agent_text"),
        ("a:add_dir", "agent.add_dir (콤마, 선택)", "agent_csv"),
        ("a:timeout", "agent.timeout 초 (선택)", "agent_text"),
    ]
    cur = 0
    cur_name = name

    def get_display(key, kind):
        if kind == "name":
            return cur_name
        if kind in ("text", "secret"):
            return str(route.get(key, ""))
        if kind == "choice":
            return route.get("mode", "agent")
        if kind == "csv":
            return list_to_csv(route.get(key))
        if kind == "prompt":
            p = str(route.get("prompt", "")).replace("\n", "⏎")
            return p
        if kind in ("agent_text", "agent_csv"):
            akey = key.split(":", 1)[1]
            v = agent.get(akey)
            return list_to_csv(v) if kind == "agent_csv" else str(v or "")
        return ""

    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        title = f" 라우트 편집: {cur_name or '(새 엔드포인트)'} "
        _addstr(stdscr, 0, 0, title.center(w - 1, "─"), curses.A_BOLD)
        for i, (key, label, kind) in enumerate(fields):
            y = 2 + i
            marker = "▶ " if i == cur else "  "
            val = get_display(key, kind)
            if kind == "choice":
                opts = ["agent", "log"]
                val = "  ".join(
                    f"[{o}]" if o == route.get("mode", "agent") else f" {o} "
                    for o in opts
                )
            attr = curses.A_REVERSE if i == cur else 0
            _addstr(stdscr, y, 0, f"{marker}{label}", attr | curses.A_BOLD)
            _addstr(stdscr, y, max(38, len(label) + 6), val[: w - 40], attr)
        # help
        mode = route.get("mode", "agent")
        hint = ("agent = claude 에이전트 실행 / log = 프롬프트만 로그(드라이런)"
                if fields[cur][2] == "choice" else
                "⏎ 편집 · ←/→ 토글 · ^G 시크릿 생성 · ^S 저장 · Esc 취소")
        _addstr(stdscr, h - 3, 0, f"mode={mode}", curses.A_DIM)
        _addstr(stdscr, h - 2, 0, hint, curses.A_DIM)
        stdscr.refresh()

        try:
            ch = stdscr.get_wch()
        except curses.error:
            continue

        key, label, kind = fields[cur]

        if ch == "\x1b":                                    # Esc
            return None
        if ch == "\x13":                                    # ^S save
            errs = validate_route(cur_name, route, existing_names)
            if errs:
                flash(stdscr, "✗ " + " / ".join(errs), curses.A_BOLD)
                stdscr.get_wch()
                continue
            # prune empty optional agent keys
            for ak in list(agent.keys()):
                if agent[ak] in ("", None, []):
                    del agent[ak]
            if agent:
                route["agent"] = agent
            elif "agent" in route:
                del route["agent"]
            if not route.get("events"):
                route.pop("events", None)
            return cur_name, route
        if ch == "\x07" and kind == "secret":               # ^G gen secret
            route["secret"] = gen_secret()
            continue
        if isinstance(ch, int):
            if ch == curses.KEY_UP:
                cur = (cur - 1) % len(fields)
            elif ch == curses.KEY_DOWN:
                cur = (cur + 1) % len(fields)
            elif ch in (curses.KEY_LEFT, curses.KEY_RIGHT) and kind == "choice":
                route["mode"] = "log" if route.get("mode") == "agent" else "agent"
            continue
        if ch in ("\n", "\r"):
            if kind == "choice":
                route["mode"] = "log" if route.get("mode") == "agent" else "agent"
            elif kind == "prompt":
                route["prompt"] = edit_in_editor(str(route.get("prompt", "")))
            elif kind == "name":
                v = line_editor(stdscr, "엔드포인트 이름", cur_name)
                if v is not None:
                    cur_name = v.strip()
            elif kind in ("text", "secret"):
                v = line_editor(stdscr, label, str(route.get(key, "")))
                if v is not None:
                    route[key] = v
            elif kind == "csv":
                v = line_editor(stdscr, label, list_to_csv(route.get(key)))
                if v is not None:
                    route[key] = csv_to_list(v)
            elif kind in ("agent_text", "agent_csv"):
                akey = key.split(":", 1)[1]
                v = line_editor(stdscr, label, list_to_csv(agent.get(akey)))
                if v is not None:
                    if kind == "agent_csv":
                        agent[akey] = csv_to_list(v)
                    elif akey == "timeout":
                        agent[akey] = int(v) if v.strip().isdigit() else v.strip()
                    else:
                        agent[akey] = v.strip()
            continue
        # space toggles choice too
        if ch == " " and kind == "choice":
            route["mode"] = "log" if route.get("mode") == "agent" else "agent"


def global_form(stdscr, cfg: dict):
    """Edit global settings. Mutates cfg in place. Esc/^S both return."""
    agent = cfg.setdefault("agent", {})
    fields = [
        ("host", "바인드 호스트", "g"),
        ("port", "포트", "g"),
        ("secret", "전역 fallback secret", "g"),
        ("a:bin", "agent.bin (claude 경로)", "a"),
        ("a:workdir", "agent.workdir (실행 cwd)", "a"),
        ("a:timeout", "agent.timeout 초", "a"),
        ("a:model", "agent.model (선택)", "a"),
    ]
    cur = 0
    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        _addstr(stdscr, 0, 0, " 전역 설정 ".center(w - 1, "─"), curses.A_BOLD)
        for i, (key, label, scope) in enumerate(fields):
            y = 2 + i
            marker = "▶ " if i == cur else "  "
            if scope == "a":
                val = str(agent.get(key.split(":", 1)[1], ""))
            else:
                val = str(cfg.get(key, ""))
            attr = curses.A_REVERSE if i == cur else 0
            _addstr(stdscr, y, 0, f"{marker}{label}", attr | curses.A_BOLD)
            _addstr(stdscr, y, 36, val[: w - 38], attr)
        _addstr(stdscr, h - 2, 0, "⏎ 편집 · ^S/Esc 돌아가기", curses.A_DIM)
        stdscr.refresh()
        try:
            ch = stdscr.get_wch()
        except curses.error:
            continue
        if ch in ("\x1b", "\x13"):
            return
        if isinstance(ch, int):
            if ch == curses.KEY_UP:
                cur = (cur - 1) % len(fields)
            elif ch == curses.KEY_DOWN:
                cur = (cur + 1) % len(fields)
            continue
        if ch in ("\n", "\r"):
            key, label, scope = fields[cur]
            akey = key.split(":", 1)[1] if scope == "a" else key
            current = (agent.get(akey, "") if scope == "a" else cfg.get(key, ""))
            v = line_editor(stdscr, label, str(current))
            if v is None:
                continue
            if akey in ("port", "timeout"):
                v = int(v) if v.strip().isdigit() else current
            if scope == "a":
                agent[akey] = v
            else:
                cfg[key] = v


# --------------------------------------------------------------------------
# Main list screen
# --------------------------------------------------------------------------

def run(stdscr, path: str):
    curses.curs_set(0)
    cfg = load_config(path)
    dirty = False
    cur = 0

    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        routes = cfg["routes"]
        names = list(routes.keys())

        head = f" agenthook 설정  ·  {cfg['host']}:{cfg['port']}  ·  {path} "
        _addstr(stdscr, 0, 0, head.center(w - 1, "─"),
                curses.A_BOLD | (curses.A_REVERSE if dirty else 0))
        _addstr(stdscr, 1, 0,
                f"라우트 {len(names)}개" + ("   ● 저장 안 됨" if dirty else ""),
                curses.A_DIM)

        if not names:
            _addstr(stdscr, 3, 2, "라우트가 없습니다.  'a' 로 엔드포인트를 추가하세요.",
                    curses.A_DIM)
        for i, nm in enumerate(names):
            y = 3 + i
            if y >= h - 3:
                break
            r = routes[nm]
            mode = r.get("mode", "agent")
            sec = "open" if r.get("secret") == "INSECURE_NO_AUTH" else "🔒"
            prev = str(r.get("prompt", "")).replace("\n", " ")[: w - 40]
            marker = "▶ " if i == cur else "  "
            attr = curses.A_REVERSE if i == cur else 0
            _addstr(stdscr, y, 0,
                    f"{marker}/webhooks/{nm}", attr | curses.A_BOLD)
            _addstr(stdscr, y, 26, f"[{mode}] {sec}", attr)
            _addstr(stdscr, y, 40, prev, attr | curses.A_DIM)

        help1 = "↑/↓ 선택 · a 추가 · e/⏎ 편집 · d 삭제 · g 전역설정"
        help2 = "w 저장 · q 종료"
        _addstr(stdscr, h - 2, 0, help1, curses.A_DIM)
        _addstr(stdscr, h - 1, 0, help2, curses.A_DIM)
        stdscr.refresh()

        try:
            ch = stdscr.get_wch()
        except curses.error:
            continue

        if isinstance(ch, int):
            if ch == curses.KEY_UP and names:
                cur = (cur - 1) % len(names)
            elif ch == curses.KEY_DOWN and names:
                cur = (cur + 1) % len(names)
            continue

        if ch in ("q", "Q"):
            if dirty and not confirm(stdscr, "저장 안 된 변경이 있습니다. 그냥 종료?"):
                continue
            return
        elif ch in ("w", "W"):
            save_config(path, cfg)
            dirty = False
            flash(stdscr, f"✓ 저장됨: {path} (chmod 600)", curses.A_BOLD)
            stdscr.get_wch()
        elif ch in ("g", "G"):
            before = json.dumps(cfg, sort_keys=True)
            global_form(stdscr, cfg)
            if json.dumps(cfg, sort_keys=True) != before:
                dirty = True
        elif ch in ("a", "A"):
            res = route_form(stdscr, "", new_route(), names)
            if res:
                nm, r = res
                cfg["routes"][nm] = r
                dirty = True
                cur = list(cfg["routes"]).index(nm)
        elif ch in ("e", "E", "\n", "\r") and names:
            nm = names[cur]
            others = [n for n in names if n != nm]
            res = route_form(stdscr, nm, routes[nm], others)
            if res:
                new_nm, r = res
                if new_nm != nm:
                    del cfg["routes"][nm]
                cfg["routes"][new_nm] = r
                dirty = True
        elif ch in ("d", "D") and names:
            nm = names[cur]
            if confirm(stdscr, f"'{nm}' 라우트를 삭제할까요?"):
                del cfg["routes"][nm]
                dirty = True
                cur = max(0, cur - 1)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CONFIG_PATH
    curses.wrapper(run, path)


if __name__ == "__main__":
    main()
