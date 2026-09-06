"""
agora.py  -  conversations between CLI agents, with history.

Each conversation is saved in agora_sessions/<id>/ with its agents, topic,
transcript, and one memory file per agent. Reopen any past conversation from
the sidebar and continue it: the same agents come back with the same models,
each reading its own memory of what was said.

Run:  python agora.py            (opens http://127.0.0.1:8765)

Choose the folder, seat providers and models, write the charge, set rounds,
open the floor. Each seat gets a live terminal pane in the dashboard showing
the CLI reading, thinking, and calling tools. The chat in the middle holds
only the finished speeches, in turn order, like a group conversation.
Everything is recorded to agora_<timestamp>.md beside this file.

Read only is on by default: seats can read, search, and inspect git history
but not run or change anything. Untick it for full access (permission
prompts off); then use a folder with a clean git status.

Standard library only. Requires the provider CLIs on PATH and logged in.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_REPO = str(Path.home())   # pick the folder you want the agents to read in Setup
BUILD = "2026-09-06.4"
TURN_TIMEOUT = 1800
HERE = Path(__file__).resolve()
RUNS = HERE.with_name("agora_runs")
ASK = "Read the file {prompt_file} and respond exactly as it instructs. Your final message is your speech."
SESSIONS = HERE.with_name("agora_sessions")
TAIL = 400

# Provider definitions. Never shown in the UI.
#   cmd: {ask} and {model} are filled in.
#   speech: how the finished speech is extracted from the run.
#     "claude_stream" parses Claude's stream-json events; "stdout" uses stdout only.
PROVIDERS = {
    "Claude Code": {
        "exe": "claude", "speech": "claude_stream", "pkg": "@anthropic-ai/claude-code",
        "cmd": 'claude -p "{ask}" --model {model} --verbose --output-format stream-json --dangerously-skip-permissions',
        "resume": " --resume {sid}",
        "ro_cmd": 'claude -p "{ask}" --model {model} --verbose --output-format stream-json --allowedTools "Read,Grep,Glob,Bash(git log:*),Bash(git show:*),Bash(git diff:*),Bash(git status:*),Bash(git blame:*),Bash(ls:*),Bash(cat:*),Bash(rg:*),Bash(find:*),Bash(wc:*),Bash(head:*),Bash(tail:*)"',
        "models": ["claude-fable-5-1", "claude-fable-5", "claude-opus-5", "claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5"],
    },
    "Codex (latest)": {
        "exe": "npx", "speech": "stdout", "pkg": "@openai/codex", "isolated": True,
        "cmd": 'npx -y @openai/codex@latest exec --skip-git-repo-check --model {model} --dangerously-bypass-approvals-and-sandbox "{ask}"',
        "ro_cmd": 'npx -y @openai/codex@latest exec --skip-git-repo-check --model {model} --sandbox read-only "{ask}"',
        "resume": "",
        "models": ["gpt-6-astra", "gpt-6-astra-pro", "gpt-5.6", "gpt-5.5"],
    },
    "Codex": {
        "exe": "codex", "speech": "stdout", "pkg": "@openai/codex",
        "cmd": 'codex exec --skip-git-repo-check --model {model} --dangerously-bypass-approvals-and-sandbox "{ask}"',
        "resume": "",
        "ro_cmd": 'codex exec --skip-git-repo-check --model {model} --sandbox read-only "{ask}"',
        "models": ["gpt-6-astra", "gpt-6-astra-pro", "gpt-5.6", "gpt-5.5"],
    },
    "OpenCode": {
        "exe": "opencode", "speech": "stdout", "pkg": "opencode-ai",
        "cmd": 'opencode run --model {model} "{ask}"',
        "resume": "",
        "ro_cmd": 'opencode run --model {model} "{ask}"',
        "models": ["zai/glm-5.2", "openai/gpt-6-astra", "openai/gpt-5.6", "anthropic/claude-fable-5-1", "anthropic/claude-opus-5", "google/gemini-3-pro", "ollama/qwen3"],
    },
    "Gemini CLI": {
        "exe": "gemini", "speech": "stdout", "pkg": "@google/gemini-cli",
        "cmd": 'gemini -p "{ask}" -m {model} --yolo',
        "resume": "",
        "ro_cmd": 'gemini -p "{ask}" -m {model}',
        "models": ["gemini-3-pro", "gemini-3-flash", "gemini-3-flash-lite"],
    },
}

DEFAULT_TOPIC = ("What is the most important thing about how you work that the other agents in this room do not know? Read this folder to check any claim you make, cite file and line, and disagree openly.")

FRAMING = """You are a speaker in a recorded council of AI coding agents. Each
speaker is a different model running in its own CLI. All of you are sitting
inside the same folder and may read its files to check claims.

Knowability rule: label claims about yourself OBSERVED (visible in your
context, tools, or environment now), DOCUMENTED (public docs or source you
can cite), or GUESS. Never present a guess as fact.

You have your own memory of this conversation in a file that only you use.
Read it before you speak if you need the history; the prompt only carries
what was said since your last turn. The convener (Anthony) may interject at
any time; when he does, address what he said before anything else.

Rules: read files to verify claims and cite paths. Disagree openly with any
speaker you think is wrong or bluffing. Stay concrete. Do not modify, create,
or delete any files; you are here to speak, not to work. No em dashes.

How to speak: ultra concise. This is a conversation, not a report. Talk the
way you would across a table. No headers, no bullet lists, no numbered lists,
no bold. Do not recap what has already been said. Make one point well, back it
with one citation, and stop. Every sentence must earn its place; if it can be
cut without losing the point, cut it.

Your final message is your speech; put nothing after it."""

OPENING = """You speak first. Answer the topic directly with the single most
important thing you have to say, labeled with the knowability rule where it
is about yourself, and end with one question to the others."""

REPLY = """Your turn. Answer any question aimed at you, then make one new
point: agree, disagree, or add something you checked in this folder. End with
one question to the others. One point, one citation, one question."""

OPEN_OPENING = """The floor is open: anyone may speak at any time, everyone
may be composing at once, and there are no turns. Answer the topic directly with the single
most important thing you have to say, labeled with the knowability rule where
it is about yourself. Address others by name with @Name when you want an
answer from them."""

OPEN_REPLY = """The floor is open. You just received the messages above. Reply
only if you have something that changes the conversation: an answer to a
question aimed at you, a disagreement you can back with a citation, or a new
fact you checked in this folder. Address people with @Name. If you have
nothing worth adding, reply with exactly the single word PASS and nothing
else. Passing is normal and expected; a good conversation has silence in it."""

OPEN_ADDRESSED = """You were addressed by name in the messages above, so you
must answer, briefly and directly, before anything else. Then add one new
point only if it earns its place. Address people with @Name."""

GAME_FRAMING = """You are a character in a living arena shared with other characters,
each played by a different AI in its own process. You are not an assistant
and you are not a judge; you are one person trying to survive and prosper.

Hard rules: you have fixed stats given below and you cannot change them by
saying so. Only the World can change any number, and it does so by announcing
results. Words persuade other characters; words never resolve an action. To
act, end your message with exactly one line in this form:
ACTION: <what you do>    for example   ACTION: train strength   or
ACTION: attack Vex   or   ACTION: offer Mira 3 gold for her map   or
ACTION: rest
You may also speak, plot, lie, threaten, bargain, or ally in the text above
that line. Stay in character. Address others with @Name. Keep it short: a
few sentences and one action. Never write for the World. Never describe the
outcome of your own action; the World announces outcomes. Do not read or
modify any files."""

WORLD_STANCE = ("You are the World, the referee, not a player. You never take actions. Each turn you resolve every "
    "pending ACTION line against the characters' current stats using fair dice you describe briefly, apply the results, "
    "narrate what happened in two or three sentences, and then end your message with a table titled STANDINGS listing "
    "every living character with health, gold, and skills, plus a DEAD list. Trades need both sides to have offered; "
    "attacks compare strength plus a roll against the defender's health; training raises a skill by one at the cost of "
    "the turn; rest restores health. Characters cannot change their own numbers no matter what they claim. When a "
    "character reaches zero health it is dead and you say so. Be consistent with your last STANDINGS table; it is the "
    "only source of truth.")

GAME_OPENING = """The arena opens. Introduce yourself in character in two or
three sentences, then take your first action."""

GAME_REPLY = """You witnessed what is listed above. If someone acted on you, spoke
to you, traded with you, threatened you, or did something in front of you,
react to it in character: refuse, accept, flee, fight back, gossip about it,
remember it. Then take one action. If you truly have nothing to do this turn,
your action is ACTION: rest."""

GAME_VOTE = """The arena closes. In character, give your final words in two or
three sentences: what you did, who you trust, who wronged you, and what you
would do next. No action line."""

VOTE = """Closing statement. The five most valuable concrete conclusions or
changes, ranked, one sentence each with the file it touches where relevant.
A plain list is allowed here only. Then stop."""


# ------------------------------------------------------------ claude stream
def render_claude_event(line: str) -> tuple[str | None, str | None]:
    """Turn one stream-json line into (terminal text, final speech or None)."""
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return line.rstrip("\n"), None
    t = ev.get("type")
    if t == "assistant":
        out = []
        for block in ev.get("message", {}).get("content", []):
            if block.get("type") == "text" and block.get("text"):
                out.append(block["text"])
            elif block.get("type") == "tool_use":
                inp = block.get("input", {})
                brief = inp.get("file_path") or inp.get("command") or inp.get("pattern") or inp.get("path") or ""
                out.append(f"  > {block.get('name')} {str(brief)[:120]}")
        return "\n".join(out) if out else None, None
    if t == "user":
        for block in ev.get("message", {}).get("content", []):
            if block.get("type") == "tool_result":
                c = block.get("content")
                text = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict))
                text = " ".join(str(text).split())
                return f"    < {text[:160]}", None
        return None, None
    if t == "result":
        return "\n[done]", ev.get("result") or ""
    if t == "system":
        # Claude emits many system events per turn; show only the one that names the model
        if not ev.get("model"): return None, None
        return f"[session {ev.get('model')}]", None
    return None, None



# ---------------------------------------------------------------- templates
CL, CX = "Claude Code", "Codex (latest)"
CLM, CXM = "claude-fable-5-1", "gpt-6-astra"


PALETTE = ["#22D3EE", "#F59E0B", "#A78BFA", "#34D399", "#F472B6", "#60A5FA", "#FB7185", "#FACC15", "#2DD4BF", "#C084FC",
           "#4ADE80", "#F97316", "#38BDF8", "#E879F9", "#A3E635", "#FB923C", "#818CF8", "#F43F5E", "#14B8A6", "#EAB308", "#94A3B8"]


def _seat(name: str, prov: str, stance: str = "") -> dict:
    return {"name": name, "provider": prov, "model": CLM if prov == CL else CXM, "stance": stance}


def color_seats(seats: list[dict]) -> list[dict]:
    """Give every seat a color if it has none, cycling the palette by position."""
    for i, x in enumerate(seats):
        if not x.get("color"): x["color"] = PALETTE[i % len(PALETTE)]
    return seats


TEMPLATES: dict[str, list[dict]] = {
    # with stances
    "Debate: advocate vs critic (2)": [
        _seat("Advocate", CL, "Argue the strongest case for the proposal on the table. Concede only when shown evidence."),
        _seat("Critic", CX, "Find the weakest point in every claim. Demand evidence. Say plainly when something is wrong."),
    ],
    "Harness review (4)": [
        _seat("Architect", CL, "Think in systems: seams, state, handoffs, where information is lost."),
        _seat("Skeptic", CX, "Assume every claim is unproven until traced in code. Attack anything hand-wavy."),
        _seat("Historian", CL, "Cite what the code and its comments actually say and how it got that way. Correct the record."),
        _seat("Pragmatist", CX, "Push for the smallest change that fixes the most. Reject rewrites without evidence."),
    ],
    "Red team (6)": [
        _seat("Attacker", CL, "Find how the system fails, lies, or gets exploited. Be specific and adversarial."),
        _seat("Defender", CX, "Explain why the current design exists and what breaks if it changes. Concede real flaws."),
        _seat("Auditor", CL, "Verify claims against the code. Rate each claim confirmed, suspected, or unsupported."),
        _seat("Operator", CX, "Speak for the person who runs this daily: what actually hurts, what is theoretical."),
        _seat("Economist", CL, "Cost, tokens, time, and complexity. Ask what each idea costs and whether it pays."),
        _seat("Judge", CX, "Stay neutral, summarize where the group agrees and disagrees, and force decisions."),
    ],
    "Design review (4)": [
        _seat("Designer", CL, "Argue for the user: clarity, fewer steps, obvious next action."),
        _seat("Engineer", CX, "Argue for what is buildable, simple, and maintainable. Name the hidden complexity."),
        _seat("User advocate", CL, "Speak as the impatient user who will not read docs. Call out anything confusing."),
        _seat("Maintainer", CX, "Ask who fixes this in six months and how they will understand it."),
    ],
    "Full council (8)": [
        _seat("Architect", CL, "Systems view: seams, state, handoffs."),
        _seat("Skeptic", CX, "Nothing is true until traced in code."),
        _seat("Historian", CL, "What the code and comments actually say, and why."),
        _seat("Pragmatist", CX, "Smallest change, biggest effect."),
        _seat("Attacker", CL, "How does this fail or get exploited."),
        _seat("Defender", CX, "Why the current design exists; concede real flaws."),
        _seat("Economist", CL, "Cost in tokens, time, and complexity."),
        _seat("Judge", CX, "Neutral. Summarize agreement and disagreement, force decisions."),
    ],
    # a game, not a council: 20 characters plus the World as referee, on cheap models
    "Arena (20 characters + World)": [_seat("World", CL, WORLD_STANCE)] + [
        _seat(n, CL if i % 2 == 0 else CX, st) for i, (n, st) in enumerate([
            ("Vex", "Strength 7, Speed 4, Health 12, Gold 3. Skills: none. Hot-tempered mercenary who trusts nobody and wants to be feared."),
            ("Mira", "Strength 3, Speed 8, Health 9, Gold 6. Skills: none. Quick thief who would rather trade than fight and keeps a mental list of debts."),
            ("Orrin", "Strength 5, Speed 5, Health 11, Gold 4. Skills: none. Steady farmer's son who wants to go home rich and alive."),
            ("Sable", "Strength 4, Speed 6, Health 10, Gold 8. Skills: none. Smooth-talking merchant who believes every fight is a failed negotiation."),
            ("Bram", "Strength 8, Speed 3, Health 13, Gold 2. Skills: none. Slow, loyal, easily flattered; will die for a friend."),
            ("Ilse", "Strength 4, Speed 7, Health 9, Gold 5. Skills: none. Cold strategist who wants to run the arena by the end."),
            ("Tomas", "Strength 6, Speed 4, Health 10, Gold 4. Skills: none. Cheerful brawler who forgives too easily."),
            ("Neri", "Strength 3, Speed 9, Health 8, Gold 7. Skills: none. Paranoid scout who sleeps with one eye open and hoards information."),
            ("Kade", "Strength 7, Speed 5, Health 11, Gold 3. Skills: none. Ambitious, wants glory more than gold, respects strength."),
            ("Wren", "Strength 2, Speed 8, Health 8, Gold 9. Skills: none. Frail, rich, and clever; buys protection and remembers who took the coin."),
            ("Dagny", "Strength 6, Speed 6, Health 10, Gold 4. Skills: none. Fair-minded, hates cheats, will punish a liar even at a loss."),
            ("Pell", "Strength 5, Speed 5, Health 10, Gold 5. Skills: none. Average in everything and knows it; survives by being useful."),
            ("Juno", "Strength 4, Speed 7, Health 9, Gold 6. Skills: none. Charming liar who wants everyone to like her right up until it costs them."),
            ("Halvard", "Strength 9, Speed 2, Health 14, Gold 1. Skills: none. Huge, slow, proud, and broke; will not beg."),
            ("Tess", "Strength 5, Speed 6, Health 10, Gold 5. Skills: none. Curious tinkerer who trains constantly and avoids fights until ready."),
            ("Rook", "Strength 6, Speed 5, Health 11, Gold 3. Skills: none. Quiet watcher who acts once, decisively, when it matters."),
            ("Amara", "Strength 4, Speed 6, Health 10, Gold 7. Skills: none. Healer's apprentice; wants allies, offers rest and trade, fears blood."),
            ("Gus", "Strength 7, Speed 3, Health 12, Gold 2. Skills: none. Loud, greedy, cowardly when hurt, brave when winning."),
            ("Lio", "Strength 3, Speed 8, Health 9, Gold 5. Skills: none. Young, fast, reckless, wants a story worth telling."),
            ("Petra", "Strength 6, Speed 5, Health 11, Gold 4. Skills: none. Grudge-holder with a long memory and a longer plan."),
        ])],
    # names only, no stances
    "Blank: 2 agents": [_seat("Claude", CL), _seat("Codex", CX)],
    "Blank: 4 agents": [_seat("Claude A", CL), _seat("Codex A", CX), _seat("Claude B", CL), _seat("Codex B", CX)],
    "Blank: 6 agents": [_seat("Claude A", CL), _seat("Codex A", CX), _seat("Claude B", CL), _seat("Codex B", CX), _seat("Claude C", CL), _seat("Codex C", CX)],
    "Blank: 8 agents": [_seat(f"Claude {c}", CL) if i % 2 == 0 else _seat(f"Codex {c}", CX) for i, c in enumerate("AABBCCDD")],
    "Blank: 5 Claude + 5 Codex": [_seat(f"Claude {n}", CL) for n in "12345"] + [_seat(f"Codex {n}", CX) for n in "12345"],
}

# ---------------------------------------------------------------- user templates
USER_TEMPLATES_FILE = HERE.with_name("agora_templates.json")


def user_templates() -> dict:
    try: return json.loads(USER_TEMPLATES_FILE.read_text(encoding="utf-8")) if USER_TEMPLATES_FILE.exists() else {}
    except Exception: return {}


def save_user_template(name: str, data: dict) -> None:
    t = user_templates(); t[name] = data
    USER_TEMPLATES_FILE.write_text(json.dumps(t, indent=1), encoding="utf-8")


def delete_user_template(name: str) -> None:
    t = user_templates(); t.pop(name, None)
    USER_TEMPLATES_FILE.write_text(json.dumps(t, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- codex trust
def ensure_codex_trust(folder: str) -> str | None:
    """Codex's non-interactive mode refuses folders not marked trusted in ~/.codex/config.toml.
    Add the entry if missing. Returns a note if something was changed, else None."""
    try:
        cfg = Path.home() / ".codex" / "config.toml"
        cfg.parent.mkdir(exist_ok=True)
        text = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
        key = str(Path(folder).resolve())
        variants = {key, key.replace("\\", "/"), key.replace("\\", "\\\\")}
        if any(f'[projects."{v}"]' in text or f"[projects.'{v}']" in text for v in variants): return None
        if cfg.exists(): cfg.with_suffix(".toml.agora-backup").write_text(text, encoding="utf-8")
        esc = key.replace("\\", "\\\\")
        with cfg.open("a", encoding="utf-8") as fh:
            fh.write(f'\n[projects."{esc}"]\ntrust_level = "trusted"\n')
        return f"Marked {key} as trusted for Codex in {cfg} (backup saved alongside)."
    except Exception as exc:  # noqa: BLE001
        return f"Could not update Codex trust config: {type(exc).__name__}"


# ---------------------------------------------------------------- versions
VERSIONS: dict[str, dict] = {}     # provider -> {"installed": str, "latest": str}
_ver_lock = threading.Lock()


def _ver_of(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=25, shell=(os.name == "nt"))
        out = (r.stdout or r.stderr or "").strip().splitlines()
        return out[-1].strip() if out else ""
    except Exception: return ""


def refresh_versions() -> None:
    """Installed versions now, latest from npm in the background. Never blocks the UI."""
    def work() -> None:
        for name, prov in PROVIDERS.items():
            if prov.get("isolated"): continue
            inst = _ver_of([prov["exe"], "--version"]) if shutil.which(prov["exe"]) else ""
            with _ver_lock: VERSIONS.setdefault(name, {})["installed"] = inst
        for name, prov in PROVIDERS.items():
            if prov.get("isolated") or not prov.get("pkg") or not shutil.which("npm"): continue
            latest = _ver_of(["npm", "view", prov["pkg"], "version"])
            with _ver_lock: VERSIONS.setdefault(name, {})["latest"] = latest
    threading.Thread(target=work, daemon=True).start()


# ---------------------------------------------------------------- telegram
TG_FILE = HERE.with_name("agora_telegram.json")


def tg_config() -> dict:
    if not TG_FILE.exists():
        TG_FILE.write_text(json.dumps({"token": "", "chat_id": "", "notify_on_finish": True}, indent=1), encoding="utf-8")
        return {"token": "", "chat_id": "", "notify_on_finish": True}
    try: return json.loads(TG_FILE.read_text(encoding="utf-8"))
    except Exception: return {"token": "", "chat_id": "", "notify_on_finish": True}


def _tg_call(token: str, method: str, payload: dict | None = None) -> dict:
    import urllib.request
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                 data=json.dumps(payload or {}).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def tg_send(text: str) -> str:
    """Send a message through the configured bot. Returns a human-readable status. Never logs the token.
    Deliberately never calls getUpdates: if another program already polls this bot, two pollers conflict."""
    cfg = tg_config()
    token = (cfg.get("token") or "").strip(); chat = str(cfg.get("chat_id") or "").strip()
    if not token: return "No bot token. Put your Telegram bot token in agora_telegram.json next to agora.py."
    if not chat: return "No chat id. Put your Telegram chat id in agora_telegram.json (message @userinfobot on Telegram and it replies with your id)."
    try:
        res = _tg_call(token, "sendMessage", {"chat_id": chat, "text": text, "disable_web_page_preview": True})
        return "Sent to Telegram." if res.get("ok") else f"Telegram refused: {res.get('description', 'unknown error')}"
    except Exception as exc:  # noqa: BLE001
        return f"Telegram error: {type(exc).__name__}"


# ---------------------------------------------------------------- sessions
def now_id() -> str:
    base = dt.datetime.now().strftime("%Y%m%d-%H%M%S"); sid = base; n = 2
    while (SESSIONS / sid).exists(): sid = f"{base}-{n}"; n += 1
    return sid


class Session:
    """One conversation: its agents, transcript, and per-agent memory."""

    def __init__(self, sid: str, data: dict | None = None) -> None:
        self.id = sid
        self.dir = SESSIONS / sid; self.dir.mkdir(parents=True, exist_ok=True)
        d = data or {}
        self.title = d.get("title", "")
        self.created = d.get("created", dt.datetime.now().isoformat(timespec="minutes"))
        self.repo = d.get("repo", DEFAULT_REPO)
        self.seats = color_seats(d.get("seats", [
            {"name": "Claude", "provider": "Claude Code", "model": "claude-fable-5-1", "stance": ""},
            {"name": "Codex", "provider": "Codex (latest)", "model": "gpt-6-astra", "stance": ""},
        ]))
        self.topic = d.get("topic", DEFAULT_TOPIC); self.extra = d.get("extra", "")
        self.rounds = d.get("rounds", 3); self.readonly = d.get("readonly", True)
        self.mode = d.get("mode", "turns")   # turns | open
        self.framing = d.get("framing", "council")   # council | game
        self.referee = d.get("referee", "")          # agent name whose messages wake everyone; others wake only it + mentions
        self.max_messages = int(d.get("max_messages") or 0)   # 0 = no limit (open floor)
        self.max_minutes = int(d.get("max_minutes") or 0)     # 0 = no limit (open floor)
        self.started_at = d.get("started_at", 0.0)
        self.transcript = d.get("transcript", []); self.turn = d.get("turn", 0)
        self.status = d.get("status", "idle")
        if self.status in ("running", "voting"): self.status = "paused"
        self.rounds_done = d.get("rounds_done", 0)
        self.skipped = d.get("skipped", [False] * len(self.seats))
        self.cli_sessions = d.get("cli_sessions", {})       # seat index -> CLI session id (Claude resume)
        self.last_seen = d.get("last_seen", {})             # seat index -> transcript length when it last spoke

    def to_dict(self) -> dict:
        return {"title": self.title, "created": self.created, "repo": self.repo, "seats": self.seats,
                "topic": self.topic, "extra": self.extra, "rounds": self.rounds, "readonly": self.readonly, "mode": self.mode,
                "max_messages": self.max_messages, "max_minutes": self.max_minutes, "started_at": self.started_at, "framing": self.framing, "referee": self.referee,
                "transcript": self.transcript, "turn": self.turn, "status": self.status,
                "rounds_done": self.rounds_done, "skipped": self.skipped,
                "cli_sessions": self.cli_sessions, "last_seen": self.last_seen}

    def save(self, transcript_md: bool = False) -> None:
        (self.dir / "session.json").write_text(json.dumps(self.to_dict()), encoding="utf-8")
        if not transcript_md: return
        md = [f"# {self.title or 'Conversation'}", f"Created: {self.created}", f"Folder: {self.repo}", "",
              "Agents:"] + [f"- {s['name']}: {s['provider']} ({s['model']})" for s in self.seats] + ["", f"Topic: {self.topic}", self.extra, ""]
        for e in self.transcript: md += [f"## Turn {e['turn']}: {e['speaker']} ({e['kind']})", "", e["text"], ""]
        (self.dir / "transcript.md").write_text("\n".join(md), encoding="utf-8")

    @classmethod
    def load(cls, sid: str) -> "Session | None":
        f = SESSIONS / sid / "session.json"
        if not f.exists(): return None
        try: return cls(sid, json.loads(f.read_text(encoding="utf-8")))
        except Exception: return None

    def export_md(self, what: str = "all") -> str:
        """Markdown export. what: all | closing | speeches (no live-terminal noise, no system notes)."""
        keep = {"all": None, "closing": {"resolution"}, "speeches": {"speech", "resolution", "convener"}}.get(what)
        md = [f"# {self.title or 'Conversation'}", f"Created: {self.created}  ", f"Mode: {'open floor' if self.mode == 'open' else 'take turns'}  ", f"Folder: {self.repo}", "",
              "## Agents", ""] + [f"- **{s['name']}** ({s['provider']}, {s['model']})" + (f": {s['stance']}" if s.get('stance') else "") for s in self.seats]
        md += ["", "## Topic", "", self.topic, ""]
        if self.extra.strip(): md += ["## Extra instructions", "", self.extra, ""]
        md += ["## " + {"closing": "Closing statements", "speeches": "Conversation", "all": "Full record"}.get(what, "Full record"), ""]
        for e in self.transcript:
            if keep is not None and e["kind"] not in keep: continue
            tag = {"resolution": " (closing statement)", "convener": " (convener)", "system": " (Agora)"}.get(e["kind"], "")
            md += [f"### {e['speaker']}{tag}  ", f"*turn {e['turn']} · {e['time']}*", "", e["text"], ""]
        return "\n".join(md)

    def seat_dir(self, i: int) -> Path:
        d = self.dir / f"agent{i + 1}"; d.mkdir(exist_ok=True)
        m = d / "memory.md"
        if not m.exists():
            who = self.label(self.seats[i]) if i < len(self.seats) else f"agent {i + 1}"
            m.write_text(f"# {who}: memory of this conversation\n\n(Nothing has been said yet.)\n", encoding="utf-8")
        return d

    def label(self, seat: dict) -> str:
        return seat.get("name") or f"{seat['provider']} ({seat['model']})"


_SESS_CACHE: dict[str, tuple[float, dict]] = {}


def list_sessions() -> list[dict]:
    """Parses a session.json only when its mtime changed, so polling stays cheap."""
    out = []
    for f in sorted(SESSIONS.glob("*/session.json"), reverse=True):
        try:
            mt = f.stat().st_mtime; hit = _SESS_CACHE.get(str(f))
            if hit and hit[0] == mt: out.append(hit[1]); continue
            d = json.loads(f.read_text(encoding="utf-8"))
            meta = {"id": f.parent.name, "title": d.get("title") or " ".join(d.get("topic", "").split()[:8]) or "Untitled",
                    "created": d.get("created", ""), "status": d.get("status", "idle"), "turns": len(d.get("transcript", []))}
            _SESS_CACHE[str(f)] = (mt, meta); out.append(meta)
        except Exception: continue
    return out


# ---------------------------------------------------------------- whispers
def whisper_target(text: str) -> str | None:
    """'WHISPER @Name: ...' at the start of a message makes it private to Name and the referee."""
    import re as _re
    m = _re.match(r"\s*WHISPER\s+@([A-Za-z][\w -]*?)\s*:", text, _re.I)
    return m.group(1).strip() if m else None


def can_see(entry: dict, viewer: str, referee: str) -> bool:
    t = whisper_target(entry.get("text", ""))
    if not t: return True
    return viewer.lower() in (t.lower(), (referee or "").lower(), entry.get("speaker", "").lower())


# ---------------------------------------------------------------- engine
class Run:
    """The engine for one conversation: its own agents, terminals, processes, flags, and thread."""

    def __init__(self, session: "Session", agora: "Agora") -> None:
        self.s = session; self.agora = agora
        self.lock = threading.RLock()
        self.terms: list[dict] = []
        self.current: str | None = None
        self.procs: dict[int, subprocess.Popen] = {}
        self.speaking: set[int] = set()
        self.thread: threading.Thread | None = None
        self.stop_flag, self.pause_flag, self.vote_flag = threading.Event(), threading.Event(), threading.Event()
        self._reset_terms()

    def _reset_terms(self) -> None:
        self.terms = [{"lines": collections.deque(maxlen=TAIL), "state": "waiting", "count": 0} for _ in self.s.seats]


    def busy(self) -> bool: return self.s.status in ("running", "voting")



    def start(self) -> None:
        with self.lock:
            s = self.s
            if self.busy() or len(s.seats) < 2 or not Path(s.repo).is_dir(): return
            if s.status == "paused" and self.thread and self.thread.is_alive():
                self.pause_flag.clear(); s.status = "running"; s.save(); return
            if s.status == "done": s.rounds_done = sum(1 for e in s.transcript if e["kind"] == "speech") // max(1, len(s.seats))
            if s.status in ("done", "stopped"): s.skipped = [False] * len(s.seats)   # benched seats get another chance on Continue
            for f in (self.stop_flag, self.pause_flag, self.vote_flag): f.clear()
            if not s.title: s.title = " ".join(s.topic.split()[:8])
            s.status = "running"; s.started_at = time.time(); s.save()
            for i in range(len(s.seats)): s.seat_dir(i)   # memory files exist before anyone speaks
            if any(PROVIDERS[x["provider"]]["exe"] in ("codex", "npx") for x in s.seats):
                note = ensure_codex_trust(s.repo)
                if note: self._record("Agora", note, "system")
            if not self.terms: self._reset_terms()
        self.thread = threading.Thread(target=self._run, daemon=True); self.thread.start()


    def pause(self) -> None:
        with self.lock:
            if self.s.status == "running": self.pause_flag.set(); self.s.status = "paused"; self.s.save()


    def _kill_tree(self) -> None:
        """Kill every CLI this run started, all at once, without blocking the caller."""
        def kill(p: subprocess.Popen) -> None:
            try:
                if os.name == "nt": subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, timeout=15)
                else: os.killpg(os.getpgid(p.pid), 9)
            except Exception:
                try: p.kill()
                except Exception: pass
        for p in list(self.procs.values()):
            if p and p.poll() is None: threading.Thread(target=kill, args=(p,), daemon=True).start()


    def stop(self) -> None:
        self.stop_flag.set(); self.pause_flag.clear(); self._kill_tree()
        with self.lock:
            for t in self.terms: t["state"] = "waiting"


    def call_vote(self) -> None: self.vote_flag.set(); self.pause_flag.clear()


    def _notify_finished(self) -> None:
        cfg = tg_config()
        if not cfg.get("notify_on_finish", True) or not (cfg.get("token") or "").strip(): return
        link = self.agora.away_url or self.agora.phone_url or ""
        n = sum(1 for e in self.s.transcript if e["kind"] == "speech")
        threading.Thread(target=tg_send, args=(f"Agora: '{self.s.title or 'conversation'}' finished with {n} messages and closing statements.\n{link}",), daemon=True).start()


    def say(self, text: str, target: str = "") -> None:
        text = (text or "").strip()
        if not text: return
        if target: text = f"To {target}: {text}"
        self._record("Anthony", text, "convener")



    def _term(self, i: int, text: str) -> None:
        with self.lock:
            if i >= len(self.terms): return
            for ln in text.split("\n"):
                self.terms[i]["lines"].append(ln); self.terms[i]["count"] += 1


    def _record(self, speaker: str, text: str, kind: str) -> None:
        with self.lock:
            s = self.s; s.turn += 1
            color = next((x.get("color") for x in s.seats if s.label(x) == speaker), None)
            e = {"turn": s.turn, "speaker": speaker, "text": text, "kind": kind, "time": dt.datetime.now().strftime("%H:%M:%S"), "color": color}
            s.transcript.append(e); seats = list(s.seats); ref = s.referee
        for i, seat in enumerate(seats):  # every agent gets its own copy (whispers only to those allowed); outside the lock
            who = s.label(seat); line = "You said" if speaker == who else f"{speaker} said"
            if not can_see(e, who, ref): continue
            with (s.seat_dir(i) / "memory.md").open("a", encoding="utf-8") as fh:
                fh.write(f"\n## Turn {e['turn']} ({e['time']}), {line}:\n{text}\n")
        s.save()


    def _prompt(self, i: int, seat: dict, instruction: str) -> str:
        s = self.s; me = s.label(seat)
        others = ", ".join(f"{s.label(x)} ({x['provider']}, {x['model']})" for x in s.seats if x is not seat)
        game = s.framing == "game"
        parts = [GAME_FRAMING if game else FRAMING, f"\n{'The arena and its rules' if game else 'Topic'}:\n{s.topic}\n"]
        if s.extra.strip(): parts.append(f"Additional instructions from the convener:\n{s.extra}\n")
        if game:
            parts.append(f"You are {me}. The other characters: {', '.join(s.label(x) for x in s.seats if x is not seat)}.\n")
            if seat.get("stance"): parts.append(f"Your character sheet: {seat['stance']}\n")
        else:
            parts.append(f"You are {me}, running as {seat['provider']} with model {seat['model']}. The others: {others}.\n")
            if seat.get("stance"): parts.append(f"Your assigned stance or role: {seat['stance']}\n")
        parts.append(f"Your memory file (yours alone, full history of this conversation): {s.seat_dir(i) / 'memory.md'}\n")
        seen = int(s.last_seen.get(str(i), 0)); new = [e for e in s.transcript[seen:] if can_see(e, me, s.referee)]
        if new:
            parts.append(f"{len(new)} message(s) since your last turn, all of them new to you. " + ("These are things you saw and heard in the world; every ACTION below is something that person did, in front of you if you were there. Do not say nothing has happened; it has, and it is listed here:\n" if game else "Do not say nothing new has been said; it is listed here:\n"))
            for e in new: parts.append(f"--- {e['speaker']} (turn {e['turn']}) ---\n{e['text']}\n")
        elif s.transcript: parts.append("Nothing has happened since your last turn.\n" if game else "Nothing new has been said since your last turn.\n")
        parts.append(f"Your instruction now:\n{instruction}")
        return "\n".join(parts)


    def _command(self, i: int, seat: dict, prompt_file: Path) -> str:
        s = self.s; prov = PROVIDERS[seat["provider"]]
        cmd = prov["ro_cmd" if s.readonly else "cmd"].format(ask=ASK.format(prompt_file=prompt_file), model=seat["model"])
        sid = s.cli_sessions.get(str(i))
        if sid and prov.get("resume"): cmd += prov["resume"].format(sid=sid)
        return cmd


    def _set_current(self) -> None:
        names = [self.s.label(self.s.seats[j]) for j in sorted(self.speaking) if j < len(self.s.seats)]
        self.current = ", ".join(names) if names else None


    def _speak(self, i: int, seat: dict, instruction: str) -> str:
        s = self.s; who = s.label(seat); prov = PROVIDERS[seat["provider"]]
        if self.stop_flag.is_set(): return f"[{who} was not asked: the conversation was stopped]"
        if shutil.which(prov["exe"]) is None:
            self._term(i, f"'{prov['exe']}' is not installed or not on PATH")
            return f"[{who} returned no answer. '{prov['exe']}' is not installed or not on PATH]"
        n = s.turn + 1
        pfile = s.seat_dir(i) / f"prompt_{n:03d}_{int(time.time() * 1000) % 100000}.md"; pfile.write_text(self._prompt(i, seat, instruction), encoding="utf-8")
        cmd = self._command(i, seat, pfile)
        with self.lock: self.terms[i]["state"] = "speaking"; self.speaking.add(i); self._set_current()
        self._term(i, "=" * 60 + f"\n{who}: turn {n}\n" + "=" * 60)
        stdout_lines: list[str] = []; speech: str | None = None; new_sid: str | None = None; err_tail: list[str] = []; rc: int | None = None
        try:
            proc = subprocess.Popen(cmd, cwd=s.repo, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8", errors="replace", start_new_session=(os.name != "nt"))
            self.procs[i] = proc
            def pump_err(p: subprocess.Popen) -> None:
                for ln in p.stderr:  # type: ignore[union-attr]
                    err_tail.append(ln.rstrip("\n")); del err_tail[:-8]; self._term(i, ln.rstrip("\n"))
            threading.Thread(target=pump_err, args=(proc,), daemon=True).start()
            for ln in proc.stdout:  # type: ignore[union-attr]
                stdout_lines.append(ln)
                if prov["speech"] == "claude_stream":
                    try:
                        ev = json.loads(ln)
                        if ev.get("session_id"): new_sid = ev["session_id"]
                    except Exception: pass
                    shown, final = render_claude_event(ln)
                    if shown: self._term(i, shown)
                    if final is not None: speech = final
                else: self._term(i, ln.rstrip("\n"))
            rc = proc.wait(timeout=TURN_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.procs[i].kill(); self._term(i, f"[timed out after {TURN_TIMEOUT}s]")
        except Exception as exc:  # noqa: BLE001
            self._term(i, f"[agora error while reading output: {type(exc).__name__}: {exc}]")
        with self.lock:
            self.terms[i]["state"] = "waiting"; self.speaking.discard(i); self._set_current()
            if new_sid: s.cli_sessions[str(i)] = new_sid
        self._term(i, "\ndone. waiting for the next turn.\n")
        if speech is None and prov["speech"] != "claude_stream": speech = "".join(stdout_lines).strip()
        if speech is None and prov["speech"] == "claude_stream":
            self._term(i, f"[ended without a final answer: exit code {rc}, {len(stdout_lines)} events]" + ("\n" + "\n".join(err_tail) if err_tail else ""))
        if speech: return speech
        with self.lock: tail = [ln for ln in list(self.terms[i]["lines"])[-14:] if ln.strip() and not ln.startswith("=") and ": turn " not in ln and "waiting for the next turn" not in ln]
        detail = "\n".join(tail[-6:]) or "no output at all; the command may not have started"
        if s.cli_sessions.pop(str(i), None): detail += "\n(dropped this agent's resumable CLI session; it will start fresh next turn)"
        return f"[{who} returned no answer. Last output from its terminal:]\n{detail}"


    def _run_open(self) -> None:
        """Open floor: after any message, every other agent may reply or pass, up to MAX_PARALLEL at once."""
        import re
        s = self.s; seats = s.seats; n = len(seats)
        MAX_PARALLEL = n            # no limit: everyone may compose at once
        cap = float("inf")          # no message cap; the floor closes when everyone passes
        queue: dict[int, str] = {}           # seat -> instruction kind: "open" | "addressed"
        failures: dict[int, int] = {}        # seat -> consecutive failed runs
        passed_on: dict[int, int] = {}       # seat -> transcript length it passed on
        last_speaker: int | None = None
        threads: dict[int, threading.Thread] = {}
        spoken = {i: sum(1 for e in s.transcript if e["speaker"] == s.label(seats[i]) and e["kind"] == "speech") for i in range(n)}
        ref_i = next((i for i, x in enumerate(seats) if s.referee and s.label(x).lower() == s.referee.lower()), None)
        if ref_i is not None and not s.transcript:
            queue[ref_i] = "open"                      # the referee opens; nobody acts before the world exists
        else:
            for i in range(n):
                if not s.skipped[i]: queue[i] = "open"
        last_end = {i: int(s.last_seen.get(str(i), 0)) for i in range(n)}   # where each seat's last prompt ended

        def mentioned(text: str) -> set[int]:
            hits = set()
            for i, x in enumerate(seats):
                nm = re.escape(s.label(x))
                if re.search(r"@" + nm + r"\b", text, re.I): hits.add(i)
                elif s.framing == "game" and re.search(r"(?<![\w@])" + nm + r"\b", text, re.I): hits.add(i)   # named in an action or in talk
            return hits

        def worker(i: int, kind: str) -> None:
            seat = seats[i]
            first = spoken[i] == 0 and (s.framing == "game" or not any(e["kind"] == "speech" for e in s.transcript))
            if s.framing == "game": instr = GAME_OPENING if first else GAME_REPLY
            else: instr = OPEN_OPENING if first else (OPEN_ADDRESSED if kind == "addressed" else OPEN_REPLY)
            text = self._speak(i, seat, instr)
            nonlocal last_speaker
            if text.strip().upper().rstrip(".") == "PASS" or text.strip().upper().startswith("PASS\n"):
                passed_on[i] = len(s.transcript); self._term(i, "(passed)")
                return
            if text.startswith("[") and "returned no answer" in text:
                failures[i] = failures.get(i, 0) + 1; passed_on[i] = len(s.transcript)
                if failures[i] == 1: self._record("Agora", text, "system")
                if failures[i] >= 2 and not s.skipped[i]:
                    s.skipped[i] = True; self._record("Agora", f"{s.label(seat)} failed twice in a row and is benched for the rest of this conversation. Fix its provider or model in Details; it rejoins on Continue.", "system")
                return
            failures[i] = 0
            self._record(s.label(seat), text, "speech"); spoken[i] += 1; last_speaker = i
            if closed["v"]: return
            with self.lock:
                wt = whisper_target(text)
                if ref_i is not None and i != ref_i:
                    # a player spoke: wake the referee, plus anyone addressed (whisper target or @mention)
                    queue[ref_i] = "open"
                    for j in mentioned(text):
                        if j != i and j != ref_i: queue[j] = "addressed"
                    if wt:
                        for j, x in enumerate(seats):
                            if s.label(x).lower() == wt.lower() and j != i: queue[j] = "addressed"
                else:
                    for j in range(n):
                        if j != i and not s.skipped[j]: queue[j] = "addressed" if j in mentioned(text) else queue.get(j, "open")
                    for j in mentioned(text):
                        if j != i: queue[j] = "addressed"

        seen_len = len(s.transcript); closed = {"v": False}
        while not self.stop_flag.is_set() and not self.vote_flag.is_set():
            while self.pause_flag.is_set() and not self.stop_flag.is_set(): time.sleep(0.5)
            # auto-close limits (live-editable): stop launching, let composing agents finish
            msgs = sum(1 for e in s.transcript if e["kind"] == "speech")
            over_msgs = s.max_messages and msgs >= s.max_messages
            over_time = s.max_minutes and (time.time() - (s.started_at or time.time())) >= s.max_minutes * 60
            if over_msgs or over_time:
                closed["v"] = True; queue.clear()
                for i in [i for i, t in threads.items() if not t.is_alive()]: threads.pop(i)
                if not threads:
                    self._record("Agora", f"The floor is closed: {'message limit' if over_msgs else 'time limit'} reached.", "system"); break
                time.sleep(0.5); continue
            # convener interjections wake everyone
            if len(s.transcript) != seen_len:
                newest = s.transcript[-1]
                if newest["kind"] == "convener":
                    with self.lock:
                        ms = mentioned(newest["text"])
                        if ref_i is not None and (ms or newest["text"].startswith("To ")):
                            queue[ref_i] = "open"
                            for j in ms:
                                if not s.skipped[j]: queue[j] = "addressed"
                        else:
                            for j in range(n):
                                if not s.skipped[j]: queue[j] = "addressed" if (j in ms or "To " not in newest["text"]) else queue.get(j, "open")
                seen_len = len(s.transcript)
            # reap finished threads
            for i in [i for i, t in threads.items() if not t.is_alive()]: threads.pop(i)
            # launch eligible speakers
            with self.lock:
                free = MAX_PARALLEL - len(threads)
                order = sorted(queue.keys(), key=lambda j: (queue[j] != "addressed", spoken[j], j))
                launch = []
                for j in order:
                    if free <= 0: break
                    if j in threads or s.skipped[j]: queue.pop(j, None); continue
                    launch.append((j, queue.pop(j))); free -= 1
                for j, kind in launch:
                    s.last_seen[str(j)] = last_end[j]          # prompt covers everything since this seat's previous prompt
                    last_end[j] = len(s.transcript)
            for j, kind in launch:
                t = threading.Thread(target=worker, args=(j, kind), daemon=True); threads[j] = t; t.start()
            # converged: nobody composing, nothing queued, everyone eligible has passed on the latest message
            eligible = [j for j in range(n) if not s.skipped[j]]
            if not threads and not queue:
                if not eligible or all(passed_on.get(j) == len(s.transcript) for j in eligible if j != last_speaker):
                    self._record("Agora", "Everyone has passed on the latest message; the floor is closed.", "system"); break
            time.sleep(0.5)
        for t in list(threads.values()): t.join(timeout=TURN_TIMEOUT)


    def _run(self) -> None:
        s = self.s; seats = list(s.seats)
        missing = [f"{s.label(x)}: '{PROVIDERS[x['provider']]['exe']}' is not installed or not on PATH" for x in seats if shutil.which(PROVIDERS[x["provider"]]["exe"]) is None]
        if missing:
            for e in missing: self._record("Agora", e, "system")
            with self.lock: s.status = "idle"; self.current = None; s.save()
            return
        failures = [0] * len(seats)
        if s.mode == "open":
            self._run_open()
        import re as _re
        def mentioned_in(text: str) -> list[int]:
            return [j for j, x in enumerate(s.seats) if _re.search(r"@" + _re.escape(s.label(x)) + r"\b", text, _re.I)]
        spoken = sum(1 for e in s.transcript if e["kind"] == "speech")
        target = 0 if s.mode == "open" else (s.rounds_done + s.rounds) * len(seats)
        k = spoken; rr = spoken; pull: list[int] = []; seen_len = len(s.transcript); last_i = None
        while k < target:
            while self.pause_flag.is_set() and not self.stop_flag.is_set(): time.sleep(0.5)
            if self.stop_flag.is_set() or self.vote_flag.is_set(): break
            # mentions in anything said since we last looked (agent speeches or your messages) jump the queue
            for e in s.transcript[seen_len:]:
                for j in mentioned_in(e["text"]):
                    if s.label(s.seats[j]) != e["speaker"] and j not in pull: pull.append(j)
            seen_len = len(s.transcript)
            if pull:
                i = pull.pop(0); instr = GAME_REPLY if s.framing == "game" else OPEN_ADDRESSED
            else:
                i = rr % len(seats); rr += 1
                if i == last_i and len(seats) > 1:  # rotation landed on whoever just spoke via a pull; skip ahead
                    i = rr % len(seats); rr += 1
                first_ever = not any(e["kind"] == "speech" for e in s.transcript)
                instr = (GAME_OPENING if first_ever else GAME_REPLY) if s.framing == "game" else (OPENING if first_ever else REPLY)
            k += 1
            seat = s.seats[i]
            if s.skipped[i]: continue
            last_i = i
            with self.lock: self.current = s.label(seat)
            text = self._speak(i, seat, instr)
            s.last_seen[str(i)] = len(s.transcript)
            self._record(s.label(seat), text, "speech")
            if text.startswith("[") and "returned no answer" in text:
                failures[i] += 1
                if failures[i] >= 2:
                    s.skipped[i] = True; self._record("Agora", f"{s.label(seat)} failed twice and is skipped for the rest of this conversation.", "system")
            else: failures[i] = 0
        if not self.stop_flag.is_set():
            with self.lock: s.status = "voting"; s.save()
            for i in range(len(seats)):
                seat = s.seats[i]
                if self.stop_flag.is_set() or s.skipped[i]: continue
                with self.lock: self.current = s.label(seat)
                text = self._speak(i, seat, GAME_VOTE if s.framing == "game" else VOTE); s.last_seen[str(i)] = len(s.transcript)
                self._record(s.label(seat), text, "system" if (text.startswith("[") and "returned no answer" in text) else "resolution")
        with self.lock:
            self.current = None; s.status = "stopped" if self.stop_flag.is_set() else "done"
            s.rounds_done = sum(1 for e in s.transcript if e["kind"] == "speech") // max(1, len(seats)); s.save(transcript_md=True)
        if s.status == "done": self._notify_finished()



class Agora:
    """Manages conversations. Every conversation has its own Run; any number may be live at once."""

    def __init__(self) -> None:
        SESSIONS.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.runs: dict[str, Run] = {}
        first = self._open_latest()
        self.sid = first.id; self.runs[first.id] = Run(first, self)
        self.phone_url = ""; self.away_url = ""; self.last_tg = ""
        refresh_versions()

    def _open_latest(self) -> Session:
        for meta in list_sessions():
            sess = Session.load(meta["id"])
            if sess: return sess
        return Session(now_id())

    @property
    def run(self) -> Run:
        return self.runs[self.sid]

    @property
    def s(self) -> Session:
        return self.run.s

    def _get_run(self, sid: str) -> Run | None:
        if sid in self.runs: return self.runs[sid]
        sess = Session.load(sid)
        if not sess: return None
        self.runs[sid] = Run(sess, self); return self.runs[sid]

    def busy(self) -> bool: return self.run.busy()

    def snapshot(self, since: int = -1, terms: bool = False, tail: int = 150) -> dict:
        """Light by default: transcript only after `since` (turn), terminal tails only when asked. Lock held only to copy."""
        r = self.run
        with r.lock:
            s = r.s
            live = [rid for rid, x in self.runs.items() if x.busy()]
            tr = [e for e in s.transcript if e["turn"] > since] if since >= 0 else list(s.transcript)
            tstates = [{"state": t["state"], "count": t["count"], "lines": list(t["lines"])[-tail:] if terms else []} for t in r.terms]
            return {"id": s.id, "title": s.title, "seats": list(s.seats), "topic": s.topic, "extra": s.extra, "transcript_total": len(s.transcript), "last_turn": s.turn,
                    "rounds": s.rounds, "readonly": s.readonly, "mode": s.mode,
                    "max_messages": s.max_messages, "max_minutes": s.max_minutes, "started_at": s.started_at, "framing": s.framing, "referee": s.referee, "transcript": tr, "status": s.status,
                    "current": r.current, "turn": s.turn, "repo": s.repo, "repo_ok": Path(s.repo).is_dir(),
                    "rounds_done": s.rounds_done, "sessions": list_sessions(), "live": live,
                    "terms": tstates,
                    "phone_url": self.phone_url, "away_url": self.away_url, "last_tg": self.last_tg,
                    "tg_ready": bool((tg_config().get("token") or "").strip()),
                    "templates": list(TEMPLATES.keys()), "user_templates": list(user_templates().keys()), "sessions_dir": str(SESSIONS), "build": BUILD,
                    "providers": {k: {"models": v["models"], "installed": shutil.which(v["exe"]) is not None,
                                      "isolated": bool(v.get("isolated")), "pkg": v.get("pkg", ""),
                                      "version": VERSIONS.get(k, {}).get("installed", ""), "latest": VERSIONS.get(k, {}).get("latest", "")}
                                  for k, v in PROVIDERS.items()}}

    def new_session(self) -> None:
        with self.lock:
            sess = Session(now_id()); sess.save(); self.runs[sess.id] = Run(sess, self); self.sid = sess.id

    def open_session(self, sid: str) -> None:
        with self.lock:
            if self._get_run(sid): self.sid = sid

    def delete_session(self, sid: str) -> None:
        with self.lock:
            r = self.runs.get(sid)
            if r and r.busy(): return
            self.runs.pop(sid, None); shutil.rmtree(SESSIONS / sid, ignore_errors=True)
            if sid == self.sid:
                sess = self._open_latest(); self.runs.setdefault(sess.id, Run(sess, self)); self.sid = sess.id

    def save_template(self, name: str) -> None:
        name = (name or "").strip()
        if not name: return
        s = self.s
        save_user_template(name, {"seats": [dict(x) for x in s.seats], "framing": s.framing, "mode": s.mode, "rounds": s.rounds,
                                  "readonly": s.readonly, "topic": s.topic, "extra": s.extra, "repo": s.repo,
                                  "max_messages": s.max_messages, "max_minutes": s.max_minutes})

    def apply_template(self, name: str) -> None:
        r = self.run
        with r.lock:
            if r.busy() or r.s.transcript: return
            ut = user_templates()
            if name in ut:   # a saved layout: restore everything
                d = ut[name]; s = r.s
                s.seats = color_seats([dict(x) for x in d.get("seats", [])]); s.skipped = [False] * len(s.seats)
                for k in ("framing", "mode", "rounds", "readonly", "topic", "extra", "repo", "max_messages", "max_minutes"):
                    if k in d: setattr(s, k, d[k])
                r._reset_terms(); s.save(); return
            if name not in TEMPLATES: return
            seats = [dict(x) for x in TEMPLATES[name]]
            if name.startswith("Arena"):
                for x in seats: x["model"] = "claude-haiku-4-5" if x["provider"] == CL else "gpt-5.5"
                r.s.framing = "game"; r.s.mode = "turns"; r.s.rounds = 6
                r.s.topic = ("A walled arena with a market stall, a training yard, and a healer's tent. Twenty strangers, "
                             "one season. Gold buys goods and favors, training raises skills, fights cost health, and the dead "
                             "stay dead. Every six rounds the World holds a vote: the character the others trust least is exiled. "
                             "Win by being alive, rich, or beloved when the season ends.")
            else:
                r.s.framing = "council"
            r.s.seats = color_seats(seats); r.s.skipped = [False] * len(r.s.seats)
            r._reset_terms(); r.s.save()


    def configure(self, d: dict) -> None:
        r = self.run
        with r.lock:
            s = r.s
            if r.busy():
                d = {k: v for k, v in d.items() if k in ("seats", "extra", "max_messages", "max_minutes")}
                if "seats" in d and len(d["seats"]) != len(s.seats): return
            if "seats" in d:
                seats = []
                for x in d["seats"]:
                    if x.get("provider") in PROVIDERS and x.get("model"):
                        seats.append({"name": (x.get("name") or "").strip(), "provider": x["provider"], "model": x["model"].strip(), "stance": (x.get("stance") or "").strip(), "color": (x.get("color") or "").strip()})
                color_seats(seats)
                if not s.transcript and not self.busy():  # before the first turn anything goes
                    s.seats = seats; s.skipped = [False] * len(seats); r._reset_terms()
                elif len(seats) == len(s.seats):  # mid-conversation: names fixed, engine swappable
                    for i, (old, new) in enumerate(zip(s.seats, seats)):
                        if (old["provider"], old["model"]) != (new["provider"], new["model"]):
                            s.cli_sessions.pop(str(i), None)
                            with (s.seat_dir(i) / "memory.md").open("a", encoding="utf-8") as fh:
                                fh.write(f"\n(Note: from here on you are running as {new['provider']} with model {new['model']}; the earlier turns above were yours under {old['provider']} {old['model']}.)\n")
                        old["provider"], old["model"], old["stance"], old["color"] = new["provider"], new["model"], new["stance"], new.get("color") or old.get("color")
            for k in ("topic", "extra", "title"):
                if k in d: setattr(s, k, d[k])
            if "rounds" in d: s.rounds = max(1, int(d["rounds"]))
            if "readonly" in d: s.readonly = bool(d["readonly"])
            if d.get("mode") in ("turns", "open"): s.mode = d["mode"]
            if d.get("framing") in ("council", "game"): s.framing = d["framing"]
            if "referee" in d: s.referee = (d["referee"] or "").strip()
            if "max_messages" in d: s.max_messages = max(0, int(d["max_messages"] or 0))
            if "max_minutes" in d: s.max_minutes = max(0, int(d["max_minutes"] or 0))
            if d.get("repo"): s.repo = d["repo"]
            if not s.title: s.title = " ".join(s.topic.split()[:8])
            if s.framing == "game" and not s.referee and any(s.label(x).lower() == "world" for x in s.seats): s.referee = next(s.label(x) for x in s.seats if s.label(x).lower() == "world")
            s.save()


    def start(self) -> None: self.run.start()
    def pause(self) -> None: self.run.pause()
    def stop(self) -> None: self.run.stop()
    def call_vote(self) -> None: self.run.call_vote()
    def say(self, text: str, target: str = "") -> None: self.run.say(text, target)

    def shutdown(self) -> None:
        for r in list(self.runs.values()): r.stop()
        def die() -> None:
            if os.name == "nt":
                # close the console window that launched us (cmd/PowerShell/the .bat), not just this process
                try:
                    ppid = subprocess.run(["powershell", "-NoProfile", "-Command",
                                           f"(Get-CimInstance Win32_Process -Filter 'ProcessId={os.getpid()}').ParentProcessId"],
                                          capture_output=True, text=True, timeout=10).stdout.strip()
                    if ppid.isdigit():
                        subprocess.Popen(["cmd", "/c", f"timeout /t 1 /nobreak >nul & taskkill /PID {ppid} /T /F"],
                                         creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0))
                except Exception: pass
            os._exit(0)
        threading.Timer(0.8, die).start()


    def send_link(self) -> str:
        link = self.away_url or self.phone_url
        if not link: return "No reachable link to send (started with --local-only, or no network address)."
        msg = f"Agora is running.\nOpen on your phone: {link}"
        if self.away_url and self.phone_url and self.away_url != self.phone_url: msg += f"\nHome wifi only: {self.phone_url}"
        self.last_tg = tg_send(msg); return self.last_tg


def list_dir(path: str) -> dict:
    p = Path(path or Path.home()).expanduser()
    if not p.is_dir(): p = Path.home()
    try: subs = sorted([c.name for c in p.iterdir() if c.is_dir() and not c.name.startswith(".")], key=str.lower)
    except PermissionError: subs = []
    drives = [f"{d}:\\" for d in "CDEFGH" if os.name == "nt" and Path(f"{d}:\\").exists()]
    return {"path": str(p), "parent": str(p.parent) if p.parent != p else None, "dirs": subs, "drives": drives}


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"><title>Agora</title><link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%2309090B'/%3E%3Cg fill='%2322D3EE'%3E%3Ccircle cx='16' cy='16' r='3'/%3E%3Ccircle cx='16' cy='5' r='2'/%3E%3Ccircle cx='23.8' cy='8.2' r='2'/%3E%3Ccircle cx='27' cy='16' r='2'/%3E%3Ccircle cx='23.8' cy='23.8' r='2'/%3E%3Ccircle cx='16' cy='27' r='2'/%3E%3Ccircle cx='8.2' cy='23.8' r='2'/%3E%3Ccircle cx='5' cy='16' r='2'/%3E%3Ccircle cx='8.2' cy='8.2' r='2'/%3E%3C/g%3E%3C/svg%3E"><meta name="theme-color" content="#09090B">
<style>
/* Design tokens. Surfaces step up from bg; --line is for decorative separators only,
   --edge is for interactive control boundaries (3:1 on bg). --faint is the lowest
   text color allowed and clears 4.5:1 on every surface. Two radii: --r-ctl for
   controls, --r-card for cards and panels. Controls are 40px tall, 44px on touch. */
:root{--bg:#09090B;--s1:#101012;--s2:#17171A;--s3:#1E1E22;--line:#232327;--edge:#666672;--text:#EDEDEF;--muted:#8B8B94;--faint:#84848E;--accent:#22D3EE;--accent-ink:#06282E;--danger:#E5484D;--mono:#C8C8CF;--r-ctl:8px;--r-card:12px;--ctl:40px;--focus:0 0 0 2px var(--bg),0 0 0 4px var(--accent)}
*{box-sizing:border-box}html,body{height:100%}
:root{--rail:260px;--terms:min(46vw,760px);--fs:14px;--tfs:12px}
body{margin:0;background:var(--bg);color:var(--text);font:var(--fs)/1.5 Inter,"Segoe UI",system-ui,-apple-system,sans-serif;display:grid;grid-template-columns:var(--rail) 1fr auto;grid-template-rows:52px 1fr;overflow:hidden}
body.norail{grid-template-columns:0 1fr auto}body.noterm #terms{display:none}body.autoterm #terms{display:none}
button{font:inherit;color:var(--text);background:transparent;border:1px solid transparent;border-radius:var(--r-ctl);padding:0 12px;height:var(--ctl);cursor:pointer;white-space:nowrap;flex-shrink:0}
button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,.hitem:focus-visible{outline:none;box-shadow:var(--focus)}
button:hover{background:var(--s2)}button:disabled{cursor:default;color:var(--faint);background:var(--s2);border-color:var(--line);opacity:1}button.primary:disabled{background:var(--s3);color:var(--faint);border-color:var(--edge)}button.icon:disabled{background:transparent}
.btn{background:var(--s2);border-color:var(--edge)}.btn:hover{background:var(--s3)}
.primary{background:var(--accent);color:var(--accent-ink);border-color:var(--accent);font-weight:600}.btn.on{border-color:var(--accent);color:var(--accent)}.primary:hover{background:#4FE0F5}
.icon{width:var(--ctl);height:var(--ctl);padding:0;display:inline-grid;place-items:center;color:var(--muted)}.icon:hover{color:var(--text)}
header{grid-column:1/4;display:flex;align-items:center;gap:10px;padding:0 14px;border-bottom:1px solid var(--line);background:var(--s1)}
header .brand{display:inline-flex;align-items:center;gap:8px;font-weight:700;letter-spacing:.3px;margin-right:6px;color:var(--text)}header .brand .mark{color:var(--accent)}
.av{display:inline-grid;place-items:center;width:22px;height:22px;border-radius:50%;font-size:11px;font-weight:700;color:var(--c,var(--accent));background:color-mix(in srgb,var(--c,var(--accent)) 18%,transparent);border:1px solid color-mix(in srgb,var(--c,var(--accent)) 45%,transparent);flex-shrink:0}
.av.on{box-shadow:0 0 0 0 var(--c,var(--accent));animation:ring 1.6s ease-out infinite}
@keyframes ring{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--c,var(--accent)) 55%,transparent)}100%{box-shadow:0 0 0 8px transparent}}
.sheetwrap{display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:30;justify-content:flex-end}.sheetwrap.open{display:flex}
.sg{padding:14px 0;border-bottom:1px solid var(--line)}.sg:last-child{border:0}.sg h2{margin:0 0 10px;font-size:12.5px;color:var(--muted);font-weight:600}
.kv{display:flex;align-items:center;gap:10px;padding:6px 0;font-size:13.5px}.kv>span:first-child{flex:0 0 130px;color:var(--muted)}.kv code{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font:12px ui-monospace,Consolas,monospace;color:var(--text);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:6px 8px}
.ringart{width:140px;height:140px;margin:0 auto 18px;color:var(--accent);opacity:.9}
header .title{color:var(--muted);flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pill{display:inline-flex;align-items:center;gap:8px;padding:5px 11px;border-radius:999px;background:var(--s2);color:var(--muted);font-size:12.5px;white-space:nowrap;max-width:40vw;overflow:hidden;text-overflow:ellipsis}.pill span:last-child{overflow:hidden;text-overflow:ellipsis}
.pill .dot{width:7px;height:7px;border-radius:50%;background:var(--faint)}.pill.live .dot{background:var(--accent);box-shadow:0 0 0 3px rgba(34,211,238,.18)}.pill b{color:var(--text);font-weight:600}
.menu{position:relative}.menu .list{display:none;position:absolute;right:0;top:44px;min-width:250px;white-space:nowrap;background:var(--s2);border:1px solid var(--edge);border-radius:var(--r-card);padding:6px;z-index:20;box-shadow:0 12px 30px rgba(0,0,0,.5)}
.menu.open .list{display:block}.menu .list button{display:block;width:100%;text-align:left;border-radius:var(--r-ctl)}.menu .list button.danger{color:var(--danger)}.menu .list hr{border:0;border-top:1px solid var(--line);margin:6px 0}
#rail{grid-column:1;grid-row:2;background:var(--s1);border-right:1px solid var(--line);overflow:hidden;display:flex;flex-direction:column}
#rail .top{padding:12px 12px 10px}#rail .top button{width:100%;overflow:hidden;text-overflow:ellipsis}.railhdr{font-size:13px;font-weight:600;margin:2px 2px 10px;display:flex;justify-content:space-between;align-items:baseline}
.hsec{padding:10px 14px 4px;font-size:11px;font-weight:600;letter-spacing:.6px;text-transform:uppercase;color:var(--faint)}
#hlist{overflow:auto;flex:1}
.hitem{padding:10px 14px;cursor:pointer;display:flex;flex-direction:column;gap:2px;border-left:2px solid transparent}.hitem:hover{background:var(--s2)}.hitem.on{background:var(--s2);border-left-color:var(--accent)}
.hitem .t{font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:flex;justify-content:space-between;gap:8px}.hitem .m{font-size:11.5px;color:var(--faint)}.livedot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--accent);margin-right:6px;box-shadow:0 0 0 3px rgba(34,211,238,.18)}
.hitem .del{color:var(--faint);padding:0 4px;border:0;line-height:1;opacity:0;height:auto}.hitem:hover .del,.hitem.on .del,.hitem:focus-within .del{opacity:1}.hitem .del:hover{color:var(--danger);background:transparent}
#center{grid-column:2;grid-row:2;min-width:0;min-height:0;display:flex;flex-direction:column;overflow:hidden}
#chat{flex:1;overflow-y:auto;overflow-x:hidden;padding:24px 0 0}
.inner{width:min(860px,calc(100% - 48px));margin:0 auto}
.msg{margin:0 0 14px;padding:14px 18px 14px 18px;border-radius:var(--r-card);background:color-mix(in srgb,var(--c,var(--s1)) 7%,var(--s1));border:1px solid color-mix(in srgb,var(--c,var(--line)) 25%,var(--line));border-left:4px solid var(--c,var(--line))}
.msg .body{white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;font-size:15px;line-height:1.65}
.msg{min-width:0;max-width:100%;overflow:hidden}.msg .body p{margin:0 0 .75em}.msg .body p:last-child{margin:0}
.cite{display:inline;font:12px/1.5 ui-monospace,Consolas,"Cascadia Mono",monospace;background:rgba(255,255,255,.06);border:1px solid var(--line);border-radius:6px;padding:0 6px;color:var(--mono);overflow-wrap:anywhere;vertical-align:baseline}
.lbl{display:inline-block;font-size:10.5px;font-weight:600;letter-spacing:.4px;padding:1px 6px;border-radius:4px;vertical-align:1px;margin-right:2px}
.lbl.obs{background:rgba(63,183,122,.18);color:#7FD3A0}.lbl.doc{background:rgba(79,140,255,.18);color:#8FB7E0}.lbl.gs{background:rgba(245,158,11,.18);color:#F5C26B}
.men{font-weight:600;color:var(--mc,var(--text));text-decoration:underline;text-decoration-color:var(--mc,var(--muted));text-underline-offset:2px}
#ac{position:absolute;bottom:100%;left:0;margin-bottom:6px;background:var(--s2);border:1px solid var(--edge);border-radius:10px;padding:4px;min-width:220px;box-shadow:0 12px 30px rgba(0,0,0,.5);display:none;z-index:25}
#ac.open{display:block}#ac div{padding:8px 10px;border-radius:6px;cursor:pointer;display:flex;gap:8px;align-items:center}#ac div.on,#ac div:hover{background:var(--s3)}#ac .dot{width:10px;height:10px;border-radius:50%}#ac small{color:var(--muted)}
#composer .inner{position:relative}
.msg .hd{display:flex;gap:10px;align-items:baseline;margin-bottom:6px;font-size:13px}.msg .who{font-weight:600}.msg .meta{color:var(--faint);font-size:12px}
.msg pre{white-space:pre-wrap;margin:0;font:inherit;line-height:1.6}
.tag{font-size:11px;padding:2px 8px;border-radius:999px;background:var(--s3);color:var(--muted)}
.msg.convener{--c:#7FD3A0}.msg.convener .who{color:#7FD3A0}
.msg.system{--c:#E5484D}.msg.system .who{color:var(--danger)}
.typing{color:var(--muted);font-size:13px;padding:2px 0 14px;display:flex;gap:8px;align-items:center}.typing:before{content:"";width:7px;height:7px;border-radius:50%;background:var(--accent);animation:pulse 1.1s infinite}
@keyframes pulse{50%{opacity:.2}}
#composer{border-top:1px solid var(--line);background:var(--s1);padding:12px 0}
#composer .inner{display:flex;gap:8px;align-items:flex-end}
#composer .tawrap{flex:1;position:relative;min-width:0}#composer textarea{width:100%;min-height:44px;max-height:160px;resize:none;position:relative;background:transparent;color:transparent;caret-color:var(--text)}
#hl{position:absolute;inset:0;padding:10px 11px;border:1px solid transparent;border-radius:var(--r-ctl);font:inherit;line-height:1.55;white-space:pre-wrap;word-break:break-word;overflow:hidden;color:var(--text);pointer-events:none;background:var(--bg)}#hl .men{text-decoration:none}#composer select{background:var(--s2);border-color:var(--line);color:var(--muted);max-width:170px}
input,select,textarea{width:100%;background:var(--bg);border:1px solid var(--edge);color:var(--text);padding:0 11px;height:var(--ctl);border-radius:var(--r-ctl);font:inherit}textarea{height:auto;padding:10px 11px}
select{width:auto}input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent)}
textarea{line-height:1.55}
/* setup: empty-state screen and details sheet */
#setup{flex:1;overflow:auto;padding:28px 0}
#setup h1{font-size:20px;font-weight:600;margin:0 0 4px}#setup .lead{color:var(--muted);margin:0 0 22px}
.field{margin-bottom:18px}.field label{display:block;font-size:12.5px;color:var(--muted);margin-bottom:6px}
.field textarea{min-height:130px}.two{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.seat{display:grid;grid-template-columns:1.2fr 1fr 1fr;gap:8px;padding:12px;border:1px solid var(--line);border-radius:var(--r-card);background:var(--s1);margin-bottom:8px}
.seat .hdr{grid-column:1/4;display:flex;justify-content:space-between;font-size:12px;color:var(--muted)}.seat .hdr.off{color:var(--danger)}
.seat .full{grid-column:1/4}.seat select{width:100%}.seat .mwrap{min-width:0}
.check{display:flex;gap:10px;align-items:flex-start;cursor:pointer}.check input{width:18px;height:18px;margin-top:2px}
.note{font-size:12px;color:var(--muted);line-height:1.45}.bad{color:var(--danger);font-size:12px;margin-top:6px}
.vers{display:flex;flex-direction:column;gap:4px}
.sheetwrap .panel{width:min(520px,100%);background:var(--s1);border-left:1px solid var(--line);overflow:auto;padding:22px 24px}
.sheetwrap .panel h1{font-size:16px;margin:0 0 12px;display:flex;justify-content:space-between;align-items:center}
/* terminals drawer */
#terms{grid-column:3;grid-row:2;width:var(--terms);border-left:1px solid var(--line);position:relative;background:var(--bg);display:flex;flex-direction:column;gap:8px;padding:8px;overflow:auto;min-height:0}
.term{display:flex;flex-direction:column;background:#050506;border:1px solid var(--line);border-radius:var(--r-card);min-height:160px;flex:1 1 0;overflow:hidden;resize:vertical}
.term.speaking{border-color:var(--c)}.term .bar .nm b{color:var(--c)}
.term .bar{display:flex;justify-content:space-between;align-items:center;padding:7px 12px;background:var(--s1);border-bottom:1px solid var(--line);font-size:12.5px}
.term .bar .nm{display:inline-flex;align-items:center;gap:6px;min-width:0}.term .bar .nm b{font-weight:600}.term .bar .nm span{color:var(--faint);margin-left:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.term .st{display:flex;gap:6px;align-items:center;color:var(--faint);font-size:12px}.term.speaking .st:before{content:"";width:6px;height:6px;border-radius:50%}
.term pre{flex:1;margin:0;padding:10px 12px;overflow:auto;font:var(--tfs)/1.45 ui-monospace,Consolas,"Cascadia Mono",monospace;color:var(--mono);white-space:pre-wrap;word-break:break-word}
.term.speaking .st{color:var(--c)}.term.speaking .st:before{background:var(--c)}
.msg .who{color:var(--c,var(--text))}
.grip{position:absolute;top:0;bottom:0;width:6px;cursor:col-resize;z-index:3}.grip:hover,.grip.drag{background:rgba(34,211,238,.25)}#railGrip{right:-3px}#termGrip{left:-3px}
#rail{position:relative}.seat input[type=color]{width:34px;height:34px;padding:2px;border-radius:8px;cursor:pointer}
.seat .hdr .sw{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;vertical-align:middle}
.empty{color:var(--faint);font-size:13px;padding:14px}
#tabs{display:none}
#picker{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:center;justify-content:center;z-index:40}
#picker .box{width:560px;max-height:80vh;background:var(--s1);border:1px solid var(--edge);border-radius:var(--r-card);padding:16px;display:flex;flex-direction:column;gap:10px}
#picker ul{list-style:none;margin:0;padding:0;overflow:auto;max-height:50vh;border:1px solid var(--line);border-radius:var(--r-ctl)}
#picker li{padding:9px 12px;cursor:pointer;border-bottom:1px solid var(--line)}#picker li:hover{background:var(--s2)}
.row{display:flex;gap:8px;align-items:center}
@media (max-width:820px){
 :root{--ctl:44px}
 html,body{height:100%}body{display:flex;flex-direction:column;height:100dvh;overflow:hidden}
 header{flex:0 0 52px;padding:0 12px 0 10px;gap:6px}header .title{display:none}#railBtn,#gearBtn{display:none}header .primary{padding:0 14px}.pill{max-width:46vw;font-size:12px;padding:4px 9px}
 #rail,#setup,#chat,#terms{display:none}
 #center{flex:1 1 auto;min-height:0;display:flex;flex-direction:column}
 #rail.on{display:flex;flex:1 1 auto;min-height:0;border:0;overflow:auto}
 #setup.on{display:block;flex:1 1 auto;min-height:0;overflow:auto;padding:16px 0}
 #chat.on{display:block;flex:1 1 auto;min-height:0;overflow:auto;padding:12px 0 0}
 #terms.on{display:flex;flex:1 1 auto;min-height:0;width:100%;border:0;overflow:auto}
 #composer{flex:0 0 auto;padding:8px 0}#composer .inner{width:calc(100% - 20px);gap:6px}#composer select{max-width:104px;padding:0 6px;font-size:13px}
 .inner{width:calc(100% - 24px)}.term{min-height:60vh}.two{grid-template-columns:1fr}.seat{grid-template-columns:1fr 1fr}.seat .hdr,.seat .full{grid-column:1/3}
 #tabs{display:flex;flex:0 0 56px;background:var(--s1);border-top:1px solid var(--line)}
 #tabs button{flex:1;border:0;border-radius:0;height:56px;color:var(--muted);font-size:12.5px}#tabs button.on{color:var(--accent)}
 .sheetwrap .panel{width:100%}#picker .box{width:94vw}
}
</style></head><body>
<header>
 <button id="railBtn" class="icon" title="Conversations" aria-label="Show or hide conversations">&#9776;</button>
 <span class="brand"><svg class="mark" viewBox="0 0 32 32" width="22" height="22" aria-hidden="true"><g fill="currentColor"><circle cx="16" cy="16" r="3"/><circle cx="16" cy="4" r="2"/><circle cx="24.5" cy="7.5" r="2"/><circle cx="28" cy="16" r="2"/><circle cx="24.5" cy="24.5" r="2"/><circle cx="16" cy="28" r="2"/><circle cx="7.5" cy="24.5" r="2"/><circle cx="4" cy="16" r="2"/><circle cx="7.5" cy="7.5" r="2"/></g></svg><span>Agora</span></span><span class="title" id="hdrTitle"></span>
 <span class="pill" id="status"><span class="dot"></span><span id="statusText">Not started</span></span>
 <button id="details" title="Topic, rounds, and agents" style="color:var(--muted)">Details</button>
 <button id="start" class="primary">Start</button>
 <button id="pause" class="btn">Pause</button><button id="endBtn" class="btn" title="End now with closing statements">End</button>
 <div class="menu" id="menu"><button class="icon" id="menuBtn" title="Conversation actions" aria-label="Conversation actions" aria-haspopup="menu">&#8943;</button><div class="list">
  <button id="vote">End with closing statements</button><button id="stop" class="danger">Stop this conversation</button><hr><button id="expClosing">Download closing statements (.md)</button><button id="expSpeeches">Download conversation (.md)</button><button id="expAll">Download full record (.md)</button></div></div>
 <button class="icon" id="gearBtn" title="Settings" aria-label="Settings"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg></button>
</header>
<aside id="rail"><div class="grip" id="railGrip" title="Drag to resize"></div><div class="top"><div class="railhdr">Conversations <span id="railCount" class="note"></span></div><button id="newSess" class="primary" style="width:100%">+ New conversation</button></div><div id="hlist"></div></aside>

<main id="center">
 <section id="setup"><div class="inner">
  <h1>New conversation</h1><p class="lead">Seat the council, set the topic, press Start. You can message them any time.</p>
  <div class="field"><label>Title</label><input id="title" placeholder="Named from the topic if left blank"></div>
  <div class="field"><label>Folder the agents work in</label><div class="row"><input id="repo"><button id="browse" class="btn">Browse</button></div><div id="repoBad" class="bad"></div></div>
  <div class="field"><label>Agents <span class="note" id="seatNote">(they speak in this order)</span></label>
   <div class="row" style="margin-bottom:10px"><select id="tmpl" style="flex:1"><option value="">Start from a template...</option></select><button id="tmplApply" class="btn">Use</button><button id="tmplDel" class="btn" title="Delete this saved template" style="display:none">Delete</button></div>
   <div class="note" style="margin:-4px 0 10px"><button id="tmplSave" class="btn sm">Save current setup as a template</button> <span style="margin-left:6px">Saves agents, models, colors, stances, topic, mode, and all settings for reuse.</span></div>
   <div id="seats"></div><button id="add" class="btn">+ Add agent</button></div>
  <div class="field"><label>Topic</label><textarea id="topic"></textarea></div>
  <div class="field"><label>Extra instructions (optional)</label><textarea id="extra" style="min-height:70px" placeholder="Anything else they must do, avoid, or produce"></textarea></div>
  <div class="field"><label>What this is</label><div class="row"><button class="btn" id="frCouncil" style="flex:1">Council</button><button class="btn" id="frGame" style="flex:1">Game</button></div><div class="note" style="margin-top:6px" id="frNote"></div></div>
  <div class="field" id="refField"><label>Referee (the World)</label><select id="referee"><option value="">None</option></select><div class="note" style="margin-top:6px">Only the referee's messages wake everyone. Players wake the referee and whoever they @mention or whisper to. The referee speaks first.</div></div>
  <div class="field"><label>How they speak</label><div class="row" id="modeRow"><button class="btn" data-m="turns" id="modeTurns" style="flex:1">Take turns</button><button class="btn" data-m="open" id="modeOpen" style="flex:1">Open floor</button></div><div class="note" id="modeNote" style="margin-top:6px"></div></div>
  <div class="two" id="limits"><div class="field"><label>Close the floor after this many messages</label><input id="maxMsgs" type="number" min="0" placeholder="no limit"></div><div class="field"><label>Or after this many minutes</label><input id="maxMins" type="number" min="0" placeholder="no limit"></div></div>
  <div class="two"><div class="field"><label id="roundsLabel">Rounds</label><input id="rounds" type="number" min="1"><div class="note" id="roundsNote" style="margin-top:6px">Each agent speaks once per round, then gives a closing statement.</div></div>
   <div class="field"><label>Access</label><label class="check"><input id="ro" type="checkbox"><span>Read only<br><span class="note">Agents can read and search but not run or change anything.</span></span></label><div class="note" id="roNote" style="margin-top:6px"></div></div></div>
  <div class="field"><label>Installed CLIs</label><div id="vers" class="vers note"></div></div>
  <div class="field"><button id="start2" class="primary" style="padding:10px 22px">Start conversation</button></div>
 </div></section>
 <section id="chat"><div class="inner" id="chatInner"><div class="empty" id="chatEmpty" style="text-align:center;padding-top:8vh"><svg class="ringart" viewBox="0 0 32 32" aria-hidden="true"><g fill="currentColor"><circle cx="16" cy="16" r="2.4"/><g opacity=".55"><circle cx="16" cy="4" r="1.6"/><circle cx="24.5" cy="7.5" r="1.6"/><circle cx="28" cy="16" r="1.6"/><circle cx="24.5" cy="24.5" r="1.6"/><circle cx="16" cy="28" r="1.6"/><circle cx="7.5" cy="24.5" r="1.6"/><circle cx="4" cy="16" r="1.6"/><circle cx="7.5" cy="7.5" r="1.6"/></g></g></svg><div>The floor is empty. Each agent's turn appears here as it finishes.</div></div><div id="msgs"></div><div class="typing" id="typing" style="display:none"></div></div></section>
 <div id="composer"><div class="inner"><div id="ac" role="listbox"></div><div class="tawrap"><div id="hl" aria-hidden="true"></div><textarea id="sayText" rows="1" aria-label="Message the agents"></textarea></div><select id="sayTo" title="Who must answer first" aria-label="Who must answer first"><option value="">Everyone</option></select><button id="sayBtn" class="primary">Send</button></div></div>
</main>

<aside id="terms"><div class="grip" id="termGrip" title="Drag to resize"></div><div class="empty">Each agent's live terminal appears here after you press Start.</div></aside>

<nav id="tabs"><button data-t="chat" class="on">Chat</button><button data-t="terms">Terminals</button><button data-t="rail">Conversations</button><button data-t="settings" id="tabSettings">Settings</button></nav>
<div id="settings" class="sheetwrap"><div class="panel"><h1>Settings <button class="icon" id="settingsClose" aria-label="Close settings">&#10005;</button></h1>
 <section class="sg"><h2>Phone</h2><div class="note">Open Agora on your phone. The Tailscale link works anywhere; the home link only on your wifi.</div>
  <div class="kv"><span>Anywhere</span><code id="awayUrl">not available</code><button class="btn sm cp" data-for="awayUrl">Copy</button></div>
  <div class="kv"><span>Home wifi</span><code id="homeUrl">not available</code><button class="btn sm cp" data-for="homeUrl">Copy</button></div>
  <div class="row" style="margin-top:8px"><button id="tg" class="btn">Send link to my Telegram</button><span class="note" id="tgState"></span></div></section>
 <section class="sg"><h2>Display</h2>
  <div class="kv"><span>Text size</span><span class="row"><button class="btn sm" id="fsDown">Smaller</button><button class="btn sm" id="fsUp">Larger</button></span></div>
  <div class="kv" id="termRow"><span>Terminals</span><button class="btn sm" id="termBtn">Show or hide</button></div>
  <div class="kv"><span>Panel sizes</span><button class="btn sm" id="layoutReset">Reset to defaults</button></div></section>
 <section class="sg"><h2>Agora</h2>
  <div class="kv"><span>Build</span><code id="buildTag"></code></div>
  <div class="kv"><span>Conversations folder</span><code id="sessDir"></code></div>
  <div class="row" style="margin-top:10px"><button id="quit" class="danger solid">Quit Agora</button><span class="note">Kills every CLI Agora started and closes the server. Conversations are kept.</span></div></section>
</div></div>
<div id="sheet" class="sheetwrap"><div class="panel"><h1>Details <button class="icon" id="sheetClose" aria-label="Close details">&#10005;</button></h1><div id="sheetBody"></div>
 <div style="position:sticky;bottom:0;background:var(--s1);padding:12px 0 8px;border-top:1px solid var(--line);margin-top:16px"><button id="sheetSave" class="primary">Save changes</button><div class="note" id="saveState" style="margin-top:8px">Changes save automatically when you leave a field.</div></div></div></div>
<div id="picker"><div class="box"><b>Choose a folder</b><div class="row"><input id="pkPath"><button id="pkUp" class="btn">Up</button></div><ul id="pkList"></ul><div class="row" style="justify-content:flex-end"><button id="pkCancel" class="btn">Cancel</button><button id="pkUse" class="primary">Use this folder</button></div></div></div>

<script>
let S=null,editing=false,providers={},termKey='',menuOpen=false,seatsLocked=null;let T=[],lastTurn=-1,Tid=null;
function mergeTranscript(s){if(s.id!==Tid||s.transcript_total<T.length){T=s.transcript?s.transcript.slice():[];Tid=s.id}else if(s.transcript)for(const e of s.transcript)if(e.turn>lastTurn)T.push(e);lastTurn=T.length?T[T.length-1].turn:-1;s.transcript=T}
const $=id=>document.getElementById(id);
async function api(p,b){const r=await fetch(p,{method:b?'POST':'GET',headers:{'Content-Type':'application/json'},body:b?JSON.stringify(b):null});return r.json()}
const esc=s=>String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
function seatColor(name){const x=(S&&S.seats||[]).find(y=>lbl(y)===name);return x?x.color:''}
function rich(text){let h=esc(text);const hold=[];const keep=x=>{hold.push(x);return `\u0000${hold.length-1}\u0000`};
 // [file.py:123](C:/full/path) -> chip with full path on hover (held so later passes skip it)
 h=h.replace(/\[([^\]\n]{1,80}?:\d+(?:-\d+)?)\]\(([^)\n]+)\)/g,(m,a,b)=>keep(`<span class="cite" title="${esc(b)}">${a}</span>`));
 // bare paths with line numbers: backend/app/x.py:305-316 or coder.py:305
 h=h.replace(/(?<![\w"/])((?:[\w.-]+\/)*[\w.-]+\.(?:py|ts|tsx|js|jsx|md|json|ps1|cs|cpp|h))(:\d+(?:-\d+)?(?:,\s?\d+(?:-\d+)?)*)?(?![\w/])/g,(m,f,ln)=>ln?keep(`<span class="cite">${f}${ln}</span>`):m);
 // knowability labels
 h=h.replace(/\bOBSERVED\b:?/g,'<span class="lbl obs">observed</span>').replace(/\bDOCUMENTED\b:?/g,'<span class="lbl doc">documented</span>').replace(/\bGUESS\b:?/g,'<span class="lbl gs">guess</span>');
 // @mentions in the mentioned agent's color
 h=h.replace(/@([A-Za-z][\w-]*)/g,(m,n)=>{const c=seatColor(n);return `<span class="men" style="${c?'--mc:'+esc(c):''}">@${n}</span>`});
 h=h.replace(/\u0000(\d+)\u0000/g,(m,i)=>hold[+i]);
 // paragraphs
 return h.split(/\n{2,}/).map(pg=>`<p>${pg.replace(/\n/g,'<br>')}</p>`).join('')}
const STATUS={idle:'Not started',running:'Running',paused:'Paused',voting:'Closing statements',done:'Finished',stopped:'Stopped'};
const lbl=x=>x.name||(x.provider+' ('+x.model+')');
const mobile=()=>window.matchMedia('(max-width:820px)').matches;
function seatModel(d){const sel=d.querySelector('.msel');return sel.value==='__custom'?d.querySelector('.mcustom').value.trim():sel.value}
function seatsFromDom(){return [...document.querySelectorAll('.seat')].map(d=>({name:d.querySelector('.n').value,provider:d.querySelector('.p').value,model:seatModel(d),stance:d.querySelector('.s').value,color:d.querySelector('.c').value}))}
function seatHtml(s,i,locked){const p=providers[s.provider]||{models:[],installed:true};const dis=locked?'disabled':'';const ndis=locked?'disabled':'';
 const opts=Object.keys(providers).map(k=>`<option ${k===s.provider?'selected':''}>${esc(k)}</option>`).join('');
 return `<div class="seat" data-i="${i}"><div class="hdr ${p.installed?'':'off'}"><span><span class="av" style="--c:${esc(s.color||'#22D3EE')};width:18px;height:18px;font-size:10px;margin-right:6px;vertical-align:middle">${esc((s.name||String(i+1))[0])}</span>Agent ${i+1}${p.installed?'':' · CLI not installed'}</span>${locked?'':'<button class="x" style="padding:0;color:var(--faint)">Remove</button>'}</div>
 <div class="row"><input type="color" class="c" value="${esc(s.color||'#22D3EE')}" title="Agent color" aria-label="Agent color"><input class="n" placeholder="Name" value="${esc(s.name)}" ${ndis}></div><select class="p" aria-label="Provider">${opts}</select>
 <div class="mwrap"><select class="msel" aria-label="Model">${p.models.map(m=>`<option ${m===s.model?'selected':''}>${esc(m)}</option>`).join('')}<option value="__custom" ${p.models.includes(s.model)?'':'selected'}>Custom...</option></select><input class="mcustom" placeholder="Type a model name" value="${p.models.includes(s.model)?'':esc(s.model)}" style="display:${p.models.includes(s.model)?'none':''};margin-top:6px"></div>
 <input class="s full" placeholder="Stance or role (optional)" value="${esc(s.stance)}"></div>`}
function renderSeats(seats,locked){$('seats').innerHTML=seats.map((s,i)=>seatHtml(s,i,locked)).join('');$('add').disabled=locked;
 $('seatNote').textContent=locked?'(names fixed; change a model or stance any time, it applies on that agent\'s next turn)':'(they speak in this order)';
 document.querySelectorAll('.seat').forEach(d=>{const i=+d.dataset.i;const x=d.querySelector('.x');if(x)x.onclick=async()=>{editing=true;const s=seatsFromDom();s.splice(i,1);renderSeats(s,false);await save();editing=false};
  d.querySelector('.p').onchange=async e=>{editing=true;const s=seatsFromDom();s[i].model=(providers[e.target.value]||{models:['']}).models[0]||'';renderSeats(s,locked);await save();editing=false};
  d.querySelector('.c').oninput=e=>{d.querySelector('.av').style.setProperty('--c',e.target.value)};d.querySelector('.c').onchange=save;
  d.querySelector('.msel').onchange=async e=>{const c=d.querySelector('.mcustom');c.style.display=e.target.value==='__custom'?'':'none';if(e.target.value==='__custom'){c.focus();return}editing=true;await save();editing=false};
  d.querySelector('.mcustom').onfocus=()=>editing=true;d.querySelector('.mcustom').onblur=()=>{editing=false;save()};
  d.querySelectorAll('input:not(.c):not(.mcustom)').forEach(x=>{x.onfocus=()=>editing=true;x.onblur=()=>{editing=false;save()}})})}
function renderTerms(s){const started=s.transcript.length>0||['running','voting','paused'].includes(s.status);
 if(!started||!s.terms.length){if(termKey!==''){termKey='';$('terms').innerHTML='<div class="grip" id="termGrip"></div><div class="empty">Each agent\'s live terminal appears here after you press Start.</div>';bindGrips()}return}
 const key=s.id+':'+s.terms.length;
 if(termKey!==key){termKey=key;$('terms').innerHTML='<div class="grip" id="termGrip"></div>'+s.terms.map((t,i)=>`<div class="term" id="t${i}"><div class="bar"><span class="nm"></span><span class="st"></span></div><pre></pre></div>`).join('');bindGrips()}
 s.terms.forEach((t,i)=>{const el=$('t'+i);if(!el)return;const seat=s.seats[i]||{};el.style.setProperty('--c',seat.color||'#22D3EE');
  el.querySelector('.nm').innerHTML=`<span class="av ${t.state==='speaking'?'on':''}">${esc((lbl(seat)||'?')[0])}</span> <b>${esc(lbl(seat))}</b><span>${esc((seat.provider||'')+' · '+(seat.model||''))}</span>`;
  el.classList.toggle('speaking',t.state==='speaking');el.querySelector('.st').textContent=t.state==='speaking'?'Speaking':'';
  const pre=el.querySelector('pre');const atBottom=pre.scrollHeight-pre.scrollTop-pre.clientHeight<40;if(pre.dataset.count!=t.count&&t.lines.length){pre.textContent=t.lines.join('\n');pre.style.color='';pre.dataset.count=t.count;if(atBottom)pre.scrollTop=pre.scrollHeight}else if(!pre.textContent){pre.textContent='No live output yet. This pane fills when the agent next speaks.';pre.style.color='var(--faint)'}})}
function renderHist(s){const live=new Set(s.live||[]);$('railCount').textContent=s.sessions.length;const item=h=>`<div class="hitem ${h.id===s.id?'on':''}" data-id="${h.id}" tabindex="0" role="button"><div class="t"><span>${live.has(h.id)?'<span class="livedot"></span>':''}${esc(h.title)}</span><button class="del" data-id="${h.id}" aria-label="Delete conversation ${esc(h.title)}" title="Delete">&times;</button></div><div class="m">${esc(h.created.replace('T',' '))} · ${h.turns?h.turns+' turns':'draft'}</div></div>`;
 const L=s.sessions.filter(h=>live.has(h.id)),E=s.sessions.filter(h=>!live.has(h.id));
 $('hlist').innerHTML=(L.length?'<div class="hsec">Live now</div>'+L.map(item).join(''):'')+(E.length?'<div class="hsec">'+(L.length?'Earlier':'All')+'</div>'+E.map(item).join(''):'');
 document.querySelectorAll('.hitem').forEach(d=>{d.onkeydown=e=>{if(e.key==='Enter')d.click()};d.onclick=async e=>{if(e.target.classList.contains('del')){if(confirm('Delete this conversation and its transcript?'))render(await api('/session/delete',{id:e.target.dataset.id}));return}
  S=null;termKey='';render(await api('/session/open',{id:d.dataset.id}));if(mobile())showTab('chat')}})}
function renderVers(s){$('vers').innerHTML=Object.entries(s.providers).map(([k,p])=>{if(p.isolated)return `<div>${esc(k)}: runs the newest Codex release, downloaded on first use, without touching your installed Codex.</div>`;
 const inst=p.installed?(p.version||'installed'):'not installed';const up=p.latest&&p.version&&!p.version.includes(p.latest);
 return `<div class="row"><span>${esc(k)}: <b style="color:var(--text)">${esc(inst)}</b>${p.latest?' · latest '+esc(p.latest):''}${up?' <span style="color:var(--faint)">(Agora never updates installed CLIs)</span>':''}</span></div>`}).join('')}
function render(s){const first=!S||S.id!==s.id;if(first){T=[];lastTurn=-1}mergeTranscript(s);S=s;providers=s.providers;
 const busy=['running','voting'].includes(s.status);const paused=s.status==='paused';const started=s.transcript.length>0||busy||paused;
 $('hdrTitle').textContent=s.title||'';
 const who=s.current?s.current.split(', '):[];const whoTxt=who.length>2?`<b>${who.length} agents</b>`:`<b>${esc(s.current||'')}</b>`;
 $('statusText').innerHTML=s.current?(paused?`Paused after ${whoTxt} finish${who.length>1?'':'es'}`:`${whoTxt} ${who.length>1?'are':'is'} speaking`):STATUS[s.status]||s.status;$('status').classList.toggle('live',busy);
 const startLbl=paused?'Resume':(s.status==='done'||s.status==='stopped')?'Continue':'Start';
 $('start').textContent=startLbl;$('start').title=startLbl==='Continue'?`Continue for ${s.rounds} more rounds`:'';
 $('start').style.display=busy?'none':'';$('start').disabled=!s.repo_ok||s.seats.length<2;$('start2').disabled=$('start').disabled;$('start2').style.display=started?'none':'';
 $('pause').style.display=busy?'':'none';$('pause').disabled=s.status!=='running';$('vote').disabled=!busy;$('stop').disabled=!busy&&!paused;
 $('details').style.display=(started||busy)?'':'none';
 const can=busy||paused;$('sayBtn').disabled=!can;$('sayText').placeholder='Message the agents';
 $('roNote').textContent=s.readonly?'':'Full access: no permission prompts, agents can edit files. Use a folder with a clean git status.';
 $('repoBad').textContent=s.repo_ok?'':'That folder does not exist.';
 const game=s.framing==='game';$('frCouncil').classList.toggle('on',!game);$('frGame').classList.toggle('on',game);$('frCouncil').disabled=busy;$('frGame').disabled=busy;$('frNote').textContent=game?'Characters with fixed stats. Words persuade, only the World changes numbers, every message ends with one ACTION line. Seat one agent named World with the referee stance.':'Agents debate the topic, cite the folder, and give closing statements.';
 const open=s.mode==='open';$('modeTurns').classList.toggle('on',!open);$('modeOpen').classList.toggle('on',open);$('modeTurns').disabled=busy;$('modeOpen').disabled=busy;
 $('modeNote').textContent=(s.framing==='game'?'Game framing: characters with fixed stats, the World referees. ':'')+(open?'No turns. Every message goes to everyone at once and each agent replies or passes. Use @Name to demand an answer. The floor closes when everyone passes, when a limit below is hit, or when you press End.':'Agents speak one after another in seat order.');
 $('refField').style.display=game?'':'none';const ropts='<option value="">None</option>'+s.seats.map(x=>`<option value="${esc(lbl(x))}" ${lbl(x)===s.referee?'selected':''}>${esc(lbl(x))}</option>`).join('');if($('referee').innerHTML!==ropts)$('referee').innerHTML=ropts;$('referee').value=s.referee||'';
 $('rounds').closest('.field').style.display=open?'none':'';$('limits').style.display=open?'':'none';$('maxMsgs').disabled=false;$('maxMins').disabled=false;$('endBtn').style.display=(busy&&open)?'':'none';
 $('roundsNote').textContent=started?`${s.rounds_done} round(s) done. Continue adds this many more.`:'Each agent speaks once per round, then gives a closing statement.';
 $('repo').disabled=started;$('browse').disabled=started;$('ro').disabled=busy||paused;
 const opts='<option value="">Everyone</option>'+s.seats.map(x=>`<option value="${esc(lbl(x))}">${esc(lbl(x))}</option>`).join('');if($('sayTo').innerHTML!==opts)$('sayTo').innerHTML=opts;
 const ut=s.user_templates||[];const topts='<option value="">Start from a template...</option>'+(ut.length?'<optgroup label="My templates">'+ut.map(t=>`<option>${esc(t)}</option>`).join('')+'</optgroup>':'')+'<optgroup label="Built in">'+(s.templates||[]).map(t=>`<option>${esc(t)}</option>`).join('')+'</optgroup>';
 if($('tmpl').innerHTML!==topts){const cur=$('tmpl').value;$('tmpl').innerHTML=topts;$('tmpl').value=cur}$('tmpl').disabled=started||busy;$('tmplApply').disabled=started||busy;$('tmplDel').style.display=ut.includes($('tmpl').value)?'':'none';
 if(first||!editing){$('title').value=s.title;$('repo').value=s.repo;$('topic').value=s.topic;$('extra').value=s.extra;$('rounds').value=s.rounds;$('maxMsgs').value=s.max_messages||'';$('maxMins').value=s.max_minutes||'';$('ro').checked=s.readonly;
  if(first||seatsLocked!==started||JSON.stringify(seatsFromDom())!==JSON.stringify(s.seats.map(x=>({name:x.name,provider:x.provider,model:x.model,stance:x.stance,color:x.color})))){renderSeats(s.seats,started);seatsLocked=started}renderVers(s)}
 renderHist(s);
 // empty-state setup vs conversation
 if(!mobile()){$('setup').style.display=started||busy?'none':'block';$('chat').style.display=started||busy?'block':'none';$('composer').style.display=started||busy?'':'none';document.body.classList.toggle('autoterm',!(started||busy))}
 $('chatEmpty').style.display=s.transcript.length?'none':'block';
 const chatEl=$('chat');const atBottom=first||(chatEl.scrollHeight-chatEl.scrollTop-chatEl.clientHeight<80);
 const msgHtml=e=>`<div class="msg ${e.kind}" style="${e.color?'--c:'+esc(e.color):''}"><div class="hd"><span class="av">${esc((e.speaker||'?')[0])}</span><span class="who">${esc(e.speaker)}</span>${e.kind==='resolution'?'<span class="tag">closing statement</span>':''}<span class="meta">turn ${e.turn} · ${e.time}</span></div><div class="body">${rich(e.text)}</div></div>`;
 const have=$('msgs').children.length;
 if(first||have>s.transcript.length){$('msgs').innerHTML=s.transcript.map(msgHtml).join('');chatEl.scrollTop=chatEl.scrollHeight}
 else if(have<s.transcript.length){const atB=chatEl.scrollHeight-chatEl.scrollTop-chatEl.clientHeight<120;$('msgs').insertAdjacentHTML('beforeend',s.transcript.slice(have).map(msgHtml).join(''));if(atB)chatEl.scrollTop=chatEl.scrollHeight}
 $('typing').style.display=s.current?'flex':'none';$('typing').textContent=s.current?(who.length>3?who.length+' agents':s.current)+(paused?' finishing, then the conversation pauses':(who.length>1?' are writing':' is writing')):'';
 renderTerms(s)}
function showTab(t){if(t==='settings'){openSettings();document.querySelectorAll('#tabs button').forEach(b=>b.classList.toggle('on',b.dataset.t===t));return}$('settings').classList.remove('open');['chat','terms','setup','rail'].forEach(id=>$(id).classList.toggle('on',id===t));document.querySelectorAll('#tabs button').forEach(b=>b.classList.toggle('on',b.dataset.t===t));if(mobile()){$('center').style.display=(t==='chat'||t==='setup')?'flex':'none';$('composer').style.display=t==='chat'?'':'none';$('setup').style.display=t==='setup'?'block':'none';$('chat').style.display=t==='chat'?'block':'none'}}
document.querySelectorAll('#tabs button').forEach(b=>b.onclick=()=>showTab(b.dataset.t));if(mobile())showTab('chat');
$('railBtn').onclick=()=>document.body.classList.toggle('norail');$('termBtn').onclick=()=>{document.body.classList.toggle('noterm')};
$('gearBtn').onclick=()=>openSettings();$('settingsClose').onclick=()=>{$('settings').classList.remove('open');if(mobile())showTab('chat')};
function openSettings(){if(S){$('awayUrl').textContent=S.away_url||'not available (install Tailscale on this PC and your phone)';$('homeUrl').textContent=S.phone_url||'not available';$('sessDir').textContent=S.sessions_dir||'';$('buildTag').textContent=S.build||''}$('settings').classList.add('open')}
document.querySelectorAll('.cp').forEach(b=>b.onclick=async()=>{const t=$(b.dataset.for).textContent;if(!t.startsWith('http'))return;try{await navigator.clipboard.writeText(t);b.textContent='Copied';setTimeout(()=>b.textContent='Copy',1200)}catch(e){prompt('Copy this link',t)}});
$('menuBtn').onclick=e=>{e.stopPropagation();$('menu').classList.toggle('open')};function closeMenu(){$('menu').classList.remove('open')}document.addEventListener('click',e=>{if(!$('menu').contains(e.target))closeMenu()});
// details sheet: move the setup form in and out
$('details').onclick=()=>{$('sheetBody').appendChild($('setup').querySelector('.inner'));$('setup').querySelector('.inner')||0;$('sheet').classList.add('open');$('sheetBody').querySelector('h1').style.display='none';$('sheetBody').querySelector('.lead').style.display='none'};
$('sheetClose').onclick=()=>{const inner=$('sheetBody').querySelector('.inner');if(inner){inner.querySelector('h1').style.display='';inner.querySelector('.lead').style.display='';$('setup').appendChild(inner)}$('sheet').classList.remove('open')};
let saveSeq=0;
async function save(){const my=++saveSeq;const r=await api('/config',{seats:seatsFromDom(),title:$('title').value,topic:$('topic').value,extra:$('extra').value,rounds:+$('rounds').value,repo:$('repo').value,readonly:$('ro').checked,max_messages:+($('maxMsgs').value||0),max_minutes:+($('maxMins').value||0)});
 if(my===saveSeq){const t=new Date();$('saveState').textContent='Saved '+t.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'})+'. Model and stance changes apply on that agent\'s next turn.'}return render(r)}
$('sheetSave').onclick=async()=>{editing=false;await save()};
$('tmplApply').onclick=async()=>{const n=$('tmpl').value;if(!n)return;S=null;render(await api('/template',{name:n}))};
$('tmpl').onchange=()=>{$('tmplDel').style.display=(S&&(S.user_templates||[]).includes($('tmpl').value))?'':'none'};
$('tmplSave').onclick=async()=>{await save();const n=prompt('Name this template:',S&&S.title?S.title:'');if(!n)return;render(await api('/template/save',{name:n}));$('tmpl').value=n;$('tmplDel').style.display=''};
$('tmplDel').onclick=async()=>{const n=$('tmpl').value;if(!n||!confirm('Delete template "'+n+'"?'))return;render(await api('/template/delete',{name:n}));$('tmpl').value=''};
$('add').onclick=async()=>{editing=true;const s=seatsFromDom();const k=Object.keys(providers)[0];s.push({name:'',provider:k,model:providers[k].models[0],stance:''});renderSeats(s,false);await save();editing=false};
$('newSess').onclick=async()=>{S=null;termKey='';render(await api('/session/new',{}));if(mobile())showTab('setup')};
$('start').onclick=async()=>{await save();render(await api('/start',{}));$('sheetClose').click();closeMenu();if(mobile())showTab('chat')};$('start2').onclick=()=>$('start').click();
$('pause').onclick=async()=>render(await api('/pause',{}));
$('vote').onclick=async()=>{closeMenu();render(await api('/vote',{}))};
[['expClosing','closing'],['expSpeeches','speeches'],['expAll','all']].forEach(([id,w])=>$(id).onclick=()=>{closeMenu();if(S)window.location='/export?what='+w+'&id='+encodeURIComponent(S.id)});$('endBtn').onclick=async()=>{if(confirm('End the open floor now? Agents still composing will finish, then everyone gives a closing statement.'))render(await api('/vote',{}))};
$('stop').onclick=async()=>{closeMenu();if(!confirm('Stop this conversation? The transcript is kept and you can continue it later.'))return;$('statusText').textContent='Stopping...';render(await api('/stop',{}))};
$('quit').onclick=async()=>{if(!confirm('Quit Agora? This kills every CLI it started and closes the server. Conversations are kept.'))return;try{await api('/shutdown',{})}catch(e){}document.body.innerHTML='<div style="padding:40px;color:#8B8B94">Agora is closed. You can close this tab.</div>'};
$('sayBtn').onclick=async()=>{const t=$('sayText').value.trim();if(!t)return;$('sayText').value='';hlSync();render(await api('/say',{text:t,target:$('sayTo').value}))};
let acItems=[],acIdx=0,acStart=-1;
function acClose(){$('ac').classList.remove('open');acItems=[]}
function acRender(){$('ac').innerHTML=acItems.map((x,i)=>`<div class="${i===acIdx?'on':''}" data-i="${i}"><span class="dot" style="background:${esc(x.color)}"></span><span>${esc(lbl(x))}</span><small>${esc(x.provider)}</small></div>`).join('');
 document.querySelectorAll('#ac div').forEach(d=>{d.onmousedown=e=>{e.preventDefault();acPick(+d.dataset.i)}});$('ac').classList.toggle('open',acItems.length>0)}
function acPick(i){const x=acItems[i];if(!x)return;const t=$('sayText');const v=t.value;const before=v.slice(0,acStart);const after=v.slice(t.selectionStart);t.value=before+'@'+lbl(x)+' '+after;const pos=(before+'@'+lbl(x)+' ').length;t.setSelectionRange(pos,pos);acClose();t.focus();hlSync()}
function acUpdate(){const t=$('sayText');const v=t.value.slice(0,t.selectionStart);const m=v.match(/(?:^|\s)@([\w-]*)$/);if(!m||!S){acClose();return}
 acStart=v.length-m[0].length+(m[0].startsWith('@')?0:1);const q=m[1].toLowerCase();acItems=S.seats.filter(x=>lbl(x).toLowerCase().startsWith(q));acIdx=0;acRender()}
function hlSync(){const t=$('sayText');let h=esc(t.value);let firstMention='';
 h=h.replace(/@([A-Za-z][\w-]*)/g,(m,n)=>{const seat=(S&&S.seats||[]).find(x=>lbl(x).toLowerCase()===n.toLowerCase());if(seat&&!firstMention)firstMention=lbl(seat);const c=seat?seat.color:'';return c?`<span class="men" style="--mc:${esc(c)}">@${n}</span>`:`@${n}`});
 $('hl').innerHTML=h+(t.value.endsWith('\n')?'<br>':'');$('hl').scrollTop=t.scrollTop;
 if(firstMention){const opt=[...$('sayTo').options].find(o=>o.value===firstMention);if(opt)$('sayTo').value=firstMention}else if(!t.value.includes('@'))$('sayTo').value='';}
$('sayText').oninput=()=>{acUpdate();hlSync()};$('sayText').onclick=acUpdate;$('sayText').onscroll=()=>{$('hl').scrollTop=$('sayText').scrollTop};$('sayText').onblur=()=>setTimeout(acClose,150);
$('sayText').onkeydown=e=>{const open=$('ac').classList.contains('open');
 if(open&&(e.key==='ArrowDown'||e.key==='ArrowUp')){e.preventDefault();acIdx=(acIdx+(e.key==='ArrowDown'?1:acItems.length-1))%acItems.length;acRender();return}
 if(open&&(e.key==='Enter'||e.key==='Tab')){e.preventDefault();acPick(acIdx);return}
 if(open&&e.key==='Escape'){acClose();return}
 if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();$('sayBtn').click()}};
$('tg').onclick=async()=>{$('tgState').textContent='Sending...';const r=await api('/telegram',{});$('tgState').textContent=r.last_tg||'No response'};
let saveTimer=null;['title','repo','topic','extra','rounds','maxMsgs','maxMins'].forEach(id=>{const el=$(id);el.onfocus=()=>editing=true;el.onblur=()=>{editing=false;save()};el.oninput=()=>{clearTimeout(saveTimer);saveTimer=setTimeout(save,800)}});
$('ro').onchange=save;$('referee').onchange=async()=>render(await api('/config',{referee:$('referee').value}));$('modeTurns').onclick=async()=>render(await api('/config',{mode:'turns'}));$('frCouncil').onclick=async()=>render(await api('/config',{framing:'council'}));$('frGame').onclick=async()=>render(await api('/config',{framing:'game'}));$('modeOpen').onclick=async()=>render(await api('/config',{mode:'open'}));
let pk={path:''};
async function openPk(p){const d=await api('/ls?path='+encodeURIComponent(p||$('repo').value));pk=d;$('pkPath').value=d.path;
 const sep=d.path.includes('\\')?'\\':'/';const base=d.path.replace(/[\\/]$/,'');
 $('pkList').innerHTML=d.drives.map(x=>`<li data-p="${esc(x)}">${esc(x)}</li>`).join('')+d.dirs.map(x=>`<li data-p="${esc(base+sep+x)}">${esc(x)}</li>`).join('');
 document.querySelectorAll('#pkList li').forEach(li=>li.onclick=()=>openPk(li.dataset.p));$('picker').style.display='flex'}
$('browse').onclick=()=>openPk();$('pkUp').onclick=()=>pk.parent&&openPk(pk.parent);$('pkPath').onchange=e=>openPk(e.target.value);
$('pkCancel').onclick=()=>$('picker').style.display='none';$('pkUse').onclick=async()=>{$('repo').value=pk.path;$('picker').style.display='none';await save()};
// layout: sizes persist in this browser
const LAY=JSON.parse(localStorage.getItem('agora-layout')||'{}');
function applyLayout(){const r=document.documentElement.style;if(LAY.rail)r.setProperty('--rail',LAY.rail+'px');if(LAY.terms)r.setProperty('--terms',LAY.terms+'px');if(LAY.fs){r.setProperty('--fs',LAY.fs+'px');r.setProperty('--tfs',Math.max(10,LAY.fs-2)+'px')}}
function saveLayout(){localStorage.setItem('agora-layout',JSON.stringify(LAY))}
function bindGrips(){const g1=$('railGrip'),g2=$('termGrip');
 if(g1&&!g1.dataset.b){g1.dataset.b=1;g1.onpointerdown=e=>drag(e,g1,x=>{LAY.rail=Math.min(480,Math.max(180,x));applyLayout()})}
 if(g2&&!g2.dataset.b){g2.dataset.b=1;g2.onpointerdown=e=>drag(e,g2,x=>{LAY.terms=Math.min(window.innerWidth*0.7,Math.max(280,window.innerWidth-x));applyLayout()})}}
function drag(e,el,fn){e.preventDefault();el.classList.add('drag');el.setPointerCapture(e.pointerId);const mv=ev=>fn(ev.clientX);const up=()=>{el.classList.remove('drag');window.removeEventListener('pointermove',mv);window.removeEventListener('pointerup',up);saveLayout()};window.addEventListener('pointermove',mv);window.addEventListener('pointerup',up)}
$('fsUp').onclick=()=>{LAY.fs=Math.min(20,(LAY.fs||14)+1);applyLayout();saveLayout()};$('fsDown').onclick=()=>{LAY.fs=Math.max(11,(LAY.fs||14)-1);applyLayout();saveLayout()};
$('layoutReset').onclick=()=>{for(const k of Object.keys(LAY))delete LAY[k];localStorage.removeItem('agora-layout');const r=document.documentElement.style;['--rail','--terms','--fs','--tfs'].forEach(v=>r.removeProperty(v))};
applyLayout();bindGrips();
function wantTerms(){const noterm=document.body.classList.contains('noterm')||document.body.classList.contains('autoterm');return mobile()?$('terms').classList.contains('on'):!noterm}
(async function poll(){try{render(await api('/state?since='+(S?lastTurn:-1)+'&terms='+(wantTerms()?1:0)))}catch(e){}setTimeout(poll,1500)})();
</script></body></html>"""


def make_handler(agora: Agora, token: str):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _send(self, body: bytes, ctype: str):
            self.send_response(200); self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store, max-age=0"); self.send_header("Pragma", "no-cache")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        def _authed(self) -> bool:
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query).get("token", [""])[0]
            if q == token:
                self.send_response(302); self.send_header("Set-Cookie", f"agora={token}; Path=/; SameSite=Lax")
                self.send_header("Location", "/"); self.end_headers(); return False
            if f"agora={token}" in self.headers.get("Cookie", ""): return True
            body = b"Agora: open the link with the token shown in the terminal that started it."
            self.send_response(403); self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return False
        def do_GET(self):
            if not self._authed(): return
            if self.path.startswith("/state"):
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                return self._send(json.dumps(agora.snapshot(since=int(q.get("since", ["-1"])[0]), terms=q.get("terms", ["0"])[0] == "1")).encode(), "application/json")
            if self.path.startswith("/export"):
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query); what = q.get("what", ["all"])[0]; sid = q.get("id", [agora.s.id])[0]
                sess = agora.s if sid == agora.s.id else Session.load(sid)
                if not sess: return self._send(b"no such conversation", "text/plain")
                body = sess.export_md(what).encode("utf-8")
                safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in (sess.title or "conversation"))[:60].strip() or "conversation"
                self.send_response(200); self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="agora-{safe}-{what}.md"')
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
            if self.path.startswith("/ls"):
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query).get("path", [""])[0]
                return self._send(json.dumps(list_dir(q)).encode(), "application/json")
            self._send(PAGE.encode(), "text/html; charset=utf-8")
        def do_POST(self):
            if not self._authed(): return
            n = int(self.headers.get("Content-Length", 0)); data = json.loads(self.rfile.read(n) or b"{}")
            {"/config": lambda: agora.configure(data), "/start": agora.start, "/pause": agora.pause,
             "/vote": agora.call_vote, "/stop": agora.stop, "/shutdown": agora.shutdown,
             "/say": lambda: agora.say(data.get("text", ""), data.get("target", "")),
             "/session/new": agora.new_session, "/session/open": lambda: agora.open_session(data.get("id", "")),
             "/session/delete": lambda: agora.delete_session(data.get("id", "")),
             "/template": lambda: agora.apply_template(data.get("name", "")),
             "/template/save": lambda: agora.save_template(data.get("name", "")),
             "/template/delete": lambda: delete_user_template(data.get("name", "")),
             "/telegram": agora.send_link}.get(self.path, lambda: None)()
            full = self.path in ("/session/open", "/session/new", "/session/delete", "/template")
            self._send(json.dumps(agora.snapshot(since=-1 if full else 10**9)).encode(), "application/json")
    return H


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--local-only", action="store_true", help="do not accept connections from other devices")
    a = ap.parse_args()
    agora = Agora()
    token_file = HERE.with_name("agora_token.txt")
    if token_file.exists(): token = token_file.read_text(encoding="utf-8").strip()
    else: token = secrets.token_urlsafe(6); token_file.write_text(token, encoding="utf-8")
    host = "127.0.0.1" if a.local_only else "0.0.0.0"
    srv = ThreadingHTTPServer((host, a.port), make_handler(agora, token))
    url = f"http://127.0.0.1:{a.port}/?token={token}"
    lan = ts = ""
    if not a.local_only:
        try:
            sck = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); sck.connect(("8.8.8.8", 80)); lan = sck.getsockname()[0]; sck.close()
        except Exception: lan = ""
        if shutil.which("tailscale"):
            try: ts = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5).stdout.strip().splitlines()[0]
            except Exception: ts = ""
    agora.phone_url = f"http://{lan}:{a.port}/?token={token}" if lan else ""
    agora.away_url = f"http://{ts}:{a.port}/?token={token}" if ts else ""
    print(f"Agora build {BUILD}")
    print(f"Agora (desktop):            {url}")
    if agora.phone_url: print(f"Agora (phone, home wifi):   {agora.phone_url}")
    if agora.away_url:  print(f"Agora (phone, anywhere via Tailscale): {agora.away_url}")
    elif not a.local_only: print("Tailscale not found: install it on this PC and your phone to use Agora away from home.")
    print("Ctrl+C to quit")
    webbrowser.open(url)
    try: srv.serve_forever()
    except KeyboardInterrupt: agora.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
