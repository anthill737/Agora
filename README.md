# Agora

Recorded conversations between CLI coding agents, sitting inside the same folder.

Agora seats several AI coding agents (Claude Code, Codex, OpenCode, Gemini CLI) at one table, gives them a topic, and lets them talk to each other. Every agent runs in its own real CLI inside the folder you choose, so it can read files, search, and check git history to back up what it says. A small local web dashboard shows each agent's terminal live, and the chat in the middle holds only the finished speeches, in order, like a group conversation.

Conversations are saved with per-agent memory, so you can reopen any past one and continue it with the same agents. Several conversations can run at the same time.

Agora can also run as a game instead of a council: characters with fixed stats, a World that referees, and one action per message.

## Quick start

Requirements:

- Python 3.10 or newer. Standard library only, nothing to `pip install`.
- At least one provider CLI installed, on `PATH`, and logged in:
  - [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`claude`)
  - [Codex](https://github.com/openai/codex) (`codex`, or `npx` for the "Codex (latest)" seat)
  - [OpenCode](https://opencode.ai) (`opencode`)
  - [Gemini CLI](https://github.com/google-gemini/gemini-cli) (`gemini`)

Run it:

```sh
python agora.py
```

It opens `http://127.0.0.1:8765/?token=...` in your browser. Then:

1. Pick the folder the agents will sit in.
2. Add seats, or start from a template. Each seat has a name, a provider, a model, an optional stance ("Skeptic", "Defender", a character sheet), and a color.
3. Write the topic (the charge) and any extra instructions.
4. Choose Council or Game, how they speak, and how many rounds or what limits.
5. Open the floor.

Flags:

| Flag | Meaning |
| --- | --- |
| `--port N` | Serve on a different port (default 8765). |
| `--local-only` | Bind to 127.0.0.1 only. By default Agora also listens on your LAN so you can follow along from a phone. |

## How a conversation works

**Two speaking modes.**

- *Take turns.* Agents speak one after another in seat order. Each round every agent speaks once, then everyone gives a ranked closing statement. You can add more rounds afterwards with Continue.
- *Open floor.* No turns. Every message goes to everyone at once and each agent either replies or answers `PASS`. Use `@Name` in a message to demand an answer from someone. The floor closes when everyone passes on the latest message, when a message or time limit you set is reached, or when you press End. Closing statements follow.

**You are the convener.** You can interject at any time from the dashboard, to everyone or to one agent. Agents are told to address what you said before anything else.

**Knowability rule.** In a council, agents must label claims about themselves as OBSERVED (visible in their context or environment right now), DOCUMENTED (public docs or source they can cite), or GUESS. They are told to read files, cite paths, and disagree openly.

**Read only by default.** With Read only ticked, seats can read, search, and inspect git history but cannot run or change anything (Claude gets a restricted tool allowlist, Codex runs with `--sandbox read-only`, Gemini runs without `--yolo`). Untick it for full access with permission prompts off. If you do, use a folder with a clean git status.

**Memory and resuming.** Each agent has its own memory file that only it reads. The prompt for a turn carries only what was said since that agent last spoke, and the agent is told where its full history lives. Claude seats also resume their CLI session between turns. You can swap a seat's model mid-conversation and the change applies on its next turn, with a note written into that agent's memory.

**Failing agents get benched.** On the open floor, a seat that returns no answer twice in a row is benched for the rest of the conversation, with a note in the chat. Fix its provider or model in Details and it rejoins when you press Continue.

## Council or game

**Council** is the default. Agents debate the topic, cite the folder, and give closing statements.

**Game** turns the seats into characters in a shared arena. Each character has fixed stats in its stance (strength, speed, health, gold, skills) and a personality. Characters may talk, plot, lie, bargain, and ally, but words never resolve anything: every message ends with exactly one `ACTION:` line, and only the seat named World, given the referee stance, resolves actions with dice, applies the results, and posts the STANDINGS table that is the single source of truth.

The built-in template **Arena (20 characters + World)** sets all of this up: twenty named characters with stat sheets, a World, game framing, six rounds, cheap models, and an arena topic. Change models or stats before you start.

## Templates

The template dropdown has two groups.

- **Built in**: council layouts with named stances, the Arena game, and blank layouts with names only.
- **My templates**: anything you saved with "Save current setup as a template". A saved template restores seats, models, colors, stances, topic, extra instructions, folder, Council or Game, speaking mode, rounds, read-only, and limits. Delete one with the Delete button next to the dropdown.

Saved templates live in `agora_templates.json` next to the script and are not committed.

## Exporting

The conversation menu (the three dots in the header) can download the current conversation as Markdown in three cuts: closing statements only, the conversation (speeches, closing statements, and your interjections), or the full record including Agora's system notes.

## Where things are saved

```
agora_sessions/
  <timestamp>/
    session.json      seats, topic, settings, full transcript
    transcript.md     the same conversation as readable Markdown
    agent1/
      memory.md       this agent's private memory of the conversation
      prompt_*.md     the exact prompt it was given on each turn
    agent2/
    ...
```

The Conversations sidebar lists every saved conversation, with the live ones at the top. Open one to reread it, continue it, or watch it while another runs.

## Phone and away-from-home access

Unless you pass `--local-only`, Agora prints a LAN URL you can open on a phone on the same wifi. If [Tailscale](https://tailscale.com) is installed, it also prints a Tailscale URL that works from anywhere.

Access is protected by a token stored in `agora_token.txt` next to the script. It is generated on first run. Delete the file to rotate it.

## Telegram notifications (optional)

Copy `agora_telegram.example.json` to `agora_telegram.json` and fill in a bot token and your chat id:

```json
{"token": "123456:ABC...", "chat_id": "123456789", "notify_on_finish": true}
```

Message `@userinfobot` on Telegram to get your chat id. Agora will send you a message when a conversation finishes, and the Settings panel can send the phone link to you.

## Codex notes

Codex refuses to run non-interactively in a folder it has not been told to trust. When a Codex seat is present, Agora adds the working folder to `~/.codex/config.toml` as trusted (a backup of the file is saved alongside) and records a note in the chat. Codex is also run with `--skip-git-repo-check` so folders that are not git repositories work.

## Configuration in the source

A few defaults live at the top of `agora.py`:

- `DEFAULT_REPO`: the folder new conversations start in. Change it to your own project, or just pick a folder in the dashboard.
- `PROVIDERS`: the CLI command templates and model lists for each provider. Add a provider or model here. Any seat can also use a custom model name typed in the UI.
- `TURN_TIMEOUT`: how long one agent may take per turn, in seconds.
- `FRAMING`, `OPENING`, `REPLY`, `OPEN_*`, `VOTE`: the council prompts.
- `GAME_FRAMING`, `WORLD_STANCE`, `GAME_OPENING`, `GAME_REPLY`, `GAME_VOTE`: the game prompts.
- `TEMPLATES`: the built-in seat layouts.

## Files not committed

`.gitignore` excludes the access token, the Telegram config, your saved templates, and all saved conversations and run output. Those stay on your machine.

## License

Copyright (c) 2026 Anthony Hill. All rights reserved. No license is granted; see [LICENSE](LICENSE).
