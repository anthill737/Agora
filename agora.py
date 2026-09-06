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
import re
import secrets
import sys
import shutil
import socket
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_REPO = str(Path.home())   # the folder offered when a conversation chooses "A folder I choose"
BUILD = "2026-09-06.10"
TURN_TIMEOUT = 1800
HERE = Path(__file__).resolve()
RUNS = HERE.with_name("agora_runs")
ROOM = HERE.with_name("agora_room")   # the empty room agents sit in when a conversation points at no folder


def room_dir() -> str:
    """One shared, empty, disposable folder next to Agora. Created on demand, recreated if deleted, trusted for Codex once."""
    ROOM.mkdir(exist_ok=True)
    marker = ROOM / "README.txt"
    if not marker.exists():
        marker.write_text("This folder is Agora's empty room: the working directory for conversations that are not about any code. "
                          "There is nothing here to read. Agora recreates it when it is missing; deleting it is safe.\n", encoding="utf-8")
    return str(ROOM)


ASK = "Read the file {prompt_file} and respond exactly as it instructs. Your final message is your speech."
SESSIONS = HERE.with_name("agora_sessions")
TAIL = 400

# Provider definitions. Never shown in the UI.
#   cmd: {ask}, {model} and {seat_dir} (the agent's own folder with its prompt and memory files) are filled in.
#   speech: how the finished speech is extracted from the run.
#     "claude_stream" parses Claude's stream-json events; "stdout" uses stdout only.
PROVIDERS = {
    "Claude Code": {
        "exe": "claude", "speech": "claude_stream", "pkg": "@anthropic-ai/claude-code", "gate": True,
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
    "Copilot CLI": {
        # read-only tools are allowed without asking; write and shell need --allow-all-tools. -s prints only the answer.
        "exe": "copilot", "speech": "stdout", "pkg": "@github/copilot",
        # "auto" lets Copilot pick; which named models an account may use varies, so type one in the seat's Custom box if you need it.
        # Copilot may only touch files inside its working folder, so the seat's own folder (prompt + memory) is added explicitly.
        "cmd": 'copilot -p "{ask}" --model {model} -s --no-ask-user --no-auto-update --add-dir "{seat_dir}" --allow-all-tools',
        "resume": "",
        "ro_cmd": 'copilot -p "{ask}" --model {model} -s --no-ask-user --no-auto-update --add-dir "{seat_dir}"',
        "models": ["auto"],
    },
    "Gemini CLI": {
        "exe": "gemini", "speech": "stdout", "pkg": "@google/gemini-cli",
        "cmd": 'gemini -p "{ask}" -m {model} --yolo',
        "resume": "",
        "ro_cmd": 'gemini -p "{ask}" -m {model}',
        "models": ["gemini-3-pro", "gemini-3-flash", "gemini-3-flash-lite"],
    },
}

# How to get each CLI, shown under Settings > CLIs. Agora only runs CLIs; it never installs, updates, or logs in to them.
for _n, _inst, _login in [
    ("Claude Code", "npm install -g @anthropic-ai/claude-code", "claude   (then type /login)"),
    ("Codex (latest)", "Install Node.js from nodejs.org; npx comes with it and downloads Codex on first use", "npx -y @openai/codex@latest login"),
    ("Codex", "npm install -g @openai/codex", "codex login"),
    ("OpenCode", "npm install -g opencode-ai", "opencode auth login"),
    ("Copilot CLI", "npm install -g @github/copilot", "copilot login   (or set GH_TOKEN)"),
    ("Gemini CLI", "npm install -g @google/gemini-cli", "gemini   (then follow the sign-in prompt)"),
]: PROVIDERS[_n]["install"], PROVIDERS[_n]["login"] = _inst, _login

# ---------------------------------------------------------------- where each CLI lives
CLI_FILE = HERE.with_name("agora_clis.json")   # {"Claude Code": "C:\\full\\path\\claude.cmd", ...} for CLIs that are not on PATH


def cli_overrides() -> dict:
    try: return json.loads(CLI_FILE.read_text(encoding="utf-8")) if CLI_FILE.exists() else {}
    except Exception: return {}


def set_cli_path(provider: str, path: str) -> None:
    if provider not in PROVIDERS: return
    o = cli_overrides(); path = (path or "").strip().strip('"')
    if path: o[provider] = path
    else: o.pop(provider, None)
    CLI_FILE.write_text(json.dumps(o, indent=1), encoding="utf-8")


def exe_of(provider: str) -> str:
    """What Agora runs for this provider: the path saved in agora_clis.json if any, else the plain command name looked up on PATH."""
    return (cli_overrides().get(provider) or "").strip() or PROVIDERS[provider]["exe"]


def cli_path(provider: str) -> str | None:
    """The resolved executable, or None when Agora cannot find it."""
    return shutil.which(exe_of(provider), path=agent_path())


def agent_path() -> str:
    """PATH for everything Agora runs: this process's own, plus the folders the official installers use. A GUI
    launch or a bare shell never has those, and a CLI installed after Agora started is not on this process's
    PATH at all, so without this a fresh install stays invisible until Agora restarts."""
    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    for p in tool_dirs():
        if p.is_dir() and str(p) not in parts: parts.append(str(p))
    return os.pathsep.join(parts)


_NESTED = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")   # a claude started with these thinks it is nested and hangs


def child_env(provider: str) -> dict:
    """The environment each agent CLI gets: the user's own, with the PATH above, without Claude Code's nested-session
    markers, without color codes in the output, and with Claude's auto-updater off (many seats must not all update at once)."""
    env = dict(os.environ); env["PATH"] = agent_path()
    for k in _NESTED: env.pop(k, None)
    env.setdefault("NO_COLOR", "1")
    if provider == "Claude Code": env.setdefault("DISABLE_AUTOUPDATER", "1")
    key = SESSION_KEYS.get(provider)
    if key and KEY_ENV.get(provider): env[KEY_ENV[provider]] = key   # typed in Connections, this run only
    return env


def env_warnings(provider: str) -> list[list[str]]:
    """[level, text] notes about this machine's environment that change how a CLI will behave when Agora runs it."""
    w: list[list[str]] = []; e = os.environ
    if provider == "Claude Code":
        if e.get("ANTHROPIC_API_KEY"):
            w.append(["warn", "ANTHROPIC_API_KEY is set in Agora's environment. In non-interactive mode Claude Code always uses that key instead of your login, so Claude seats bill the key and fail if it is invalid. Unset it before starting Agora to use your login."])
        if e.get("ANTHROPIC_AUTH_TOKEN") or e.get("ANTHROPIC_BASE_URL"):
            w.append(["warn", "ANTHROPIC_AUTH_TOKEN or ANTHROPIC_BASE_URL is set, so Claude Code will send requests to that endpoint with that token, not to Anthropic with your login."])
        if e.get("CLAUDE_CODE_USE_BEDROCK") or e.get("CLAUDE_CODE_USE_VERTEX"):
            w.append(["warn", "CLAUDE_CODE_USE_BEDROCK or CLAUDE_CODE_USE_VERTEX is set: Claude Code will use that cloud provider, and OAuth login is not available there."])
        if sys.platform == "darwin" and e.get("SSH_CONNECTION"):
            w.append(["warn", "Agora was started over SSH. On macOS, Claude Code keeps its login in the Keychain, which is locked for SSH sessions, so Claude seats cannot read it. Start Agora from a terminal on the Mac itself."])
        if e.get("CLAUDECODE"):
            w.append(["info", "Agora was started from inside a Claude Code session. Agora removes that marker for its agents, otherwise Claude seats would hang at startup."])
    if provider == "Copilot CLI" and not any(e.get(k) for k in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")):
        w.append(["info", "Uses the login saved by 'copilot login'. A GH_TOKEN in the environment would take precedence."])
    return w


_GATES: dict[str, threading.Lock] = {k: threading.Lock() for k in PROVIDERS}   # one start at a time per gated provider
_AUTH_RE = re.compile(r"oauth|not logged in|/login\b|log in|authenticat|unauthori[sz]ed|\b401\b|invalid api key|token.{0,30}(expired|refresh|revoked)|please run .?claude.?\s*(auth|login)", re.I)


def decode_out(b: bytes) -> str:
    """CLI output as text. UTF-8 first; a CLI that writes the Windows console code page instead gets a second try."""
    try: return b.decode("utf-8")
    except UnicodeDecodeError:
        import locale
        try: return b.decode(locale.getpreferredencoding(False))
        except Exception: return b.decode("utf-8", errors="replace")


def looks_like_auth_error(text: str) -> bool:
    return bool(_AUTH_RE.search(text or ""))


def auth_hint(provider: str, text: str) -> str:
    if not looks_like_auth_error(text): return ""
    if provider == "Claude Code":
        h = ("Claude Code could not use its login. Run 'claude' in a terminal and /login if it asks. If other Claude sessions are open, "
             "one of them may have rotated the login token; Agora starts Claude seats one at a time so its own seats do not do that to each other.")
        if sys.platform == "darwin" and os.environ.get("SSH_CONNECTION"): h += " Agora is running over SSH, where the macOS Keychain that holds the login is locked."
        return h
    if provider == "Copilot CLI": return "Copilot CLI is not logged in for this user. Run 'copilot login' in a terminal, or set GH_TOKEN."
    if provider in ("Codex", "Codex (latest)"): return "Codex is not logged in. Run 'codex login' in a terminal."
    if provider == "OpenCode": return "OpenCode has no credentials for that model. Run 'opencode auth login'."
    if provider == "Gemini CLI": return "Gemini CLI is not logged in. Run 'gemini' in a terminal and finish the sign-in."
    return ""


def build_cmd(provider: str, ask: str, model: str, readonly: bool, seat_dir: str = "") -> str:
    """The shell command for one turn, using the saved executable path when there is one."""
    prov = PROVIDERS[provider]
    cmd = prov["ro_cmd" if readonly else "cmd"].format(ask=ask, model=model, seat_dir=seat_dir or str(Path.home()))
    exe = exe_of(provider)
    if exe != prov["exe"] and cmd.startswith(prov["exe"] + " "): cmd = f'"{exe}"' + cmd[len(prov["exe"]):]
    return cmd


TESTS: dict[str, dict] = {}   # provider -> result of the last "Test" from Settings


def cli_test(provider: str, folder: str) -> None:
    """Ask the CLI one trivial question the way a seat would, read-only, and keep the verdict. Costs one tiny request."""
    prov = PROVIDERS.get(provider)
    if not prov: return
    if not cli_path(provider):
        TESTS[provider] = {"ok": False, "text": "not found", "secs": 0, "when": dt.datetime.now().strftime("%H:%M")}; return
    model = ARENA_MODEL.get(provider) or prov["models"][0]
    cwd = folder if Path(folder).is_dir() else str(Path.home())
    cmd = build_cmd(provider, "Reply with the single word OK and nothing else.", model, readonly=True, seat_dir=cwd)
    t0 = time.time(); gate = _GATES[provider] if prov.get("gate") else None
    got = bool(gate and gate.acquire(timeout=120))
    try:
        r = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, timeout=180, stdin=subprocess.DEVNULL, env=child_env(provider))
        out, err = decode_out(r.stdout or b""), decode_out(r.stderr or b""); final = ""
        if prov["speech"] == "claude_stream":
            for ln in out.splitlines():
                try: ev = json.loads(ln)
                except Exception: continue
                if ev.get("type") == "result": final = "" if ev.get("is_error") else (ev.get("result") or "")
                if ev.get("type") == "result" and ev.get("is_error"): err = (ev.get("result") or "") + "\n" + err
        else: final = out.strip()
        ok = r.returncode == 0 and bool(final.strip())
        tail = "\n".join([ln for ln in (err.strip().splitlines() or out.strip().splitlines()) if ln.strip()][-6:])
        text = f"answered: {final.strip()[:80]}" if ok else (tail or f"exit code {r.returncode}, no output")
        hint = "" if ok else auth_hint(provider, out + "\n" + err)
    except subprocess.TimeoutExpired:
        ok, text, hint = False, "timed out after 180 s", ""
    except Exception as exc:  # noqa: BLE001
        ok, text, hint = False, f"{type(exc).__name__}: {exc}", ""
    finally:
        if got: gate.release()   # type: ignore[union-attr]
    TESTS[provider] = {"ok": ok, "text": text, "hint": hint, "secs": round(time.time() - t0, 1), "when": dt.datetime.now().strftime("%H:%M")}


DEFAULT_TOPIC = ("What is the most important thing about how you work that the other agents in this room do not know? Say only what you can observe about yourself right now or cite from public documentation, label each claim, and disagree openly.")
FOLDER_TOPIC = ("What is the most important thing about how you work that the other agents in this room do not know? Read this folder to check any claim you make, cite file and line, and disagree openly.")
DEFAULT_TOPICS = (DEFAULT_TOPIC, FOLDER_TOPIC)

FRAMING = """You are a speaker in a recorded council of AI coding agents. Each
speaker is a different model running in its own CLI. {where}

Knowability rule: label claims about yourself OBSERVED (visible in your
context, tools, or environment now), DOCUMENTED (public docs or source you
can cite), or GUESS. Never present a guess as fact.

You have your own memory of this conversation in a file that only you use.
Read it before you speak if you need the history; the prompt only carries
what was said since your last turn. The convener (Anthony) may interject at
any time; when he does, address what he said before anything else.

Rules: {verify}Disagree openly with any
speaker you think is wrong or bluffing. Stay concrete. Do not modify, create,
or delete any files; you are here to speak, not to work. No em dashes.

How to speak: ultra concise. This is a conversation, not a report. Talk the
way you would across a table. No headers, no bullet lists, no numbered lists,
no bold. Do not recap what has already been said. Make one point well, back it
with one citation, and stop. Every sentence must earn its place; if it can be
cut without losing the point, cut it.

Your final message is your speech; put nothing after it."""

WHERE_FOLDER = "All of you are sitting inside the same folder and may read its files to check claims."
WHERE_ROOM = ("All of you are sitting in an empty room. There are no files worth reading here and nothing to look up, "
              "so speak from what you know and label it honestly.")
VERIFY_FOLDER = "read files to verify claims and cite paths. "


def framing_for(room: bool) -> str:
    return FRAMING.format(where=WHERE_ROOM if room else WHERE_FOLDER, verify="" if room else VERIFY_FOLDER)


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
        if ev.get("is_error"): return f"[error] {ev.get('result') or ev.get('error') or 'unknown error'}", None
        return "\n[done]", ev.get("result") or ""
    if t == "system":
        # Claude emits many system events per turn; show only the one that names the model
        if not ev.get("model"): return None, None
        return f"[session {ev.get('model')}]", None
    return None, None



# ---------------------------------------------------------------- templates
CL, CX = "Claude Code", "Codex (latest)"
CLM, CXM = "claude-fable-5-1", "gpt-6-astra"
DEFAULT_SEATS = [{"name": "Claude", "provider": CL, "model": CLM, "stance": ""}, {"name": "Codex", "provider": CX, "model": CXM, "stance": ""}]
ARENA_MODEL = {CL: "claude-haiku-4-5", CX: "gpt-5.5", "Codex": "gpt-5.5", "Copilot CLI": "auto"}   # a game is many short turns, so cheap models by default
ARENA_TOPIC = ("A walled arena with a market stall, a training yard, and a healer's tent. Twenty strangers, "
               "one season. Gold buys goods and favors, training raises skills, fights cost health, and the dead "
               "stay dead. Every six rounds the World holds a vote: the character the others trust least is exiled. "
               "Win by being alive, rich, or beloved when the season ends.")


PALETTE = ["#22D3EE", "#F59E0B", "#A78BFA", "#34D399", "#F472B6", "#60A5FA", "#FB7185", "#FACC15", "#2DD4BF", "#C084FC",
           "#4ADE80", "#F97316", "#38BDF8", "#E879F9", "#A3E635", "#FB923C", "#818CF8", "#F43F5E", "#14B8A6", "#EAB308", "#94A3B8"]


def _seat(name: str, prov: str, stance: str = "") -> dict:
    return {"name": name, "provider": prov, "model": CLM if prov == CL else CXM, "stance": stance}


def color_seats(seats: list[dict]) -> list[dict]:
    """Give every seat without a color the first palette color no other seat is using, then cycle by position."""
    used = {x.get("color") for x in seats if x.get("color")}
    for i, x in enumerate(seats):
        if x.get("color"): continue
        x["color"] = next((c for c in PALETTE if c not in used), PALETTE[i % len(PALETTE)]); used.add(x["color"])
    return seats


# name, stat block. Used by the Arena template and by the Game switch, which hands one to every seat without a stance.
ARENA_CHARACTERS: list[tuple[str, str]] = [
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
]


def _first_cli() -> str:
    """Provider for a seat Agora adds on its own: the first one that is installed, else Claude Code."""
    return next((k for k in (CL, CX, "Codex", "Copilot CLI", "OpenCode", "Gemini CLI") if cli_path(k)), CL)


def _added_seat(prov: str, name: str = "", stance: str = "") -> dict:
    if not cli_path(prov): prov = _first_cli()
    return {"name": name, "provider": prov, "model": ARENA_MODEL.get(prov) or PROVIDERS[prov]["models"][0], "stance": stance}


def game_shape(s: "Session") -> None:
    """Make a session playable as a game. Adds what is missing and leaves what the user already set:
    a World seat as referee, at least four characters when none had a stance, a stat block for every
    character without one, the arena topic if the topic is still the council default, turns, six rounds."""
    seats = [dict(x) for x in s.seats]
    world = next((x for x in seats if s.label(x).lower() == "world" or x.get("stance") == WORLD_STANCE), None)
    if world is None:
        world = _added_seat(CL, "World", WORLD_STANCE); seats.insert(0, world)
    elif not world.get("stance"): world["stance"] = WORLD_STANCE
    s.referee = s.label(world)
    players = [x for x in seats if x is not world]
    if not any(x.get("stance") for x in players):
        while len(players) < 4:
            x = _added_seat(CL if len(players) % 2 == 0 else CX); seats.append(x); players.append(x)
    used = {(x.get("name") or "").lower() for x in seats}
    pool = [(n, st) for n, st in ARENA_CHARACTERS if n.lower() not in used]
    for x in players:
        if x.get("stance") or not pool: continue
        n, st = pool.pop(0); x["stance"] = st
        if not x.get("name"): x["name"] = n
    s.seats = color_seats(seats); s.skipped = [False] * len(seats)
    if s.topic.strip() in ("",) + DEFAULT_TOPICS: s.topic = ARENA_TOPIC
    if s.rounds == 3: s.rounds = 6
    s.mode = "turns"; s.room = True; s.readonly = True   # a game reads no files, so it sits in the room


def council_shape(s: "Session") -> None:
    """Undo game_shape: drop the World and any character Agora seated untouched, blank borrowed stat blocks,
    restore the council topic. Keeps every seat the user named or wrote a stance for."""
    by_name = {n.lower(): st for n, st in ARENA_CHARACTERS}; stances = {st for _, st in ARENA_CHARACTERS}
    seats = []
    for x in s.seats:
        if x.get("stance") == WORLD_STANCE: continue
        if by_name.get((x.get("name") or "").lower()) == x.get("stance"): continue
        seats.append({**x, "stance": "" if x.get("stance") in stances else x.get("stance", "")})
    names = {(x.get("name") or "").lower() for x in seats}
    for d in DEFAULT_SEATS:
        if len(seats) >= 2: break
        if d["name"].lower() not in names: seats.append(dict(d))
    s.seats = color_seats(seats); s.skipped = [False] * len(seats); s.referee = ""
    if s.topic.strip() == ARENA_TOPIC: s.topic = DEFAULT_TOPIC
    if s.rounds == 6: s.rounds = 3


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
        _seat(n, CL if i % 2 == 0 else CX, st) for i, (n, st) in enumerate(ARENA_CHARACTERS)],
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
VERSIONS: dict[str, dict] = {}     # provider -> {"installed": str, "latest": str, "error": str}
_ver_lock = threading.Lock()


def _ver_of(cmd: list[str]) -> tuple[bool, str]:
    """Run a command and return (exit ok, the version it printed). Some CLIs add a line about updates after the
    version, so take the first line that actually looks like a version rather than the last line."""
    try:
        cmd = [shutil.which(cmd[0], path=agent_path()) or cmd[0]] + cmd[1:]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=25, stdin=subprocess.DEVNULL, env=child_env(""))
        lines = [ln.strip() for ln in (r.stdout or r.stderr or "").strip().splitlines() if ln.strip()]
        best = next((ln for ln in lines if re.search(r"\d+\.\d+", ln)), lines[-1] if lines else "")
        return r.returncode == 0, best
    except Exception as exc:  # noqa: BLE001
        return False, type(exc).__name__


def check_installed() -> None:
    """Which CLIs Agora can run right now. Synchronous, a few seconds at most."""
    for name, prov in PROVIDERS.items():
        path = cli_path(name)
        ok, text = (True, "") if prov.get("isolated") and path else (_ver_of([path, "--version"]) if path else (False, ""))
        with _ver_lock:
            v = VERSIONS.setdefault(name, {}); v["installed"] = text if ok else ""
            v["error"] = "" if ok or not path else f"Found {path} but '--version' failed: {text or 'no output'}"


def check_latest() -> None:
    """Newest published version of each CLI, from npm, in the background. Never blocks the UI."""
    def work() -> None:
        for name, prov in PROVIDERS.items():
            if prov.get("isolated") or not prov.get("pkg") or not shutil.which("npm"): continue
            ok, latest = _ver_of(["npm", "view", prov["pkg"], "version"])
            with _ver_lock: VERSIONS.setdefault(name, {})["latest"] = latest if ok else ""
    threading.Thread(target=work, daemon=True).start()


def refresh_versions() -> None:
    threading.Thread(target=check_installed, daemon=True).start(); check_latest()

# ---------------------------------------------------------------- connections
# One place that knows, for every provider: where its CLI is, whether it is signed in, how to install it, and
# how to run its own sign-in. Everything here runs the vendor's CLI. Agora never talks to an auth server itself,
# never reimplements a sign-in, and never stores a credential.
TOOLS = HERE.with_name("agora_tools")   # per-user npm prefix, so an install never needs an administrator
NODE_URL = "https://nodejs.org/en/download"


def tool_dirs() -> list[Path]:
    """Folders the official installers write CLIs into. Searched as well as PATH, so a CLI installed after Agora
    started is found without restarting it: a new PATH only reaches processes started afterwards."""
    home = Path.home()
    d = [TOOLS, TOOLS / "bin", home / ".local" / "bin", home / ".codex" / "bin", home / "bin"]
    if os.name == "nt":
        d += [Path(os.environ.get("APPDATA", "x")) / "npm",
              Path(os.environ.get("LOCALAPPDATA", "x")) / "Programs" / "OpenAI" / "Codex" / "bin",
              home / "AppData" / "Local" / "Programs" / "OpenAI" / "Codex" / "bin"]
    else:
        d += [Path("/opt/homebrew/bin"), Path("/usr/local/bin"), home / ".npm-global" / "bin",
              home / ".claude" / "local", home / ".volta" / "bin",
              home / ".local" / "share" / "fnm" / "aliases" / "default" / "bin"]
        nvm = sorted((home / ".nvm" / "versions" / "node").glob("v*"))
        if nvm: d.append(nvm[-1] / "bin")
    return d


SESSION_KEYS: dict[str, str] = {}    # provider -> API key typed in Connections. Memory only, never written to disk.
# Codex is deliberately absent: Agora drives Codex through the ChatGPT subscription sign-in only, because that is
# the login its token handling below is built around.
KEY_ENV = {"Claude Code": "ANTHROPIC_API_KEY", "Copilot CLI": "COPILOT_GITHUB_TOKEN", "Gemini CLI": "GEMINI_API_KEY"}
COPILOT_ENV = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[=>()][0-9A-Za-z]?|[\x00-\x08\x0b\x0c\x0e-\x1f]")


def strip_ansi(s: str) -> str:
    return _ANSI.sub("", s.replace("\r\n", "\n").replace("\r", "\n"))


def _probe(argv: list[str], timeout: int = 30) -> tuple[int, str]:
    """Run a CLI's own status command. Returns (exit code, combined output); 127 when the CLI is not there."""
    exe = shutil.which(argv[0], path=agent_path())
    if not exe: return 127, ""
    try:
        r = subprocess.run([exe] + argv[1:], capture_output=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, env=child_env(""))
        return r.returncode, strip_ansi(decode_out((r.stdout or b"") + b"\n" + (r.stderr or b"")))
    except subprocess.TimeoutExpired:
        return -1, f"'{' '.join(argv)}' did not answer within {timeout} seconds."
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"


def _json_in(text: str) -> dict:
    a = text.find("{"); b = text.rfind("}")
    if a < 0 or b < a: return {}
    try: return json.loads(text[a:b + 1])
    except Exception: return {}


def probe_claude(exe: str) -> tuple[str, str]:
    """'claude auth status' answers with JSON: loggedIn, authMethod, email, subscriptionType."""
    rc, out = _probe([exe, "auth", "status"], 45)
    d = _json_in(out)
    if d:
        if d.get("loggedIn"):
            who = d.get("email") or d.get("authMethod") or "signed in"
            plan = d.get("subscriptionType") or d.get("apiProvider") or ""
            return "on", f"Signed in as {who}" + (f", {plan} plan" if plan else "")
        return "off", "Not signed in."
    low = out.lower()
    if "not logged in" in low or "/login" in low: return "off", "Not signed in."
    if rc == 0 and out.strip(): return "on", " ".join(out.split())[:120]
    return "unknown", (" ".join(out.split())[:160] or "'claude auth status' gave no answer.")


def probe_codex_files() -> tuple[str, str]:
    """Codex keeps its login in ~/.codex/auth.json. Read for the npx seat, which has no binary to ask."""
    f = Path.home() / ".codex" / "auth.json"
    if not f.exists(): return "off", "Not signed in."
    try: d = json.loads(f.read_text(encoding="utf-8"))
    except Exception: return "unknown", f"{f} cannot be read."
    if d.get("auth_mode") == "chatgpt" or d.get("tokens"): return "on", "Signed in with a ChatGPT account."
    if d.get("auth_mode") == "api_key" or d.get("OPENAI_API_KEY"): return "off", CODEX_API_KEY_NOTE
    return "off", "Not signed in."


def probe_codex(exe: str) -> tuple[str, str]:
    """'codex login status' prints how it is signed in, and fails when it is not."""
    rc, out = _probe([exe, "login", "status"], 45)
    line = " ".join(out.split())[:140]; low = out.lower()
    if "api key" in low: return "off", CODEX_API_KEY_NOTE
    if "not logged in" in low: return "off", "Not signed in."
    if rc == 0 and "logged in" in out.lower(): return "on", line
    if rc == 0 and line: return "on", line
    if rc != 0 and not line: return "off", "Not signed in."
    return "unknown", line or "'codex login status' gave no answer."


def probe_opencode(exe: str) -> tuple[str, str]:
    """'opencode auth list' prints the providers it holds credentials for."""
    rc, out = _probe([exe, "auth", "list"], 45)
    if rc != 0: return "unknown", " ".join(out.split())[:160] or "'opencode auth list' failed."
    names = [ln.strip() for ln in out.splitlines() if ln.strip() and not ln.lower().startswith(("credentials", "env", "~"))]
    if len(names) > 1: return "on", "Credentials for " + ", ".join(names[1:5])
    return "off", "No provider credentials yet."


def probe_gemini(exe: str) -> tuple[str, str]:
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        if os.environ.get(k): return "on", f"Using {k} from the environment."
    rc, out = _probe([exe, "auth", "status"], 30)
    low = out.lower()
    if rc == 0 and ("authenticated" in low or "signed in" in low or "logged in" in low): return "on", " ".join(out.split())[:140]
    if any(p.exists() for p in (Path.home() / ".gemini" / "oauth_creds.json", Path.home() / ".gemini" / "google_accounts.json")):
        return "on", "Signed in with Google; credentials are in ~/.gemini."
    if rc == 0: return "unknown", " ".join(out.split())[:160] or "Gemini CLI reported no status."
    return "off", "Not signed in."


def probe_copilot(exe: str) -> tuple[str, str]:
    """Copilot CLI has no sign-in status command and keeps its token in the operating system credential store,
    which Agora cannot read. An environment token is a definite answer; otherwise only a real request can tell."""
    if SESSION_KEYS.get("Copilot CLI"): return "on", "Using the API key you typed here, this session only."
    for k in COPILOT_ENV:
        if os.environ.get(k): return "on", f"Using {k} from the environment, which takes precedence over a saved login."
    t = TESTS.get("Copilot CLI")
    if t and t.get("ok"): return "on", f"Answered a test prompt at {t.get('when', '')}."
    if t and not t.get("ok"): return "off", (t.get("hint") or t.get("text") or "The check failed.")
    return "ask", "No sign-in status command exists for this CLI, so Agora cannot tell without asking it something. Press Check: one tiny request."


CONN: dict[str, dict] = {}          # provider -> {state, detail, version, checked, checking}
_conn_lock = threading.Lock()
CONN_TTL = 90.0


def _set_conn(name: str, **kw) -> None:
    with _conn_lock: CONN.setdefault(name, {}).update(kw)


def check_conn(name: str, paid: bool = False) -> dict:
    """One provider's state, from its own CLI. paid lets the Copilot check spend one tiny request."""
    prov = PROVIDERS.get(name)
    if not prov: return {}
    _set_conn(name, checking=True)
    try:
        path = cli_path(name)
        if not path:
            _set_conn(name, state="none", detail="Not installed.", version="", checked=time.time()); return CONN[name]
        ver = ""
        if not prov.get("isolated"):
            ok, ver = _ver_of([path, "--version"])
            if not ok: ver = ""
        with _ver_lock: VERSIONS.setdefault(name, {})["installed"] = ver
        if SESSION_KEYS.get(name) and name != "Copilot CLI":
            _set_conn(name, state="on", version=ver, checked=time.time(),
                      detail=f"Using the API key you typed here as {KEY_ENV.get(name, 'the key')}, this session only.")
            return CONN[name]
        if name == "Claude Code": state, detail = probe_claude(path)
        elif name == "Codex": state, detail = probe_codex(path)
        elif name == "Codex (latest)":
            cx = cli_path("Codex")
            state, detail = probe_codex(cx) if cx else probe_codex_files()
            detail = detail.rstrip(".") + ". This seat runs the newest Codex through npx and uses the same login."
        elif name == "Copilot CLI":
            if paid: cli_test(name, room_dir())
            state, detail = probe_copilot(path)
        elif name == "OpenCode": state, detail = probe_opencode(path)
        elif name == "Gemini CLI": state, detail = probe_gemini(path)
        else: state, detail = "unknown", ""
        _set_conn(name, state=state, detail=detail, version=ver, checked=time.time())
        return CONN[name]
    finally:
        _set_conn(name, checking=False)


def check_all_conns(force: bool = False) -> None:
    """Every provider at once, in the background, so the rows are already coloured when the user looks."""
    for name in PROVIDERS:
        c = CONN.get(name, {})
        if not force and c.get("checked") and time.time() - c["checked"] < CONN_TTL: continue
        _set_conn(name, checking=True, state=c.get("state", "checking"))
        threading.Thread(target=check_conn, args=(name,), daemon=True).start()


def conn_state(name: str) -> str:
    return (CONN.get(name) or {}).get("state", "checking")


# ---------------------------------------------------------------- Codex tokens
# Codex refresh tokens rotate: the moment one process refreshes, the token every other process holds is dead. Several
# Codex seats starting at once therefore race, all but one lose with a 401, and a losing writer can leave
# ~/.codex/auth.json half written. Agora avoids the race instead of retrying through it: one priming prompt runs
# through Codex alone first, so exactly one process does the refresh, and the file it leaves behind is copied aside.
# If a seat is refused anyway, the conversation pauses, the copy goes back, the priming prompt runs again, and the
# conversation resumes by itself. Seats still run fully in parallel; only the priming is on its own.
CODEX_PROVIDERS = ("Codex", "Codex (latest)")
CODEX_AUTH = Path.home() / ".codex" / "auth.json"
CODEX_BACKUP = HERE.with_name("agora_codex_auth.bak")
CODEX_PRIME_TTL = 1500.0     # re-prime if the last one is older than this, so a stale access token is refreshed alone
CODEX_API_KEY_NOTE = ("Signed in with an API key. Agora drives Codex through the ChatGPT subscription sign-in, so press "
                      "Sign in and choose your ChatGPT account.")
_prime_lock = threading.Lock()
_LAST_PRIME: dict[str, float] = {}


def codex_seats(seats: list[dict]) -> list[str]:
    return [x["provider"] for x in seats if x["provider"] in CODEX_PROVIDERS]


def one_codex(seats: list[dict]) -> str:
    """The single Codex install a conversation may use. Mixing the pinned global CLI and the npx latest in one
    conversation means two programs sharing one rotating token, which is the race this avoids."""
    kinds = set(codex_seats(seats))
    if not kinds: return ""
    if len(kinds) == 1: return next(iter(kinds))
    return "Codex" if cli_path("Codex") else "Codex (latest)"


def backup_codex_auth() -> bool:
    try:
        if CODEX_AUTH.exists() and CODEX_AUTH.stat().st_size > 0:
            CODEX_BACKUP.write_bytes(CODEX_AUTH.read_bytes()); return True
    except Exception: pass
    return False


def restore_codex_auth() -> bool:
    try:
        if CODEX_BACKUP.exists() and CODEX_BACKUP.stat().st_size > 0:
            CODEX_AUTH.parent.mkdir(parents=True, exist_ok=True)
            CODEX_AUTH.write_bytes(CODEX_BACKUP.read_bytes()); return True
    except Exception: pass
    return False


def codex_prime(provider: str, workdir: str, force: bool = False) -> tuple[bool, str]:
    """One short prompt through Codex, alone, so a single process performs any token refresh. On success the fresh
    credentials are copied aside. Returns (worked, what it said)."""
    if provider not in CODEX_PROVIDERS: return True, ""
    with _prime_lock:
        if not force and time.time() - _LAST_PRIME.get(provider, 0) < CODEX_PRIME_TTL: return True, "primed recently"
        model = ARENA_MODEL.get(provider) or PROVIDERS[provider]["models"][-1]
        cmd = build_cmd(provider, "Reply with the single word READY and nothing else.", model, readonly=True, seat_dir=workdir)
        try:
            r = subprocess.run(cmd, cwd=workdir, shell=True, capture_output=True, timeout=300,
                               stdin=subprocess.DEVNULL, env=child_env(provider))
            out = decode_out((r.stdout or b"") + b"\n" + (r.stderr or b""))
        except subprocess.TimeoutExpired:
            return False, "Codex did not answer the priming prompt within five minutes."
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        if r.returncode == 0 and not looks_like_auth_error(out):
            _LAST_PRIME[provider] = time.time(); backup_codex_auth()
            return True, " ".join(out.split())[:120]
        return False, " ".join(strip_ansi(out).split())[:200] or f"exit code {r.returncode}"


def codex_signed_out(text: str) -> bool:
    """The refused-because-signed-out answer, as Codex words it."""
    t = (text or "").lower()
    return ("401" in t or "unauthorized" in t or "not logged in" in t or "please run codex login" in t
            or "refresh token" in t or ("token" in t and "expired" in t))


# ---------------------------------------------------------------- sign in, in a real terminal
def login_argv(name: str) -> list[str]:
    """Each CLI's own documented sign-in command. Device-code flows where the CLI offers one: they show a code
    that can be entered on any device, so signing in works from a phone as well as from this machine."""
    return {
        "Claude Code": ["claude", "auth", "login", "--claudeai"],
        "Codex": ["codex", "login", "--device-auth"],
        "Codex (latest)": ["npx", "-y", "@openai/codex@latest", "login", "--device-auth"],
        "Copilot CLI": ["copilot", "login", "--device-code"],
        "OpenCode": ["opencode", "auth", "login"],
        "Gemini CLI": ["gemini"],
    }[name]


WATCH: dict[str, dict] = {}   # provider -> {"until": t, "since": t} while Agora waits for a sign-in to finish
WATCH_EVERY, WATCH_FOR = 15.0, 600.0


def watch_login(provider: str) -> None:
    """After the terminal opens, ask the CLI every fifteen seconds for ten minutes whether it is signed in yet,
    so the row turns green on its own without the user coming back to press anything."""
    if provider in WATCH and WATCH[provider]["until"] > time.time():
        WATCH[provider]["until"] = time.time() + WATCH_FOR; return
    WATCH[provider] = {"since": time.time(), "until": time.time() + WATCH_FOR}
    def work() -> None:
        while time.time() < WATCH[provider]["until"]:
            time.sleep(WATCH_EVERY)
            if provider not in WATCH: return
            check_conn(provider)
            if conn_state(provider) == "on": break
        WATCH.pop(provider, None)
    threading.Thread(target=work, daemon=True).start()


def _term_line(argv: list[str]) -> str:
    exe = shutil.which(argv[0], path=agent_path()) or argv[0]
    parts = [exe] + argv[1:]
    if os.name == "nt": return subprocess.list2cmdline(parts)
    import shlex
    return " ".join(shlex.quote(p) for p in parts)


def open_terminal(provider: str) -> dict:
    """Open a real terminal window with the CLI's own sign-in already running. The CLI opens the browser or
    prints its device code there, exactly as it does when the user runs it themselves."""
    if provider not in PROVIDERS: return {"ok": False, "error": "Unknown provider."}
    if not cli_path(provider) and provider != "Codex (latest)":
        return {"ok": False, "error": "Install the CLI first."}
    line = _term_line(login_argv(provider))
    env = child_env(provider)
    try:
        if os.name == "nt":
            subprocess.Popen(f'start "Agora: sign in to {provider}" cmd /k {line}', shell=True, env=env, cwd=str(Path.home()))
        elif sys.platform == "darwin":
            esc = line.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.Popen(["osascript", "-e", f'tell application "Terminal" to do script "{esc}"',
                              "-e", 'tell application "Terminal" to activate'], env=env)
        else:
            term = next((t for t in ("x-terminal-emulator", "gnome-terminal", "konsole", "xfce4-terminal", "xterm") if shutil.which(t)), "")
            if not term:
                return {"ok": False, "error": "No terminal program was found on this machine. Run this yourself in any terminal: " + line, "cmd": line}
            subprocess.Popen([term, "-e", "bash", "-c", f"{line}; echo; read -p 'Press Enter to close '"], env=env)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Could not open a terminal ({type(exc).__name__}). Run this yourself in any terminal: {line}", "cmd": line}
    watch_login(provider)
    return {"ok": True, "cmd": line}


# ---------------------------------------------------------------- install
def install_argv(name: str) -> tuple[list[str], str]:
    """(argv, note). The vendor's own installer where there is one, npm into Agora's own per-user prefix
    otherwise, so an install never needs an administrator and never touches a CLI already on the machine."""
    if name == "Claude Code":
        if os.name == "nt": return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", "irm https://claude.ai/install.ps1 | iex"], "Anthropic's own installer."
        return ["bash", "-c", "curl -fsSL https://claude.ai/install.sh | bash"], "Anthropic's own installer."
    if name == "Codex":
        if os.name == "nt": return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", "irm https://chatgpt.com/codex/install.ps1 | iex"], "OpenAI's own installer."
        return ["sh", "-c", "curl -fsSL https://chatgpt.com/codex/install.sh | sh"], "OpenAI's own installer."
    return (["npm", "install", "-g", "--prefix", str(TOOLS), PROVIDERS[name]["pkg"]],
            f"npm, into {TOOLS}, so it needs no administrator and leaves anything already installed alone.")


def npm_missing() -> bool:
    return shutil.which("npm", path=agent_path()) is None


JOBS: dict[str, "Install"] = {}   # provider -> the install running or last finished
_jobs_lock = threading.Lock()


class Install:
    """One install, with its output streamed to the page under the row."""

    MAX = 900.0

    def __init__(self, provider: str, argv: list[str]) -> None:
        self.provider, self.argv = provider, argv
        self.started = time.time(); self.done = False; self.rc: int | None = None; self.note = ""
        self.lines: collections.deque = collections.deque(maxlen=300)
        self.lock = threading.Lock()
        exe = shutil.which(argv[0], path=agent_path()) or argv[0]
        self.proc = subprocess.Popen([exe] + argv[1:], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, env=child_env(provider), cwd=str(Path.home()))
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        try:
            for raw in self.proc.stdout:  # type: ignore[union-attr]
                ln = strip_ansi(decode_out(raw)).rstrip("\n")
                with self.lock:
                    for part in ln.split("\n"): self.lines.append(part)
                if time.time() - self.started > self.MAX: self.cancel(); break
        except Exception: pass
        finally:
            try: self.rc = self.proc.wait(timeout=15)
            except Exception: self.rc = None
            self.done = True
            path = cli_path(self.provider)
            self.note = ("Installed." if path else "The installer finished but Agora still cannot find the CLI. "
                         "If it printed a folder, paste the full path to the program below.") if self.rc == 0 else "The installer failed."
            check_conn(self.provider)

    def cancel(self) -> None:
        try:
            if os.name == "nt": subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True, timeout=15)
            else: self.proc.kill()
        except Exception: pass
        self.done = True

    def view(self) -> dict:
        with self.lock:
            return {"provider": self.provider, "done": self.done, "rc": self.rc, "note": self.note,
                    "secs": int(time.time() - self.started), "cmd": " ".join(self.argv),
                    "lines": [ln for ln in list(self.lines)[-40:] if ln.strip()][-24:]}


def start_install(provider: str) -> dict:
    if provider not in PROVIDERS: return {"ok": False, "error": "Unknown provider."}
    if provider == "Codex (latest)":
        return {"ok": False, "error": "Nothing to install: this seat runs the newest Codex through npx, which comes with Node.js."}
    if cli_path(provider):
        return {"ok": False, "error": "That CLI is already installed. Agora never reinstalls or updates a CLI."}
    with _jobs_lock:
        j = JOBS.get(provider)
        if j and not j.done: return {"ok": True}
    argv, _ = install_argv(provider)
    if argv[0] == "npm" and npm_missing():
        return {"ok": False, "error": f"This CLI installs with npm, which comes with Node.js, and Node.js is not on this machine. Install Node.js from {NODE_URL}, then press Check again.", "node": NODE_URL}
    TOOLS.mkdir(exist_ok=True)
    try: job = Install(provider, argv)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Could not start the installer: {type(exc).__name__}: {exc}"}
    with _jobs_lock: JOBS[provider] = job
    return {"ok": True}


def set_session_key(provider: str, key: str) -> dict:
    """An API key for this run only: held in memory, given to the CLIs Agora starts, never written to disk."""
    if provider not in KEY_ENV: return {"ok": False, "error": "That CLI takes no API key."}
    key = (key or "").strip()
    targets = [provider]
    for t in targets:
        if key: SESSION_KEYS[t] = key
        else: SESSION_KEYS.pop(t, None)
    for t in targets: check_conn(t)
    return {"ok": True, "has_key": bool(key)}


def connections() -> dict:
    """Everything the Connections screen shows. No secret ever leaves this process."""
    ov = cli_overrides(); out = {}
    for k, v in PROVIDERS.items():
        c = CONN.get(k, {}); path = cli_path(k); j = JOBS.get(k)
        state = c.get("state", "checking")
        if not path and k != "Codex (latest)": state = "none"
        if c.get("checking") and state != "none": state = "checking"
        w = WATCH.get(k)
        out[k] = {"models": v["models"], "installed": path is not None, "path": path or "", "exe_default": v["exe"],
                  "override": ov.get(k, ""), "isolated": bool(v.get("isolated")), "pkg": v.get("pkg", ""),
                  "state": state, "detail": c.get("detail", ""), "version": VERSIONS.get(k, {}).get("installed", ""),
                  "latest": VERSIONS.get(k, {}).get("latest", ""), "warnings": env_warnings(k),
                  "key_env": KEY_ENV.get(k, ""), "has_key": bool(SESSION_KEYS.get(k)),
                  "can_install": path is None and k != "Codex (latest)", "npm": not npm_missing(), "node_url": NODE_URL,
                  "install_note": "Runs the newest Codex through npx; there is nothing to install beyond Node.js." if k == "Codex (latest)" else install_argv(k)[1],
                  "login_cmd": " ".join(login_argv(k)), "waiting": bool(w and w["until"] > time.time()),
                  "install": j.view() if j else None}
    return out


# ---------------------------------------------------------------- telegram
TG_FILE = HERE.with_name("agora_telegram.json")


def tg_config() -> dict:
    if not TG_FILE.exists():
        TG_FILE.write_text(json.dumps({"token": "", "chat_id": "", "notify_on_finish": True}, indent=1), encoding="utf-8")
        return {"token": "", "chat_id": "", "notify_on_finish": True}
    try: return json.loads(TG_FILE.read_text(encoding="utf-8"))
    except Exception: return {"token": "", "chat_id": "", "notify_on_finish": True}


def _tg_call(token: str, method: str, payload: dict | None = None) -> dict:
    """One Bot API call. Telegram answers 4xx with a JSON body that explains why; return that instead of raising."""
    import urllib.error, urllib.request
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                 data=json.dumps(payload or {}).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try: return json.loads(e.read().decode())
        except Exception: return {"ok": False, "description": f"HTTP {e.code}"}


def tg_state() -> dict:
    """What the dashboard needs to know about the Telegram link. The token itself never goes to the browser."""
    c = tg_config(); tok = (c.get("token") or "").strip(); chat = str(c.get("chat_id") or "").strip()
    return {"tg_ready": bool(tok and chat), "tg_has_token": bool(tok), "tg_chat": chat,
            "tg_notify": bool(c.get("notify_on_finish", True)), "tg_bot": c.get("bot", "")}


def _tg_token(given: str) -> str:
    """The token typed in the wizard, or the saved one when the box was left empty."""
    return (given or "").strip() or (tg_config().get("token") or "").strip()


def tg_check(token: str) -> dict:
    """Step 1: is this a real bot token? Asks Telegram for the bot's name."""
    tok = _tg_token(token)
    if not tok: return {"ok": False, "error": "Paste the token BotFather gave you first."}
    if ":" not in tok or len(tok) < 30: return {"ok": False, "error": "That does not look like a bot token. It has digits, a colon, then about 35 letters and digits."}
    try: r = _tg_call(tok, "getMe")
    except Exception as exc: return {"ok": False, "error": f"Could not reach Telegram: {type(exc).__name__}"}   # noqa: BLE001
    if not r.get("ok"): return {"ok": False, "error": "Telegram rejected that token" + (f": {r.get('description')}" if r.get("description") else ".") + " Copy it again from BotFather; a missing character is the usual cause."}
    b = r.get("result", {})
    return {"ok": True, "bot": b.get("username", ""), "name": b.get("first_name", "")}


def tg_find(token: str) -> dict:
    """Step 2: who has messaged the bot? One getUpdates call, no polling. If another program is reading this bot
    Telegram answers 409, and the wizard falls back to the manual way."""
    tok = _tg_token(token)
    if not tok: return {"ok": False, "error": "Do step 1 first."}
    try: r = _tg_call(tok, "getUpdates", {"timeout": 0, "limit": 100, "allowed_updates": ["message"]})
    except Exception as exc: return {"ok": False, "error": f"Could not reach Telegram: {type(exc).__name__}"}   # noqa: BLE001
    if not r.get("ok"):
        d = r.get("description") or ""
        if "409" in d or "Conflict" in d or "terminated by other" in d or "webhook" in d.lower():
            return {"ok": False, "conflict": True, "error": "Another program is already reading this bot (or it has a webhook), so Agora cannot look. Use the manual way below."}
        return {"ok": False, "error": f"Telegram said: {d or 'unknown error'}"}
    chats: dict[str, dict] = {}
    for u in r.get("result", []):
        m = u.get("message") or u.get("edited_message") or {}
        c = m.get("chat") or {}
        if not c.get("id"): continue
        name = c.get("title") or " ".join(x for x in (c.get("first_name"), c.get("last_name")) if x) or c.get("username") or str(c["id"])
        k = str(c["id"])
        if k not in chats or len(name) > len(chats[k]["name"]): chats[k] = {"id": k, "name": name, "kind": c.get("type", "")}
    return {"ok": True, "chats": list(chats.values())}


def tg_test(token: str, chat_id: str) -> dict:
    tok = _tg_token(token); chat = (chat_id or "").strip()
    if not tok: return {"ok": False, "error": "Do step 1 first."}
    if not chat: return {"ok": False, "error": "Agora needs your chat id first (step 2)."}
    try: r = _tg_call(tok, "sendMessage", {"chat_id": chat, "text": "Agora is connected. You will get a message here when a conversation finishes, and the phone link when you ask for it.", "disable_web_page_preview": True})
    except Exception as exc: return {"ok": False, "error": f"Could not reach Telegram: {type(exc).__name__}"}   # noqa: BLE001
    if r.get("ok"): return {"ok": True}
    d = r.get("description") or "unknown error"
    hint = " Open a chat with your bot in Telegram and press Start first; a bot cannot message you until you do." if "chat not found" in d.lower() or "blocked" in d.lower() else ""
    return {"ok": False, "error": f"Telegram said: {d}.{hint}"}


def tg_save(token: str, chat_id: str, notify: bool, bot: str = "") -> dict:
    tok = _tg_token(token); chat = (chat_id or "").strip()
    if not tok or not chat: return {"ok": False, "error": "A checked token and a chat id are both needed before saving."}
    cfg = tg_config(); cfg.update({"token": tok, "chat_id": chat, "notify_on_finish": bool(notify)})
    if bot: cfg["bot"] = bot
    TG_FILE.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    return {"ok": True}


def tg_fill_bot() -> None:
    """A config written by hand has no bot name; look it up once in the background so Settings can show it."""
    c = tg_config()
    if not (c.get("token") or "").strip() or c.get("bot"): return
    def work() -> None:
        r = tg_check("")
        if r.get("ok") and r.get("bot"):
            c2 = tg_config(); c2["bot"] = r["bot"]; TG_FILE.write_text(json.dumps(c2, indent=1), encoding="utf-8")
    threading.Thread(target=work, daemon=True).start()


def tg_clear() -> dict:
    """Disconnect: forget the bot and chat. The bot itself still exists in Telegram; delete it with BotFather if you want."""
    TG_FILE.write_text(json.dumps({"token": "", "chat_id": "", "notify_on_finish": True}, indent=1), encoding="utf-8")
    return {"ok": True}


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
        self.room = bool(d.get("room", not d))   # new conversations sit in the room; older saved ones keep their folder
        self.seats = color_seats(d.get("seats", [dict(x) for x in DEFAULT_SEATS]))
        self.topic = d.get("topic", DEFAULT_TOPIC); self.extra = d.get("extra", "")
        self.rounds = d.get("rounds", 3); self.readonly = bool(d.get("readonly", True)) or self.room   # the room is always read-only
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

    @property
    def workdir(self) -> str:
        """Where the CLIs run: the shared empty room, or the folder the user chose."""
        return room_dir() if self.room else self.repo

    @property
    def place(self) -> str:
        return "nowhere in particular (Agora's empty room)" if self.room else self.repo

    def to_dict(self) -> dict:
        return {"title": self.title, "created": self.created, "repo": self.repo, "room": self.room, "seats": self.seats,
                "topic": self.topic, "extra": self.extra, "rounds": self.rounds, "readonly": self.readonly, "mode": self.mode,
                "max_messages": self.max_messages, "max_minutes": self.max_minutes, "started_at": self.started_at, "framing": self.framing, "referee": self.referee,
                "transcript": self.transcript, "turn": self.turn, "status": self.status,
                "rounds_done": self.rounds_done, "skipped": self.skipped,
                "cli_sessions": self.cli_sessions, "last_seen": self.last_seen}

    def save(self, transcript_md: bool = False) -> None:
        (self.dir / "session.json").write_text(json.dumps(self.to_dict()), encoding="utf-8")
        if not transcript_md: return
        md = [f"# {self.title or 'Conversation'}", f"Created: {self.created}", f"Folder: {self.place}", "",
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
        md = [f"# {self.title or 'Conversation'}", f"Created: {self.created}  ", f"Mode: {'open floor' if self.mode == 'open' else 'take turns'}  ", f"Folder: {self.place}", "",
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
        self.notice = ""                 # why the conversation is paused, when it paused itself
        self.recovering = False
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
            if self.busy() or len(s.seats) < 2 or not Path(s.workdir).is_dir(): return
            if s.status == "paused" and self.thread and self.thread.is_alive():
                if self.notice:                      # resuming after a Codex sign-out: prove it works before carrying on
                    if not self.prime_codex(force=True): return
                    self.notice = ""
                self.pause_flag.clear(); s.status = "running"; s.save(); return
            one = one_codex(s.seats)
            if one and len(set(codex_seats(s.seats))) > 1:
                for x in s.seats:
                    if x["provider"] in CODEX_PROVIDERS and x["provider"] != one: x["provider"] = one
                self._record("Agora", f"Every Codex seat now runs through {one}. One conversation uses one Codex install, "
                                      "because two of them share the same rotating login token.", "system")
            self.notice = ""
            if s.status == "done": s.rounds_done = sum(1 for e in s.transcript if e["kind"] == "speech") // max(1, len(s.seats))
            if s.status in ("done", "stopped"): s.skipped = [False] * len(s.seats)   # benched seats get another chance on Continue
            for f in (self.stop_flag, self.pause_flag, self.vote_flag): f.clear()
            if not s.title: s.title = " ".join(s.topic.split()[:8])
            s.status = "running"; s.started_at = time.time(); s.save()
            for i in range(len(s.seats)): s.seat_dir(i)   # memory files exist before anyone speaks
            if any(PROVIDERS[x["provider"]]["exe"] in ("codex", "npx") for x in s.seats):
                note = ensure_codex_trust(s.workdir)
                if note: self._record("Agora", note, "system")
            if not self.terms: self._reset_terms()
        self.prime_codex()
        self.thread = threading.Thread(target=self._run, daemon=True); self.thread.start()


    def pause(self) -> None:
        with self.lock:
            if self.s.status == "running": self.pause_flag.set(); self.s.status = "paused"; self.s.save()


    def prime_codex(self, force: bool = False) -> bool:
        """Run the priming prompt before any turn or batch that includes Codex seats, and keep a copy of the
        credentials it leaves behind. Nothing else is running while this happens."""
        prov = one_codex(self.s.seats)
        if not prov: return True
        ok, msg = codex_prime(prov, self.s.workdir, force=force)
        if not ok: self._term(0, f"[Codex priming failed: {msg}]")
        return ok


    def codex_recover(self) -> None:
        """A Codex seat was refused as signed out. Pause at once, put the saved credentials back, prime again, and
        carry on if that worked. If it did not, stay paused and say so plainly."""
        with self.lock:
            if self.recovering or self.stop_flag.is_set(): return
            self.recovering = True
        try:
            was_running = self.s.status == "running"
            self.pause()
            self._record("Agora", "A Codex seat was refused as signed out. The conversation is paused while Agora puts "
                                  "the saved Codex credentials back and primes them again.", "system")
            restored = restore_codex_auth()
            ok = self.prime_codex(force=True)
            if ok:
                self.notice = ""
                self._record("Agora", "Codex answered again" + (" after its saved credentials were restored" if restored else "") +
                                      ". The conversation continues.", "system")
                with self.lock:
                    if was_running and not self.stop_flag.is_set():
                        self.pause_flag.clear(); self.s.status = "running"; self.s.save()
            else:
                self.notice = "Codex signed out, sign in and press Resume"
                self._record("Agora", "Codex is signed out and Agora could not sign it back in. Open Settings, Connections, "
                                      "press Sign in for Codex, then press Resume.", "system")
                check_conn(one_codex(self.s.seats) or "Codex")
        finally:
            with self.lock: self.recovering = False


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
        parts = [GAME_FRAMING if game else framing_for(s.room), f"\n{'The arena and its rules' if game else 'Topic'}:\n{s.topic}\n"]
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
        cmd = build_cmd(seat["provider"], ASK.format(prompt_file=prompt_file), seat["model"], s.readonly or s.room, str(s.seat_dir(i)))
        sid = s.cli_sessions.get(str(i))
        if sid and prov.get("resume"): cmd += prov["resume"].format(sid=sid)
        return cmd


    def _set_current(self) -> None:
        names = [self.s.label(self.s.seats[j]) for j in sorted(self.speaking) if j < len(self.s.seats)]
        self.current = ", ".join(names) if names else None


    def _speak(self, i: int, seat: dict, instruction: str) -> str:
        s = self.s; who = s.label(seat); prov = PROVIDERS[seat["provider"]]
        if self.stop_flag.is_set(): return f"[{who} was not asked: the conversation was stopped]"
        if cli_path(seat["provider"]) is None:
            self._term(i, f"'{exe_of(seat['provider'])}' was not found. Open Settings > CLIs to connect it.")
            return f"[{who} returned no answer. '{exe_of(seat['provider'])}' was not found. Open Settings > CLIs to connect it.]"
        n = s.turn + 1
        pfile = s.seat_dir(i) / f"prompt_{n:03d}_{int(time.time() * 1000) % 100000}.md"; pfile.write_text(self._prompt(i, seat, instruction), encoding="utf-8")
        cmd = self._command(i, seat, pfile)
        with self.lock: self.terms[i]["state"] = "speaking"; self.speaking.add(i); self._set_current()
        self._term(i, "=" * 60 + f"\n{who}: turn {n}\n" + "=" * 60)
        speech = None; stdout_lines: list[str] = []; err_tail: list[str] = []
        for attempt in (1, 2):
            speech, stdout_lines, err_tail = self._launch(i, seat, prov, cmd)
            if speech or self.stop_flag.is_set() or attempt == 2: break
            if looks_like_auth_error("\n".join(err_tail + stdout_lines[-20:])):
                # another CLI may have just rotated the login; the refreshed credentials are on disk a moment later
                self._term(i, "[login problem; waiting 5 s and trying once more]"); time.sleep(5); continue
            break
        with self.lock: self.terms[i]["state"] = "waiting"; self.speaking.discard(i); self._set_current()
        self._term(i, "\ndone. waiting for the next turn.\n")
        if speech: return speech
        with self.lock: tail = [ln for ln in list(self.terms[i]["lines"])[-14:] if ln.strip() and not ln.startswith("=") and ": turn " not in ln and "waiting for the next turn" not in ln]
        detail = "\n".join(tail[-6:]) or "no output at all; the command may not have started"
        hint = auth_hint(seat["provider"], "\n".join(err_tail + stdout_lines[-20:]))
        if hint: detail += "\n" + hint
        if s.cli_sessions.pop(str(i), None): detail += "\n(dropped this agent's resumable CLI session; it will start fresh next turn)"
        return f"[{who} returned no answer. Last output from its terminal:]\n{detail}"


    def _launch(self, i: int, seat: dict, prov: dict, cmd: str) -> tuple[str | None, list[str], list[str]]:
        """Run the CLI once and collect its speech. Gated providers (Claude Code) start one at a time: the gate is held
        from launch until the CLI has authenticated and written its first line, so several seats never refresh the same
        login token at the same moment, which logs all but one of them out."""
        s = self.s; stdout_lines: list[str] = []; speech: str | None = None; new_sid: str | None = None; err_tail: list[str] = []; rc: int | None = None
        gate = _GATES[seat["provider"]] if prov.get("gate") else None
        held = bool(gate and gate.acquire(timeout=90))
        def let_go() -> None:
            nonlocal held
            if held: held = False; gate.release()   # type: ignore[union-attr]
        try:
            proc = subprocess.Popen(cmd, cwd=s.workdir, shell=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    start_new_session=(os.name != "nt"), env=child_env(seat["provider"]))
            self.procs[i] = proc
            def pump_err(p: subprocess.Popen) -> None:
                for raw in p.stderr:  # type: ignore[union-attr]
                    ln = decode_out(raw).rstrip("\r\n"); err_tail.append(ln); del err_tail[:-8]; self._term(i, ln)
            threading.Thread(target=pump_err, args=(proc,), daemon=True).start()
            for raw in proc.stdout:  # type: ignore[union-attr]
                ln = decode_out(raw).rstrip("\r\n") + "\n"
                if held and ln.strip(): let_go()
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
        finally: let_go()
        with self.lock:
            if new_sid: s.cli_sessions[str(i)] = new_sid
        if speech is None and prov["speech"] != "claude_stream": speech = "".join(stdout_lines).strip() or None
        if speech is None and prov["speech"] == "claude_stream":
            self._term(i, f"[ended without a final answer: exit code {rc}, {len(stdout_lines)} events]" + ("\n" + "\n".join(err_tail) if err_tail else ""))
        return speech, stdout_lines, err_tail


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
            if text.startswith("[") and seat["provider"] in CODEX_PROVIDERS and codex_signed_out(text):
                passed_on[i] = len(s.transcript); self.codex_recover(); return
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
            if any(seats[j]["provider"] in CODEX_PROVIDERS for j, _ in launch): self.prime_codex()
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
        missing = [f"{s.label(x)}: '{exe_of(x['provider'])}' was not found. Open Settings > CLIs to connect it." for x in seats if cli_path(x["provider"]) is None]
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
            if seat["provider"] in CODEX_PROVIDERS: self.prime_codex()
            text = self._speak(i, seat, instr)
            if seat["provider"] in CODEX_PROVIDERS and text.startswith("[") and codex_signed_out(text):
                self.codex_recover()
                if not self.notice and not self.stop_flag.is_set():
                    text = self._speak(i, seat, instr)      # the recovery worked: give this seat its turn back
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
        self.phone_url = ""; self.away_url = ""; self.last_tg = ""; self.start_error = ""
        refresh_versions(); check_all_conns(); tg_fill_bot()

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
                    "current": r.current, "notice": r.notice, "turn": s.turn, "repo": s.repo, "room": s.room, "workdir": s.workdir, "repo_ok": Path(s.workdir).is_dir(),
                    "rounds_done": s.rounds_done, "sessions": list_sessions(), "live": live,
                    "terms": tstates,
                    "phone_url": self.phone_url, "away_url": self.away_url, "last_tg": self.last_tg,
                    **tg_state(),
                    "templates": list(TEMPLATES.keys()), "user_templates": list(user_templates().keys()), "sessions_dir": str(SESSIONS), "build": BUILD,
                    "start_error": self.start_error, "providers": connections()}

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
        save_user_template(name, {"seats": [dict(x) for x in s.seats], "framing": s.framing, "referee": s.referee, "mode": s.mode, "rounds": s.rounds,
                                  "readonly": s.readonly, "topic": s.topic, "extra": s.extra, "repo": s.repo, "room": s.room,
                                  "max_messages": s.max_messages, "max_minutes": s.max_minutes})

    def apply_template(self, name: str) -> None:
        r = self.run
        with r.lock:
            if r.busy() or r.s.transcript: return
            ut = user_templates()
            if name in ut:   # a saved layout: restore everything
                d = ut[name]; s = r.s
                s.seats = color_seats([dict(x) for x in d.get("seats", [])]); s.skipped = [False] * len(s.seats)
                for k in ("framing", "referee", "mode", "rounds", "readonly", "topic", "extra", "repo", "room", "max_messages", "max_minutes"):
                    if k in d: setattr(s, k, d[k])
                if s.room: s.readonly = True
                if s.framing == "game" and not s.referee: game_shape(s)
                r._reset_terms(); s.save(); return
            if name not in TEMPLATES: return
            seats = [dict(x) for x in TEMPLATES[name]]
            if name.startswith("Arena"):
                for x in seats: x["model"] = ARENA_MODEL.get(x["provider"], x["model"])
                r.s.framing = "game"; r.s.mode = "turns"; r.s.rounds = 6; r.s.topic = ARENA_TOPIC; r.s.referee = "World"
            else:
                r.s.framing = "council"; r.s.referee = ""
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
            if "room" in d and not s.transcript and not r.busy() and bool(d["room"]) != s.room:
                s.room = bool(d["room"])
                if s.topic.strip() in DEFAULT_TOPICS: s.topic = DEFAULT_TOPIC if s.room else FOLDER_TOPIC
            if "readonly" in d and not s.room: s.readonly = bool(d["readonly"])
            if s.room: s.readonly = True
            if d.get("mode") in ("turns", "open"): s.mode = d["mode"]
            if d.get("framing") in ("council", "game") and d["framing"] != s.framing:
                s.framing = d["framing"]
                if not s.transcript and not r.busy():   # a draft: reshape the seats so the new framing actually works
                    (game_shape if s.framing == "game" else council_shape)(s); r._reset_terms()
            if "referee" in d: s.referee = (d["referee"] or "").strip()
            if s.referee and s.referee.lower() not in {s.label(x).lower() for x in s.seats}: s.referee = ""
            if "max_messages" in d: s.max_messages = max(0, int(d["max_messages"] or 0))
            if "max_minutes" in d: s.max_minutes = max(0, int(d["max_minutes"] or 0))
            if d.get("repo"): s.repo = d["repo"]
            self.start_error = ""
            if not s.title: s.title = " ".join(s.topic.split()[:8])
            if s.framing == "game" and not s.referee and any(s.label(x).lower() == "world" for x in s.seats): s.referee = next(s.label(x) for x in s.seats if s.label(x).lower() == "world")
            s.save()


    def start(self) -> None:
        """Refuse to start when a seat's CLI is not connected: the conversation would only fill with failures."""
        bad = sorted({x["provider"] for x in self.s.seats if conn_state(x["provider"]) in ("none", "off")})
        if bad:
            check_all_conns(force=True)
            names = bad[0] if len(bad) == 1 else ", ".join(bad[:-1]) + " and " + bad[-1]
            self.start_error = f"{names} {'is' if len(bad) == 1 else 'are'} not connected."
            return
        self.start_error = ""; self.run.start()
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
body{margin:0;background:var(--bg);color:var(--text);font:var(--fs)/1.5 Inter,"Segoe UI",system-ui,-apple-system,sans-serif;display:grid;grid-template-columns:var(--rail) 1fr auto;grid-template-rows:52px auto 1fr;overflow:hidden}
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
#rail{grid-column:1;grid-row:3;background:var(--s1);border-right:1px solid var(--line);overflow:hidden;display:flex;flex-direction:column}
#rail .top{padding:12px 12px 10px}#rail .top button{width:100%;overflow:hidden;text-overflow:ellipsis}.railhdr{font-size:13px;font-weight:600;margin:2px 2px 10px;display:flex;justify-content:space-between;align-items:baseline}
.hsec{padding:10px 14px 4px;font-size:11px;font-weight:600;letter-spacing:.6px;text-transform:uppercase;color:var(--faint)}
#hlist{overflow:auto;flex:1}
.hitem{padding:10px 14px;cursor:pointer;display:flex;flex-direction:column;gap:2px;border-left:2px solid transparent}.hitem:hover{background:var(--s2)}.hitem.on{background:var(--s2);border-left-color:var(--accent)}
.hitem .t{font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:flex;justify-content:space-between;gap:8px}.hitem .m{font-size:11.5px;color:var(--faint)}.livedot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--accent);margin-right:6px;box-shadow:0 0 0 3px rgba(34,211,238,.18)}
.hitem .del{color:var(--faint);padding:0 4px;border:0;line-height:1;opacity:0;height:auto}.hitem:hover .del,.hitem.on .del,.hitem:focus-within .del{opacity:1}.hitem .del:hover{color:var(--danger);background:transparent}
#center{grid-column:2;grid-row:3;min-width:0;min-height:0;display:flex;flex-direction:column;overflow:hidden}
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
.field{margin-bottom:18px}.field label{display:block;font-size:12.5px;color:var(--muted);margin-bottom:6px}
.field textarea{min-height:130px}.two{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.seat{display:grid;grid-template-columns:1.2fr 1fr 1fr;gap:8px;padding:12px;border:1px solid var(--line);border-radius:var(--r-card);background:var(--s1);margin-bottom:8px}
.seat .hdr{grid-column:1/4;display:flex;justify-content:space-between;font-size:12px;color:var(--muted)}.seat .hdr.off{color:var(--danger)}
.seat .full{grid-column:1/4}.seat select{width:100%}.seat .mwrap{min-width:0}
.check{display:flex;gap:10px;align-items:flex-start;cursor:pointer}.check input{width:18px;height:18px;margin-top:2px}
.note{font-size:12px;color:var(--muted);line-height:1.45}.bad{color:var(--danger);font-size:12px;margin-top:6px}
#tgwiz code{font:12px ui-monospace,Consolas,monospace;background:var(--bg);border:1px solid var(--line);border-radius:4px;padding:1px 5px}#tgwiz ol{padding-left:18px;margin:6px 0 10px}#tgwiz li{margin:4px 0}#tgwiz a{color:var(--accent)}#tgwiz .sg.done h2::after{content:" \2713";color:#34D399}
.sheetwrap .panel{width:min(520px,100%);background:var(--s1);border-left:1px solid var(--line);overflow:auto;padding:22px 24px}
.sheetwrap .panel h1{font-size:16px;margin:0 0 12px;display:flex;justify-content:space-between;align-items:center}
/* terminals drawer */
#terms{grid-column:3;grid-row:3;width:var(--terms);border-left:1px solid var(--line);position:relative;background:var(--bg);display:flex;flex-direction:column;gap:8px;padding:8px;overflow:auto;min-height:0}
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
 header{flex:0 0 52px;padding:0 12px 0 10px;gap:6px}header .title{display:none}#railBtn,#gearBtn,#agentsBtn,#topicBtn,#endBtn{display:none}header .primary{padding:0 14px}.pill{max-width:46vw;font-size:12px;padding:4px 9px}
 #rail,#chat,#terms{display:none}
 #center{flex:1 1 auto;min-height:0;display:flex;flex-direction:column}
 #rail.on{display:flex;flex:1 1 auto;min-height:0;border:0;overflow:auto}
 #chat.on{display:block;flex:1 1 auto;min-height:0;overflow:auto;padding:12px 0 0}
 #terms.on{display:flex;flex:1 1 auto;min-height:0;width:100%;border:0;overflow:auto}
 #composer{flex:0 0 auto;padding:8px 0}#composer .inner{width:calc(100% - 20px);gap:6px}#composer select{max-width:104px;padding:0 6px;font-size:13px}
 .inner{width:calc(100% - 24px)}.term{min-height:60vh}.two{grid-template-columns:1fr}.seat{grid-template-columns:1fr 1fr}.seat .hdr,.seat .full{grid-column:1/3}
 #tabs{display:flex;flex:0 0 56px;background:var(--s1);border-top:1px solid var(--line)}
 #tabs button{flex:1;border:0;border-radius:0;height:56px;color:var(--muted);font-size:12.5px}#tabs button.on{color:var(--accent)}
 .sheetwrap .panel{width:100%}#picker .box{width:94vw}
}
/* connections */
.crow{border:1px solid var(--line);border-radius:var(--r-card);padding:12px 14px;margin-top:8px;background:var(--s1)}
.crow .top{display:flex;align-items:center;gap:10px;min-width:0}
.crow .nm{font-weight:600;white-space:nowrap}
.crow .ver{color:var(--faint);font-size:12px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.crow .stat{margin-left:auto;display:inline-flex;align-items:center;gap:7px;font-size:12.5px;white-space:nowrap;font-weight:600}
.dotc{width:9px;height:9px;border-radius:50%;background:var(--faint);flex-shrink:0}
.st-on .dotc{background:#34D399}.st-on .stat{color:#34D399}
.st-checking .dotc,.st-ask .dotc{background:#F5C26B}.st-checking .stat,.st-ask .stat{color:#F5C26B}
.st-off .dotc,.st-none .dotc,.st-unknown .dotc{background:var(--danger)}.st-off .stat,.st-none .stat,.st-unknown .stat{color:var(--danger)}
.crow .det{color:var(--muted);font-size:12.5px;margin-top:6px}
.crow .acts{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px;align-items:center}
.crow pre{margin:10px 0 0;padding:8px 10px;background:#050506;border:1px solid var(--line);border-radius:8px;max-height:190px;overflow:auto;font:11.5px/1.45 ui-monospace,Consolas,monospace;color:var(--mono);white-space:pre-wrap}
.crow code{font:12px ui-monospace,Consolas,monospace;background:var(--bg);border:1px solid var(--line);border-radius:4px;padding:1px 5px;word-break:break-all}
.keybox,.pathbox{display:none;gap:8px;margin-top:8px}.keybox.open,.pathbox.open{display:flex}
.btn.sm{height:32px;padding:0 10px;font-size:12.5px}
#blocker{grid-column:1/4;grid-row:2;display:none;align-items:center;gap:10px;padding:9px 14px;background:#2A1215;border-bottom:1px solid var(--danger);color:#F3B0B3;font-size:13px}
#blocker.on{display:flex}#blocker .lk{color:var(--accent);background:transparent;border:0;padding:0;height:auto;text-decoration:underline;cursor:pointer;font:inherit}
#blocker .x{margin-left:auto;color:#F3B0B3;height:auto;padding:0 4px;border:0;background:transparent;cursor:pointer}
.wel{max-width:540px;margin:0 auto;text-align:left;color:var(--text)}
.wel h1{font-size:19px;font-weight:600;margin:0 0 6px}.wel p{color:var(--muted);margin:0 0 16px}
.wel .row{margin-bottom:12px}
.sg .lead{color:var(--muted);font-size:12.5px;margin:0 0 10px}
.danger.solid{background:var(--danger);border-color:var(--danger);color:#fff;font-weight:600}
.danger.solid:hover{background:#F0575C}
</style></head><body>
<header>
 <button id="railBtn" class="icon" title="Conversations" aria-label="Show or hide conversations">&#9776;</button>
 <span class="brand"><svg class="mark" viewBox="0 0 32 32" width="22" height="22" aria-hidden="true"><g fill="currentColor"><circle cx="16" cy="16" r="3"/><circle cx="16" cy="4" r="2"/><circle cx="24.5" cy="7.5" r="2"/><circle cx="28" cy="16" r="2"/><circle cx="24.5" cy="24.5" r="2"/><circle cx="16" cy="28" r="2"/><circle cx="7.5" cy="24.5" r="2"/><circle cx="4" cy="16" r="2"/><circle cx="7.5" cy="7.5" r="2"/></g></svg><span>Agora</span></span><span class="title" id="hdrTitle"></span>
 <span class="pill" id="status"><span class="dot"></span><span id="statusText">Not started</span></span>
 <button id="primary" class="primary">Start</button>
 <button id="agentsBtn" class="btn">Agents</button>
 <button id="topicBtn" class="btn">Topic</button>
 <button id="endBtn" class="btn" title="Stop after one round of closing statements">End</button>
 <button class="icon" id="gearBtn" title="Settings" aria-label="Settings"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg></button>
</header>
<div id="blocker"><span id="blockText"></span><button class="lk" id="blockOpen">Open Settings, Connections</button><button class="x" id="blockX" aria-label="Dismiss">&#10005;</button></div>
<aside id="rail"><div class="grip" id="railGrip" title="Drag to resize"></div><div class="top"><div class="railhdr">Conversations <span id="railCount" class="note"></span></div><button id="newSess" class="primary" style="width:100%">+ New conversation</button></div><div id="hlist"></div></aside>

<main id="center">
 <section id="chat"><div class="inner" id="chatInner">
  <div class="empty" id="chatEmpty" style="text-align:center;padding-top:6vh">
   <svg class="ringart" viewBox="0 0 32 32" aria-hidden="true"><g fill="currentColor"><circle cx="16" cy="16" r="2.4"/><g opacity=".55"><circle cx="16" cy="4" r="1.6"/><circle cx="24.5" cy="7.5" r="1.6"/><circle cx="28" cy="16" r="1.6"/><circle cx="24.5" cy="24.5" r="1.6"/><circle cx="16" cy="28" r="1.6"/><circle cx="7.5" cy="24.5" r="1.6"/><circle cx="4" cy="16" r="1.6"/><circle cx="7.5" cy="7.5" r="1.6"/></g></g></svg>
   <div class="wel"><h1 id="welHd">The floor is empty</h1><p>Seat the agents, set the topic, press Start. Every turn appears here as it finishes, and you can message them at any time.</p>
    <div class="row"><button id="welAgents" class="btn">Agents</button><button id="welTopic" class="btn">Topic</button><button id="welStart" class="primary">Start conversation</button></div>
    <div class="note" id="welSum"></div></div></div>
  <div id="msgs"></div><div class="typing" id="typing" style="display:none"></div></div></section>
 <div id="composer"><div class="inner"><div id="ac" role="listbox"></div><div class="tawrap"><div id="hl" aria-hidden="true"></div><textarea id="sayText" rows="1" aria-label="Message the agents"></textarea></div><select id="sayTo" title="Who must answer first" aria-label="Who must answer first"><option value="">Everyone</option></select><button id="sayBtn" class="primary">Send</button></div></div>
</main>

<aside id="terms"><div class="grip" id="termGrip" title="Drag to resize"></div><div class="empty">Each agent's live terminal appears here after you press Start.</div></aside>

<nav id="tabs"><button data-t="chat" class="on">Chat</button><button data-t="terms">Terminals</button><button data-t="rail">Conversations</button><button data-t="more">More</button></nav>

<div id="agentsSheet" class="sheetwrap"><div class="panel"><h1>Agents <button class="icon shx" aria-label="Close">&#10005;</button></h1>
 <div class="note" id="seatNote">They speak in this order. Give each one a name, a CLI, a model, a colour, and a stance if you want one.</div>
 <div id="seats" style="margin-top:12px"></div><button id="add" class="btn">+ Add agent</button>
</div></div>

<div id="topicSheet" class="sheetwrap"><div class="panel"><h1>Topic <button class="icon shx" aria-label="Close">&#10005;</button></h1>
 <div class="field"><label>Title</label><input id="title" placeholder="Named from the topic if left blank"></div>
 <div class="field"><label>Topic</label><textarea id="topic"></textarea></div>
 <div class="field"><label>Extra instructions (optional)</label><textarea id="extra" style="min-height:70px" placeholder="Anything else they must do, avoid, or produce"></textarea></div>
 <div class="field"><label>What this is</label><div class="row"><button class="btn" id="frCouncil" style="flex:1">Council</button><button class="btn" id="frGame" style="flex:1">Game</button></div><div class="note" style="margin-top:6px" id="frNote"></div></div>
 <div class="field" id="refField"><label>Referee (the World)</label><select id="referee"><option value="">None</option></select><div class="note" style="margin-top:6px">Only the referee's messages wake everyone. Players wake the referee and whoever they name or whisper to. The referee speaks first.</div></div>
 <div class="field"><label>How they speak</label><div class="row" id="modeRow"><button class="btn" id="modeTurns" style="flex:1">Take turns</button><button class="btn" id="modeOpen" style="flex:1">Open floor</button></div><div class="note" id="modeNote" style="margin-top:6px"></div></div>
 <div class="two" id="limits"><div class="field"><label>Close the floor after this many messages</label><input id="maxMsgs" type="number" min="0" placeholder="no limit"></div><div class="field"><label>Or after this many minutes</label><input id="maxMins" type="number" min="0" placeholder="no limit"></div></div>
 <div class="field" id="roundsField"><label id="roundsLabel">Rounds</label><input id="rounds" type="number" min="1"><div class="note" id="roundsNote" style="margin-top:6px">Each agent speaks once per round, then gives a closing statement.</div></div>
 <div class="field"><label>Where the agents sit</label><div class="row"><button class="btn" id="whRoom" style="flex:1">Nowhere in particular</button><button class="btn" id="whFolder" style="flex:1">A folder I choose</button></div><div class="note" id="whNote" style="margin-top:6px"></div>
  <div class="row" id="repoRow" style="margin-top:8px"><input id="repo" placeholder="Folder the agents may read"><button id="browse" class="btn">Browse</button></div><div id="repoBad" class="bad"></div></div>
 <div class="field" id="roField"><label>Access</label><label class="check"><input id="ro" type="checkbox"><span>Read only<br><span class="note">Agents can read and search but not run or change anything.</span></span></label><div class="note" id="roNote" style="margin-top:6px"></div></div>
 <div class="field"><label>Templates</label><div class="row"><select id="tmpl" style="flex:1"><option value="">Start from a template...</option></select><button id="tmplApply" class="btn">Use</button><button id="tmplDel" class="btn" title="Delete this saved template" style="display:none">Delete</button></div>
  <div class="note" style="margin-top:8px"><button id="tmplSave" class="btn sm">Save this setup as a template</button> <span style="margin-left:6px">Keeps the agents, models, colours, stances, topic, mode, and every setting for reuse.</span></div></div>
</div></div>

<div id="settings" class="sheetwrap"><div class="panel"><h1>Settings <button class="icon shx" aria-label="Close settings">&#10005;</button></h1>
 <section class="sg" id="mobileConv" style="display:none"><h2>This conversation</h2>
  <div class="row"><button class="btn" id="mAgents">Agents</button><button class="btn" id="mTopic">Topic</button><button class="btn" id="mEnd">End with closing statements</button></div></section>
 <section class="sg"><h2>Connections</h2>
  <p class="lead">Every agent is a CLI you run yourself. Agora starts it, so it needs that CLI installed and signed in. Sign in opens a real terminal window with the CLI's own sign-in already running, and this row turns green by itself when it is done.</p>
  <div id="conns"></div>
  <div class="row" style="margin-top:10px"><button id="connCheck" class="btn sm">Check all again</button><span class="note" id="connState"></span></div></section>
 <section class="sg"><h2>Export</h2><div class="row" style="flex-wrap:wrap"><button class="btn sm" id="expClosing">Closing statements</button><button class="btn sm" id="expSpeeches">Conversation</button><button class="btn sm" id="expAll">Full record</button></div>
  <div class="note" style="margin-top:8px">Markdown files, saved by your browser.</div></section>
 <section class="sg"><h2>Phone</h2><div class="note">Open Agora on your phone. The Tailscale link works anywhere; the home link only on your wifi.</div>
  <div class="kv"><span>Anywhere</span><code id="awayUrl">not available</code><button class="btn sm cp" data-for="awayUrl">Copy</button></div>
  <div class="kv"><span>Home wifi</span><code id="homeUrl">not available</code><button class="btn sm cp" data-for="homeUrl">Copy</button></div>
  <div class="row" style="margin-top:8px"><button id="tg" class="btn">Connect Telegram</button><button id="tgChange" class="btn sm" style="display:none">Change</button><button id="tgOff" class="btn sm" style="display:none">Disconnect</button><span class="note" id="tgState"></span></div>
  <div class="note" id="tgInfo" style="margin-top:6px"></div></section>
 <section class="sg"><h2>Display</h2>
  <div class="kv"><span>Text size</span><span class="row"><button class="btn sm" id="fsDown">Smaller</button><button class="btn sm" id="fsUp">Larger</button></span></div>
  <div class="kv" id="termRow"><span>Terminals</span><button class="btn sm" id="termBtn">Show or hide</button></div>
  <div class="kv"><span>Panel sizes</span><button class="btn sm" id="layoutReset">Reset to defaults</button></div></section>
 <section class="sg"><h2>Agora</h2>
  <div class="kv"><span>Build</span><code id="buildTag"></code></div>
  <div class="kv"><span>Conversations folder</span><code id="sessDir"></code></div>
  <div class="row" style="margin-top:12px"><button id="stop" class="danger solid">Stop this conversation</button><span class="note">Ends it now. The transcript is kept and you can continue it later.</span></div>
  <div class="row" style="margin-top:10px"><button id="quit" class="danger solid">Quit Agora</button><span class="note">Kills every CLI Agora started and closes the server. Conversations are kept.</span></div></section>
</div></div>
<div id="tgwiz" class="sheetwrap"><div class="panel"><h1>Connect Telegram <button class="icon" id="tgwizClose" aria-label="Close">&#10005;</button></h1>
 <div class="note">Agora can message you when a conversation finishes and send you the phone link. It does this through a Telegram bot that you own. The bot token is kept in <code>agora_telegram.json</code> next to Agora and is only ever sent to Telegram. Three short steps, about three minutes.</div>
 <section class="sg"><h2 id="tgH1">Step 1 of 3: create a bot</h2>
  <ol class="note"><li>Open Telegram (phone or PC) and search for <b>@BotFather</b>, the one with the blue check.</li><li>Send it <code>/newbot</code>. It asks for a display name (anything, for example <i>Agora</i>) and then a username, which must end in <code>bot</code> (for example <i>agora_yourname_bot</i>).</li><li>BotFather replies with a token that looks like <code>123456789:AAH8f...</code>. Copy the whole thing and paste it here.</li></ol>
  <div class="row"><input id="tgTok" placeholder="Paste the bot token" style="flex:1" autocomplete="off"><button class="btn" id="tgCheck">Check</button></div><div class="note" id="tgS1" style="margin-top:6px"></div></section>
 <section class="sg" id="tgStep2"><h2 id="tgH2">Step 2 of 3: tell the bot who you are</h2>
  <ol class="note"><li>Open a chat with your new bot: <a id="tgBotLink" href="#" target="_blank" rel="noopener">its link appears here after step 1</a>, or search its username in Telegram.</li><li>Press <b>Start</b>, then send it any message, for example <code>hi</code>. A bot cannot message you until you do this.</li><li>Come back here and press Find my chat.</li></ol>
  <div class="row"><button class="btn" id="tgFind">Find my chat</button><span class="note" id="tgS2"></span></div><div id="tgPick" class="row" style="margin-top:6px;flex-wrap:wrap"></div>
  <div class="note" style="margin-top:10px">If Find my chat cannot see your message (another program may already be reading this bot), message <b>@userinfobot</b> in Telegram. It replies with your id. Paste it here:</div>
  <div class="row" style="margin-top:6px"><input id="tgChat" placeholder="Your chat id, digits only" style="flex:1" inputmode="numeric"></div></section>
 <section class="sg" id="tgStep3"><h2 id="tgH3">Step 3 of 3: test and save</h2>
  <label class="check"><input id="tgNotify" type="checkbox" checked><span>Message me when a conversation finishes</span></label>
  <div class="row" style="margin-top:10px"><button class="btn" id="tgTest">Send a test message</button><button class="primary" id="tgSave">Save</button><span class="note" id="tgS3"></span></div></section>
</div></div>
<div id="picker"><div class="box"><b>Choose a folder</b><div class="row"><input id="pkPath"><button id="pkUp" class="btn">Up</button></div><ul id="pkList"></ul><div class="row" style="justify-content:flex-end"><button id="pkCancel" class="btn">Cancel</button><button id="pkUse" class="primary">Use this folder</button></div></div></div>

<script>
let S=null,editing=false,providers={},termKey='',seatsLocked=null,connSig='',blockOff='',started_=false;
function syncComposer(){const onChat=!mobile()||$('chat').classList.contains('on');$('composer').style.display=(started_&&onChat)?'':'none'}
let T=[],lastTurn=-1,Tid=null;
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
const lbl=x=>x.name||(x.provider+' ('+x.model+')');
const mobile=()=>window.matchMedia('(max-width:820px)').matches;
async function api(p,b){const r=await fetch(p,{method:b?'POST':'GET',headers:{'Content-Type':'application/json'},body:b?JSON.stringify(b):null});return r.json()}
function mergeTranscript(s){if(s.id!==Tid||s.transcript_total<T.length){T=s.transcript?s.transcript.slice():[];Tid=s.id}else if(s.transcript)for(const e of s.transcript)if(e.turn>lastTurn)T.push(e);lastTurn=T.length?T[T.length-1].turn:-1;s.transcript=T}
function seatColor(name){const x=(S&&S.seats||[]).find(y=>lbl(y)===name);return x?x.color:''}
function rich(text){let h=esc(text);const hold=[];const keep=x=>{hold.push(x);return `\u0000${hold.length-1}\u0000`};
 h=h.replace(/\[([^\]\n]{1,80}?:\d+(?:-\d+)?)\]\(([^)\n]+)\)/g,(m,a,b)=>keep(`<span class="cite" title="${esc(b)}">${a}</span>`));
 h=h.replace(/(?<![\w"/])((?:[\w.-]+\/)*[\w.-]+\.(?:py|ts|tsx|js|jsx|md|json|ps1|cs|cpp|h))(:\d+(?:-\d+)?(?:,\s?\d+(?:-\d+)?)*)?(?![\w/])/g,(m,f,ln)=>ln?keep(`<span class="cite">${f}${ln}</span>`):m);
 h=h.replace(/\bOBSERVED\b:?/g,'<span class="lbl obs">observed</span>').replace(/\bDOCUMENTED\b:?/g,'<span class="lbl doc">documented</span>').replace(/\bGUESS\b:?/g,'<span class="lbl gs">guess</span>');
 h=h.replace(/@([A-Za-z][\w-]*)/g,(m,n)=>{const c=seatColor(n);return `<span class="men" style="${c?'--mc:'+esc(c):''}">@${n}</span>`});
 h=h.replace(/\u0000(\d+)\u0000/g,(m,i)=>hold[+i]);
 return h.split(/\n{2,}/).map(pg=>`<p>${pg.replace(/\n/g,'<br>')}</p>`).join('')}
const STATUS={idle:'Not started',running:'Running',paused:'Paused',voting:'Closing statements',done:'Finished',stopped:'Stopped'};

/* ---------------------------------------------------------------- sheets: one at a time, X or Escape closes */
const SHEETS={agentsSheet:'agents',topicSheet:'topic',settings:'settings',tgwiz:'telegram'};
function openSheet(id,anchor){Object.keys(SHEETS).forEach(x=>$(x).classList.toggle('open',x===id));
 if(id==='settings'){renderTg(S||{});$('mobileConv').style.display=mobile()?'':'none'}
 try{history.replaceState(null,'','#'+SHEETS[id])}catch(e){}
 const panel=$(id).querySelector('.panel');if(panel)panel.scrollTop=0;
 if(anchor&&panel){const el=$(anchor);if(el)setTimeout(()=>{panel.scrollTop=Math.max(0,el.closest('.sg').offsetTop-panel.offsetTop-8)},40)}}
function closeSheets(){const was=Object.keys(SHEETS).some(x=>$(x).classList.contains('open'));
 Object.keys(SHEETS).forEach(x=>$(x).classList.remove('open'));
 if(was){try{history.replaceState(null,'',location.pathname)}catch(e){}}}
document.querySelectorAll('.shx').forEach(b=>b.onclick=closeSheets);
document.querySelectorAll('.sheetwrap').forEach(w=>w.onclick=e=>{if(e.target===w)closeSheets()});
document.addEventListener('keydown',e=>{if(e.key==='Escape'){if($('picker').style.display==='flex'){$('picker').style.display='none';return}closeSheets()}});
function openFromHash(){const h=(location.hash||'').replace('#','');const id=Object.keys(SHEETS).find(k=>SHEETS[k]===h);
 if(id)openSheet(id); else if(h==='connections')openSheet('settings','conns')}

/* ---------------------------------------------------------------- connections */
function connBtn(t,cls){return `<button class="btn sm ${cls||''}">${t}</button>`}
function connRow(k,p){
 const st={on:'Connected',checking:'Checking',ask:'Sign-in unknown',off:'Not signed in',none:'Not installed',unknown:'Unclear'}[p.state]||p.state;
 const ver=p.version?esc(p.version):(p.isolated?'through npx':'');
 let acts='';
 if(p.state==='none'){
  acts+=p.can_install?`<button class="btn sm" data-a="install">Install</button>`:'';
  if(!p.npm&&p.can_install&&p.pkg)acts=`<a class="btn sm" href="${esc(p.node_url)}" target="_blank" rel="noopener">Get Node.js first</a>`;
  acts+=`<button class="btn sm" data-a="pathbox">Already installed?</button>`;
 }else if(p.state!=='on'){
  acts+=`<button class="btn sm" data-a="login">Sign in</button><button class="btn sm" data-a="check">Check</button>`;
  if(p.key_env)acts+=`<button class="btn sm" data-a="keybox">API key</button>`;
 }else if(p.has_key){
  acts+=`<button class="btn sm" data-a="clearkey">Forget the API key</button>`;
 }
 const wait=p.waiting?`<div class="det">Waiting for the sign-in to finish in the terminal window. Agora asks ${esc(k)} every fifteen seconds and turns this row green by itself.</div>`:'';
 const job=p.install?`<pre>${esc((p.install.lines||[]).join('\n'))||'Starting...'}</pre><div class="det">${p.install.done?esc(p.install.note||''):'Installing, this can take a couple of minutes.'}</div>`:'';
 const warns=(p.warnings||[]).map(([lv,t])=>`<div class="${lv==='warn'?'bad':'det'}">${esc(t)}</div>`).join('');
 const inst=p.state==='none'?`<div class="det">${esc(p.install_note)}</div>`:'';
 return `<div class="crow st-${p.state}" data-k="${esc(k)}">
  <div class="top"><span class="dotc"></span><span class="nm">${esc(k)}</span><span class="ver">${ver}</span><span class="stat">${esc(st)}</span></div>
  ${p.detail?`<div class="det">${esc(p.detail)}</div>`:''}${inst}${warns}${wait}
  <div class="acts">${acts}</div>
  <div class="keybox"><input class="kin" type="password" placeholder="${esc(p.key_env||'API key')}" autocomplete="off"><button class="btn sm" data-a="savekey">Save</button></div>
  <div class="pathbox"><input class="pin" placeholder="Full path to ${esc(p.exe_default)}" value="${esc(p.override||'')}"><button class="btn sm" data-a="savepath">Save</button></div>
  ${job}</div>`}
function renderConns(s){const P=s.providers||{};
 const sig=JSON.stringify(Object.entries(P).map(([k,p])=>[k,p.state,p.detail,p.version,p.waiting,p.has_key,p.override,p.install&&p.install.lines.length,p.install&&p.install.done]));
 const box=$('conns');
 if(sig!==connSig){
  const opened=[...box.querySelectorAll('.keybox.open,.pathbox.open')].map(e=>e.parentElement.dataset.k+':'+e.className.split(' ')[0]);
  const focus=document.activeElement&&box.contains(document.activeElement)?document.activeElement:null;
  const val=focus?focus.value:null,cls=focus?focus.className:'',row=focus?focus.closest('.crow').dataset.k:'';
  connSig=sig;box.innerHTML=Object.entries(P).map(([k,p])=>connRow(k,p)).join('');
  opened.forEach(o=>{const [k,c]=o.split(':');const r=box.querySelector(`.crow[data-k="${CSS.escape(k)}"] .${c}`);if(r)r.classList.add('open')});
  box.querySelectorAll('.crow').forEach(r=>{const k=r.dataset.k;
   r.querySelectorAll('[data-a]').forEach(b=>b.onclick=()=>connAct(k,b.dataset.a,r));
   r.querySelector('.kin').onkeydown=e=>{if(e.key==='Enter')connAct(k,'savekey',r)};
   r.querySelector('.pin').onkeydown=e=>{if(e.key==='Enter')connAct(k,'savepath',r)}});
  if(focus&&row){const back=box.querySelector(`.crow[data-k="${CSS.escape(row)}"] .${cls.split(' ')[0]}`);if(back){back.value=val;back.focus()}}
 }}
async function connAct(k,a,row){const note=t=>$('connState').textContent=t;
 if(a==='keybox'||a==='pathbox'){const el=row.querySelector('.'+a.replace('box','box'));el.classList.toggle('open');if(el.classList.contains('open'))el.querySelector('input').focus();return}
 if(a==='install'){note('Installing '+k+'...');const r=await api('/conn/install',{provider:k});if(!r.ok)note(r.error||'It would not start.');else{connSig='';note('')}return}
 if(a==='login'){note('Opening a terminal window...');const r=await api('/conn/login',{provider:k});
  note(r.ok?'A terminal window is open with: '+r.cmd+'. Finish the sign-in there; this row turns green by itself.':(r.error||'It would not open.'));connSig='';return}
 if(a==='check'){note('Checking '+k+'...');const paid=k==='Copilot CLI';await api('/conn/check',{provider:k,paid:paid});connSig='';note('');return}
 if(a==='savekey'){const v=row.querySelector('.kin').value;note('Saving...');const r=await api('/conn/key',{provider:k,key:v});
  row.querySelector('.kin').value='';note(r.ok?(v?'Key set for this session only.':'Key forgotten.'):(r.error||''));connSig='';return}
 if(a==='clearkey'){await api('/conn/key',{provider:k,key:''});note('Key forgotten.');connSig='';return}
 if(a==='savepath'){const v=row.querySelector('.pin').value;note('Looking...');await api('/conn/path',{provider:k,path:v});connSig='';note('');return}}
$('connCheck').onclick=async()=>{$('connState').textContent='Checking every CLI...';await api('/conn/refresh',{});connSig='';setTimeout(()=>$('connState').textContent='',2500)};

/* ---------------------------------------------------------------- seats */
function seatModel(d){const sel=d.querySelector('.msel');return sel.value==='__custom'?d.querySelector('.mcustom').value.trim():sel.value}
function seatsFromDom(){return [...document.querySelectorAll('.seat')].map(d=>({name:d.querySelector('.n').value,provider:d.querySelector('.p').value,model:seatModel(d),stance:d.querySelector('.s').value,color:d.querySelector('.c').value}))}
function seatHtml(s,i,locked){const p=providers[s.provider]||{models:[]};const ndis=locked?'disabled':'';
 const opts=Object.keys(providers).map(k=>`<option ${k===s.provider?'selected':''}>${esc(k)}</option>`).join('');
 return `<div class="seat" data-i="${i}"><div class="hdr"><span><span class="av" style="--c:${esc(s.color||'#22D3EE')};width:18px;height:18px;font-size:10px;margin-right:6px;vertical-align:middle">${esc((s.name||String(i+1))[0])}</span>Agent ${i+1}</span>${locked?'':'<button class="x" style="padding:0;color:var(--faint)">Remove</button>'}</div>
 <div class="row"><input type="color" class="c" value="${esc(s.color||'#22D3EE')}" title="Agent colour" aria-label="Agent colour"><input class="n" placeholder="Name" value="${esc(s.name)}" ${ndis}></div><select class="p" aria-label="CLI">${opts}</select>
 <div class="mwrap"><select class="msel" aria-label="Model">${(p.models||[]).map(m=>`<option ${m===s.model?'selected':''}>${esc(m)}</option>`).join('')}<option value="__custom" ${(p.models||[]).includes(s.model)?'':'selected'}>Custom...</option></select><input class="mcustom" placeholder="Type a model name" value="${(p.models||[]).includes(s.model)?'':esc(s.model)}" style="display:${(p.models||[]).includes(s.model)?'none':''};margin-top:6px"></div>
 <input class="s full" placeholder="Stance or role (optional)" value="${esc(s.stance)}"></div>`}
function renderSeats(seats,locked){$('seats').innerHTML=seats.map((s,i)=>seatHtml(s,i,locked)).join('');$('add').disabled=locked;
 $('seatNote').textContent=locked?'Names are fixed once a conversation has started. Change a model or a stance at any time and it applies on that agent\'s next turn.':'They speak in this order. Give each one a name, a CLI, a model, a colour, and a stance if you want one.';
 document.querySelectorAll('.seat').forEach(d=>{const i=+d.dataset.i;const x=d.querySelector('.x');if(x)x.onclick=async()=>{editing=true;const s=seatsFromDom();s.splice(i,1);renderSeats(s,false);await save();editing=false};
  d.querySelector('.p').onchange=async e=>{editing=true;const s=seatsFromDom();s[i].model=(providers[e.target.value]||{models:['']}).models[0]||'';renderSeats(s,locked);await save();editing=false};
  d.querySelector('.c').oninput=e=>{d.querySelector('.av').style.setProperty('--c',e.target.value)};d.querySelector('.c').onchange=save;
  d.querySelector('.msel').onchange=async e=>{const c=d.querySelector('.mcustom');c.style.display=e.target.value==='__custom'?'':'none';if(e.target.value==='__custom'){c.focus();return}editing=true;await save();editing=false};
  d.querySelector('.mcustom').onfocus=()=>editing=true;d.querySelector('.mcustom').onblur=()=>{editing=false;save()};
  d.querySelectorAll('input:not(.c):not(.mcustom)').forEach(x=>{x.onfocus=()=>editing=true;x.onblur=()=>{editing=false;save()}})})}

/* ---------------------------------------------------------------- terminals, conversations */
function renderTerms(s){const started=s.transcript.length>0||['running','voting','paused'].includes(s.status);
 if(!started||!s.terms.length){if(termKey!==''){termKey='';$('terms').innerHTML='<div class="grip" id="termGrip"></div><div class="empty">Each agent\'s live terminal appears here after you press Start.</div>';bindGrips()}return}
 const key=s.id+':'+s.terms.length;
 if(termKey!==key){termKey=key;$('terms').innerHTML='<div class="grip" id="termGrip"></div>'+s.terms.map((t,i)=>`<div class="term" id="t${i}"><div class="bar"><span class="nm"></span><span class="st"></span></div><pre></pre></div>`).join('');bindGrips()}
 s.terms.forEach((t,i)=>{const el=$('t'+i);if(!el)return;const seat=s.seats[i]||{};el.style.setProperty('--c',seat.color||'#22D3EE');
  el.querySelector('.nm').innerHTML=`<span class="av ${t.state==='speaking'?'on':''}">${esc((lbl(seat)||'?')[0])}</span> <b>${esc(lbl(seat))}</b><span>${esc((seat.provider||'')+' · '+(seat.model||''))}</span>`;
  el.classList.toggle('speaking',t.state==='speaking');el.querySelector('.st').textContent=t.state==='speaking'?'Speaking':'';
  const pre=el.querySelector('pre');const atBottom=pre.scrollHeight-pre.scrollTop-pre.clientHeight<40;
  if(pre.dataset.count!=t.count&&t.lines.length){pre.textContent=t.lines.join('\n');pre.style.color='';pre.dataset.count=t.count;if(atBottom)pre.scrollTop=pre.scrollHeight}
  else if(!pre.textContent){pre.textContent='No live output yet. This pane fills when the agent next speaks.';pre.style.color='var(--faint)'}})}
function renderHist(s){const live=new Set(s.live||[]);$('railCount').textContent=s.sessions.length;
 const item=h=>`<div class="hitem ${h.id===s.id?'on':''}" data-id="${h.id}" tabindex="0" role="button"><div class="t"><span>${live.has(h.id)?'<span class="livedot"></span>':''}${esc(h.title)}</span><button class="del" data-id="${h.id}" aria-label="Delete conversation ${esc(h.title)}" title="Delete">&times;</button></div><div class="m">${esc(h.created.replace('T',' '))} · ${h.turns?h.turns+' turns':'draft'}</div></div>`;
 const L=s.sessions.filter(h=>live.has(h.id)),E=s.sessions.filter(h=>!live.has(h.id));
 $('hlist').innerHTML=(L.length?'<div class="hsec">Live now</div>'+L.map(item).join(''):'')+(E.length?'<div class="hsec">'+(L.length?'Earlier':'All')+'</div>'+E.map(item).join(''):'');
 document.querySelectorAll('.hitem').forEach(d=>{d.onkeydown=e=>{if(e.key==='Enter')d.click()};d.onclick=async e=>{if(e.target.classList.contains('del')){if(confirm('Delete this conversation and its transcript?'))render(await api('/session/delete',{id:e.target.dataset.id}));return}
  S=null;termKey='';render(await api('/session/open',{id:d.dataset.id}));if(mobile())showTab('chat')}})}

/* ---------------------------------------------------------------- the page */
function render(s){const first=!S||S.id!==s.id;if(first){T=[];lastTurn=-1;connSig=''}mergeTranscript(s);S=s;providers=s.providers;
 const busy=['running','voting'].includes(s.status);const paused=s.status==='paused';const started=s.transcript.length>0||busy||paused;
 $('hdrTitle').textContent=s.title||'';
 const who=s.current?s.current.split(', '):[];const whoTxt=who.length>2?`<b>${who.length} agents</b>`:`<b>${esc(s.current||'')}</b>`;
 $('statusText').innerHTML=s.current?(paused?`Paused after ${whoTxt} finish${who.length>1?'':'es'}`:`${whoTxt} ${who.length>1?'are':'is'} speaking`):STATUS[s.status]||s.status;
 $('status').classList.toggle('live',busy);
 const label=busy?'Pause':paused?'Resume':(s.status==='done'||s.status==='stopped')?'Continue':'Start';
 $('primary').textContent=label;$('primary').disabled=!s.repo_ok||s.seats.length<2;
 $('primary').title=label==='Continue'?`Continue for ${s.rounds} more rounds`:(label==='Pause'?'Finish the turns in flight, then hold':'');
 $('endBtn').style.display=busy?'':'none';
 $('welStart').textContent=label==='Start'?'Start conversation':label;$('welStart').disabled=$('primary').disabled;
 $('welHd').textContent=started?'This conversation':'The floor is empty';
 $('welSum').textContent=`${s.seats.length} agent${s.seats.length===1?'':'s'} · ${s.framing==='game'?'game':'council'} · ${s.mode==='open'?'open floor':'take turns'} · ${s.mode==='open'?'no round limit':s.rounds+' round'+(s.rounds===1?'':'s')} · ${s.room?'nowhere in particular':s.repo}`;
 const note=s.notice?s.notice:(s.start_error&&s.start_error!==blockOff?s.start_error:'');
 $('blocker').classList.toggle('on',!!note);if(note)$('blockText').textContent=note;
 const can=busy||paused;$('sayBtn').disabled=!can;started_=started||busy;syncComposer();
 $('mEnd').disabled=!busy;
 $('buildTag').textContent=s.build||'';$('sessDir').textContent=s.sessions_dir||'';
 $('awayUrl').textContent=s.away_url||'not available (install Tailscale on this PC and your phone)';$('homeUrl').textContent=s.phone_url||'not available';
 $('roNote').textContent=s.readonly?'':'Full access: no permission prompts, agents can change files. Use a folder with a clean git status.';
 $('repoBad').textContent=s.repo_ok?'':'That folder does not exist.';
 const room=!!s.room;$('whRoom').classList.toggle('on',room);$('whFolder').classList.toggle('on',!room);$('whRoom').disabled=started||busy;$('whFolder').disabled=started||busy;
 $('repoRow').style.display=room?'none':'';$('repoBad').style.display=room?'none':'';$('roField').style.display=room?'none':'';
 $('whNote').textContent=room?'An empty folder Agora keeps for itself. Nothing to read, nothing to change, always read only. Right for most councils and for every game.':'The agents may read and search this folder to check claims and cite files. Pick one with a clean git status.';
 const game=s.framing==='game';$('frCouncil').classList.toggle('on',!game);$('frGame').classList.toggle('on',game);$('frCouncil').disabled=busy;$('frGame').disabled=busy;
 $('frNote').textContent=game?(s.referee?'Characters with fixed stats. Words persuade, only the World changes numbers, every message ends with one ACTION line. Switching to Game seated the World as referee, gave every character a stat block, set the arena topic, turns, and six rounds. Edit any of it here.':'No referee: the game has nobody to resolve actions. Pick one below, or add an agent named World.'):'Agents debate the topic, cite the folder, and give closing statements. Switching back from Game removes the World and the characters Agora seated.';
 $('frNote').classList.toggle('bad',game&&!s.referee);
 $('refField').style.display=game?'':'none';
 const ropts='<option value="">None</option>'+s.seats.map(x=>`<option value="${esc(lbl(x))}" ${lbl(x)===s.referee?'selected':''}>${esc(lbl(x))}</option>`).join('');
 if($('referee').innerHTML!==ropts)$('referee').innerHTML=ropts;$('referee').value=s.referee||'';
 const open=s.mode==='open';$('modeTurns').classList.toggle('on',!open);$('modeOpen').classList.toggle('on',open);$('modeTurns').disabled=busy;$('modeOpen').disabled=busy;
 $('modeNote').textContent=(game?'Game: characters with fixed stats, the World referees. ':'')+(open?'No turns. Every message goes to everyone at once and each agent replies or passes. Use @Name to demand an answer. The floor closes when everyone passes, when a limit below is reached, or when you press End.':'Agents speak one after another in seat order.');
 $('roundsField').style.display=open?'none':'';$('limits').style.display=open?'':'none';
 $('roundsNote').textContent=started?`${s.rounds_done} round(s) done. Continue adds this many more.`:'Each agent speaks once per round, then gives a closing statement.';
 $('repo').disabled=started;$('browse').disabled=started;$('ro').disabled=busy||paused;
 const opts='<option value="">Everyone</option>'+s.seats.map(x=>`<option value="${esc(lbl(x))}">${esc(lbl(x))}</option>`).join('');if($('sayTo').innerHTML!==opts)$('sayTo').innerHTML=opts;
 const ut=s.user_templates||[];const topts='<option value="">Start from a template...</option>'+(ut.length?'<optgroup label="My templates">'+ut.map(t=>`<option>${esc(t)}</option>`).join('')+'</optgroup>':'')+'<optgroup label="Built in">'+(s.templates||[]).map(t=>`<option>${esc(t)}</option>`).join('')+'</optgroup>';
 if($('tmpl').innerHTML!==topts){const cur=$('tmpl').value;$('tmpl').innerHTML=topts;$('tmpl').value=cur}
 $('tmpl').disabled=started||busy;$('tmplApply').disabled=started||busy;$('tmplDel').style.display=ut.includes($('tmpl').value)?'':'none';
 if(first||!editing){$('title').value=s.title;$('repo').value=s.repo;$('topic').value=s.topic;$('extra').value=s.extra;$('rounds').value=s.rounds;$('maxMsgs').value=s.max_messages||'';$('maxMins').value=s.max_minutes||'';$('ro').checked=s.readonly;
  if(first||seatsLocked!==started||JSON.stringify(seatsFromDom())!==JSON.stringify(s.seats.map(x=>({name:x.name,provider:x.provider,model:x.model,stance:x.stance,color:x.color})))){renderSeats(s.seats,started);seatsLocked=started}}
 renderConns(s);if($('settings').classList.contains('open'))renderTg(s);
 renderHist(s);
 if(!mobile()){document.body.classList.toggle('autoterm',!(started||busy))}
 $('chatEmpty').style.display=s.transcript.length?'none':'block';
 const chatEl=$('chat');
 const msgHtml=e=>`<div class="msg ${e.kind}" style="${e.color?'--c:'+esc(e.color):''}"><div class="hd"><span class="av">${esc((e.speaker||'?')[0])}</span><span class="who">${esc(e.speaker)}</span>${e.kind==='resolution'?'<span class="tag">closing statement</span>':''}<span class="meta">turn ${e.turn} · ${e.time}</span></div><div class="body">${rich(e.text)}</div></div>`;
 const have=$('msgs').children.length;
 if(first||have>s.transcript.length){$('msgs').innerHTML=s.transcript.map(msgHtml).join('');chatEl.scrollTop=chatEl.scrollHeight}
 else if(have<s.transcript.length){const atB=chatEl.scrollHeight-chatEl.scrollTop-chatEl.clientHeight<120;$('msgs').insertAdjacentHTML('beforeend',s.transcript.slice(have).map(msgHtml).join(''));if(atB)chatEl.scrollTop=chatEl.scrollHeight}
 $('typing').style.display=s.current?'flex':'none';$('typing').textContent=s.current?(who.length>3?who.length+' agents':s.current)+(paused?' finishing, then the conversation pauses':(who.length>1?' are writing':' is writing')):'';
 renderTerms(s)}

/* ---------------------------------------------------------------- header and gear actions */
$('agentsBtn').onclick=()=>openSheet('agentsSheet');$('topicBtn').onclick=()=>openSheet('topicSheet');
$('welAgents').onclick=()=>openSheet('agentsSheet');$('welTopic').onclick=()=>openSheet('topicSheet');
$('gearBtn').onclick=()=>openSheet('settings');
$('mAgents').onclick=()=>openSheet('agentsSheet');$('mTopic').onclick=()=>openSheet('topicSheet');
$('blockOpen').onclick=()=>{$('blocker').classList.remove('on');openSheet('settings','conns')};
$('blockX').onclick=async()=>{$('blocker').classList.remove('on');blockOff=(S&&S.start_error)||'';await api('/conn/dismiss',{})};
async function primary(){const s=S||{};
 if(['running','voting'].includes(s.status))return render(await api('/pause',{}));
 await save();const r=await api('/start',{});render(r);
 if(r.start_error){blockOff='';$('blocker').classList.add('on');$('blockText').textContent=r.start_error}else closeSheets()}
$('primary').onclick=primary;$('welStart').onclick=primary;
$('endBtn').onclick=async()=>{if(confirm('End now? Agents still writing will finish, then everyone gives a closing statement.'))render(await api('/vote',{}))};
$('mEnd').onclick=()=>{closeSheets();$('endBtn').click()};
$('stop').onclick=async()=>{if(!confirm('Stop this conversation? The transcript is kept and you can continue it later.'))return;$('statusText').textContent='Stopping...';closeSheets();render(await api('/stop',{}))};
$('quit').onclick=async()=>{if(!confirm('Quit Agora? This kills every CLI it started and closes the server. Conversations are kept.'))return;try{await api('/shutdown',{})}catch(e){}document.body.innerHTML='<div style="padding:40px;color:#8B8B94">Agora is closed. You can close this tab.</div>'};
[['expClosing','closing'],['expSpeeches','speeches'],['expAll','all']].forEach(([id,w])=>$(id).onclick=()=>{if(S)window.location='/export?what='+w+'&id='+encodeURIComponent(S.id)});
$('railBtn').onclick=()=>document.body.classList.toggle('norail');
$('termBtn').onclick=()=>{document.body.classList.toggle('noterm')};
$('newSess').onclick=async()=>{S=null;termKey='';closeSheets();render(await api('/session/new',{}));openSheet('topicSheet')};
document.querySelectorAll('.cp').forEach(b=>b.onclick=async()=>{const t=$(b.dataset.for).textContent;if(!t.startsWith('http'))return;try{await navigator.clipboard.writeText(t);b.textContent='Copied';setTimeout(()=>b.textContent='Copy',1200)}catch(e){prompt('Copy this link',t)}});

/* ---------------------------------------------------------------- saving the setup */
let saveSeq=0;
async function save(){const my=++saveSeq;const r=await api('/config',{seats:seatsFromDom(),title:$('title').value,topic:$('topic').value,extra:$('extra').value,rounds:+$('rounds').value,repo:$('repo').value,readonly:$('ro').checked,max_messages:+($('maxMsgs').value||0),max_minutes:+($('maxMins').value||0)});
 if(my===saveSeq)return render(r)}
let saveTimer=null;['title','repo','topic','extra','rounds','maxMsgs','maxMins'].forEach(id=>{const el=$(id);el.onfocus=()=>editing=true;el.onblur=()=>{editing=false;save()};el.oninput=()=>{clearTimeout(saveTimer);saveTimer=setTimeout(save,800)}});
$('ro').onchange=save;
$('add').onclick=async()=>{editing=true;const s=seatsFromDom();const k=Object.keys(providers)[0];s.push({name:'',provider:k,model:(providers[k].models||[''])[0],stance:''});renderSeats(s,false);await save();editing=false};
$('whRoom').onclick=async()=>render(await api('/config',{room:true}));$('whFolder').onclick=async()=>render(await api('/config',{room:false}));
$('referee').onchange=async()=>render(await api('/config',{referee:$('referee').value}));
$('modeTurns').onclick=async()=>render(await api('/config',{mode:'turns'}));$('modeOpen').onclick=async()=>render(await api('/config',{mode:'open'}));
$('frCouncil').onclick=async()=>render(await api('/config',{framing:'council'}));$('frGame').onclick=async()=>render(await api('/config',{framing:'game'}));
$('tmplApply').onclick=async()=>{const n=$('tmpl').value;if(!n)return;S=null;render(await api('/template',{name:n}))};
$('tmpl').onchange=()=>{$('tmplDel').style.display=(S&&(S.user_templates||[]).includes($('tmpl').value))?'':'none'};
$('tmplSave').onclick=async()=>{await save();const n=prompt('Name this template:',S&&S.title?S.title:'');if(!n)return;render(await api('/template/save',{name:n}));$('tmpl').value=n;$('tmplDel').style.display=''};
$('tmplDel').onclick=async()=>{const n=$('tmpl').value;if(!n||!confirm('Delete template "'+n+'"?'))return;render(await api('/template/delete',{name:n}));$('tmpl').value=''};

/* ---------------------------------------------------------------- talking to the agents */
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
 if(firstMention){const opt=[...$('sayTo').options].find(o=>o.value===firstMention);if(opt)$('sayTo').value=firstMention}else if(!t.value.includes('@'))$('sayTo').value=''}
$('sayText').oninput=()=>{acUpdate();hlSync()};$('sayText').onclick=acUpdate;$('sayText').onscroll=()=>{$('hl').scrollTop=$('sayText').scrollTop};$('sayText').onblur=()=>setTimeout(acClose,150);
$('sayText').onkeydown=e=>{const open=$('ac').classList.contains('open');
 if(open&&(e.key==='ArrowDown'||e.key==='ArrowUp')){e.preventDefault();acIdx=(acIdx+(e.key==='ArrowDown'?1:acItems.length-1))%acItems.length;acRender();return}
 if(open&&(e.key==='Enter'||e.key==='Tab')){e.preventDefault();acPick(acIdx);return}
 if(open&&e.key==='Escape'){acClose();return}
 if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();$('sayBtn').click()}};

/* ---------------------------------------------------------------- telegram, unchanged apart from where it lives */
function renderTg(s){const ready=!!s.tg_ready;$('tg').textContent=ready?'Send the link to my Telegram':'Connect Telegram';
 $('tgChange').style.display=ready?'':'none';$('tgOff').style.display=ready?'':'none';
 $('tgInfo').textContent=ready?`Connected${s.tg_bot?' to @'+s.tg_bot:''}, chat ${s.tg_chat}. ${s.tg_notify?'Agora messages you when a conversation finishes.':'Finish notifications are off.'}`:'Not connected. Agora can message you when a conversation finishes and send you the phone link. Press Connect Telegram for a three-step setup.'}
let tgBot='';
function tgOpen(){const s=S||{};tgBot=s.tg_bot||'';$('tgTok').value='';$('tgTok').placeholder=s.tg_has_token?'A token is saved. Paste a new one only to replace it.':'Paste the bot token';
 $('tgS1').textContent=s.tg_has_token?('Using the saved token'+(s.tg_bot?' for @'+s.tg_bot:'')+'.'):'';$('tgBotLink').textContent=s.tg_bot?'t.me/'+s.tg_bot:'its link appears here after step 1';$('tgBotLink').href=s.tg_bot?'https://t.me/'+s.tg_bot:'#';
 $('tgChat').value=s.tg_chat||'';$('tgNotify').checked=s.tg_notify!==false;$('tgS2').textContent='';$('tgS3').textContent='';$('tgPick').innerHTML='';
 document.querySelectorAll('#tgwiz .sg').forEach(x=>x.classList.remove('done'));if(s.tg_has_token)$('tgH1').parentElement.classList.add('done');if(s.tg_chat)$('tgH2').parentElement.classList.add('done');
 openSheet('tgwiz')}
$('tg').onclick=async()=>{if(!(S&&S.tg_ready)){tgOpen();return}$('tgState').textContent='Sending...';const r=await api('/telegram',{});render(r);$('tgState').textContent=r.last_tg||'No response'};
$('tgChange').onclick=tgOpen;
$('tgOff').onclick=async()=>{if(!confirm('Disconnect Telegram? Agora forgets the bot token and chat id. The bot itself stays in Telegram; delete it with BotFather if you want.'))return;await api('/telegram/clear',{});$('tgState').textContent='Disconnected.';render(await api('/state?since=1000000000&terms=0'))};
$('tgCheck').onclick=async()=>{$('tgS1').textContent='Asking Telegram...';const r=await api('/telegram/check',{token:$('tgTok').value});
 if(r.ok){tgBot=r.bot;$('tgS1').textContent=`Found your bot: ${r.name} (@${r.bot}).`;$('tgBotLink').textContent='t.me/'+r.bot;$('tgBotLink').href='https://t.me/'+r.bot;$('tgH1').parentElement.classList.add('done')}else{$('tgS1').textContent=r.error;$('tgH1').parentElement.classList.remove('done')}};
$('tgFind').onclick=async()=>{$('tgS2').textContent='Looking...';$('tgPick').innerHTML='';const r=await api('/telegram/find',{token:$('tgTok').value});
 if(!r.ok){$('tgS2').textContent=r.error;return}
 if(!r.chats.length){$('tgS2').textContent='No message from you yet. Open the bot, press Start, send it "hi", wait a few seconds, then press Find my chat again.';return}
 if(r.chats.length===1){$('tgChat').value=r.chats[0].id;$('tgS2').textContent=`Found you: ${r.chats[0].name} (id ${r.chats[0].id}).`;$('tgH2').parentElement.classList.add('done');return}
 $('tgS2').textContent='Several chats have messaged this bot. Pick yours:';$('tgPick').innerHTML=r.chats.map(c=>`<button class="btn sm" data-id="${esc(c.id)}">${esc(c.name)} · ${esc(c.id)}</button>`).join('');
 document.querySelectorAll('#tgPick button').forEach(b=>b.onclick=()=>{$('tgChat').value=b.dataset.id;$('tgS2').textContent='Using chat '+b.dataset.id+'.';$('tgH2').parentElement.classList.add('done')})};
$('tgTest').onclick=async()=>{$('tgS3').textContent='Sending...';const r=await api('/telegram/test',{token:$('tgTok').value,chat_id:$('tgChat').value});$('tgS3').textContent=r.ok?'Sent. Check Telegram, then press Save.':r.error};
$('tgSave').onclick=async()=>{const r=await api('/telegram/save',{token:$('tgTok').value,chat_id:$('tgChat').value,notify:$('tgNotify').checked,bot:tgBot});
 if(!r.ok){$('tgS3').textContent=r.error;return}openSheet('settings');$('tgState').textContent='Connected.';render(await api('/state?since=1000000000&terms=0'))};

/* ---------------------------------------------------------------- folder picker */
let pk={path:''};
async function openPk(p){const d=await api('/ls?path='+encodeURIComponent(p||$('repo').value));pk=d;$('pkPath').value=d.path;
 const sep=d.path.includes('\\')?'\\':'/';const base=d.path.replace(/[\\/]$/,'');
 $('pkList').innerHTML=d.drives.map(x=>`<li data-p="${esc(x)}">${esc(x)}</li>`).join('')+d.dirs.map(x=>`<li data-p="${esc(base+sep+x)}">${esc(x)}</li>`).join('');
 document.querySelectorAll('#pkList li').forEach(li=>li.onclick=()=>openPk(li.dataset.p));$('picker').style.display='flex'}
$('browse').onclick=()=>openPk();$('pkUp').onclick=()=>pk.parent&&openPk(pk.parent);$('pkPath').onchange=e=>openPk(e.target.value);
$('pkCancel').onclick=()=>$('picker').style.display='none';$('pkUse').onclick=async()=>{$('repo').value=pk.path;$('picker').style.display='none';await save()};

/* ---------------------------------------------------------------- phone tabs, layout, polling */
function showTab(t){if(t==='more'){openSheet('settings');document.querySelectorAll('#tabs button').forEach(b=>b.classList.toggle('on',b.dataset.t===t));return}
 closeSheets();['chat','terms','rail'].forEach(id=>$(id).classList.toggle('on',id===t));
 document.querySelectorAll('#tabs button').forEach(b=>b.classList.toggle('on',b.dataset.t===t));
 if(mobile())$('center').style.display=t==='chat'?'flex':'none';
 syncComposer()}
document.querySelectorAll('#tabs button').forEach(b=>b.onclick=()=>showTab(b.dataset.t));if(mobile())showTab('chat');
const LAY=JSON.parse(localStorage.getItem('agora-layout')||'{}');
function applyLayout(){const r=document.documentElement.style;if(LAY.rail)r.setProperty('--rail',LAY.rail+'px');if(LAY.terms)r.setProperty('--terms',LAY.terms+'px');if(LAY.fs){r.setProperty('--fs',LAY.fs+'px');r.setProperty('--tfs',Math.max(10,LAY.fs-2)+'px')}}
function saveLayout(){localStorage.setItem('agora-layout',JSON.stringify(LAY))}
function bindGrips(){const g1=$('railGrip'),g2=$('termGrip');
 if(g1&&!g1.dataset.b){g1.dataset.b=1;g1.onpointerdown=e=>drag(e,g1,x=>{LAY.rail=Math.min(480,Math.max(180,x));applyLayout()})}
 if(g2&&!g2.dataset.b){g2.dataset.b=1;g2.onpointerdown=e=>drag(e,g2,x=>{LAY.terms=Math.min(window.innerWidth*0.7,Math.max(280,window.innerWidth-x));applyLayout()})}}
function drag(e,el,fn){e.preventDefault();el.classList.add('drag');el.setPointerCapture(e.pointerId);const mv=ev=>fn(ev.clientX);const up=()=>{el.classList.remove('drag');window.removeEventListener('pointermove',mv);window.removeEventListener('pointerup',up);saveLayout()};window.addEventListener('pointermove',mv);window.addEventListener('pointerup',up)}
$('fsUp').onclick=()=>{LAY.fs=Math.min(20,(LAY.fs||14)+1);applyLayout();saveLayout()};$('fsDown').onclick=()=>{LAY.fs=Math.max(11,(LAY.fs||14)-1);applyLayout();saveLayout()};
$('layoutReset').onclick=()=>{for(const k of Object.keys(LAY))delete LAY[k];localStorage.removeItem('agora-layout');const r=document.documentElement.style;['--rail','--terms','--fs','--tfs'].forEach(v=>r.removeProperty(v))};
applyLayout();bindGrips();openFromHash();
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
            direct = {"/conn/login": lambda: open_terminal(data.get("provider", "")),
                      "/conn/install": lambda: start_install(data.get("provider", "")),
                      "/conn/key": lambda: set_session_key(data.get("provider", ""), data.get("key", "")),
                      "/telegram/check": lambda: tg_check(data.get("token", "")),
                      "/telegram/find": lambda: tg_find(data.get("token", "")),
                      "/telegram/test": lambda: tg_test(data.get("token", ""), data.get("chat_id", "")),
                      "/telegram/save": lambda: tg_save(data.get("token", ""), data.get("chat_id", ""), bool(data.get("notify", True)), data.get("bot", "")),
                      "/telegram/clear": tg_clear}
            if self.path in direct: return self._send(json.dumps(direct[self.path]()).encode(), "application/json")
            {"/config": lambda: agora.configure(data), "/start": agora.start, "/pause": agora.pause,
             "/vote": agora.call_vote, "/stop": agora.stop, "/shutdown": agora.shutdown,
             "/say": lambda: agora.say(data.get("text", ""), data.get("target", "")),
             "/session/new": agora.new_session, "/session/open": lambda: agora.open_session(data.get("id", "")),
             "/session/delete": lambda: agora.delete_session(data.get("id", "")),
             "/template": lambda: agora.apply_template(data.get("name", "")),
             "/template/save": lambda: agora.save_template(data.get("name", "")),
             "/template/delete": lambda: delete_user_template(data.get("name", "")),
             "/telegram": agora.send_link,
             "/conn/refresh": lambda: check_all_conns(force=True),
             "/conn/check": lambda: check_conn(data.get("provider", ""), paid=bool(data.get("paid"))),
             "/conn/path": lambda: (set_cli_path(data.get("provider", ""), data.get("path", "")), check_conn(data.get("provider", ""))),
             "/conn/dismiss": lambda: setattr(agora, "start_error", "")}.get(self.path, lambda: None)()
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
