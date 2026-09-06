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
  - [GitHub Copilot CLI](https://docs.github.com/en/copilot/how-tos/copilot-cli) (`copilot`)
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

## Connecting your CLIs

Agora does not talk to model APIs. Each agent is a real CLI that Agora runs the way you would in a terminal, inside the folder you picked, so it uses whatever login and settings that CLI already has. To connect a CLI:

1. Install it (the install command for each one is shown under Settings > CLIs).
2. Log in to it once in a normal terminal (`claude` then `/login`, `codex login`, `opencode auth login`, `gemini`).
3. Open Agora. The top of the New conversation page shows which CLIs it found. Press **Connect or check CLIs** to see versions, install and login commands, and to check again after installing.

If a CLI is installed but Agora says it is not found, it is not on the `PATH` the Python process sees. Paste the full path to its executable (for example `C:\Users\you\AppData\Roaming\npm\claude.cmd`) into that CLI's path box under Settings > CLIs and press Save. The path is kept in `agora_clis.json` next to the script.

Press **Test** on a CLI's card to have Agora ask it one tiny read-only question exactly the way a seat would. A failure shows the CLI's last lines and, for login problems, what to do.

Agora never installs, updates, or logs in to a CLI for you.

### What Agora does so the CLIs behave

Each agent is started with the user's own environment, adjusted in a few ways that matter in practice:

- **Claude Code seats start one at a time.** Claude Code keeps one login token per machine and rotates it on refresh. When several copies start at the same moment and the token has expired, only one refresh succeeds and the others report an OAuth failure and ask for `/login`. Agora holds a gate from launch until each Claude seat has authenticated and produced its first line, then starts the next. A seat that still hits a login error waits five seconds and retries once, because the refreshed token is on disk by then.
- **No nested-session markers.** If Agora itself was started from inside a Claude Code session, the `CLAUDECODE` and `CLAUDE_CODE_ENTRYPOINT` variables it inherits would make every Claude seat hang at startup. Agora removes them for its agents.
- **The usual install folders are on `PATH`.** On macOS, a Python started from Finder or a bare shell does not see `~/.local/bin`, `/opt/homebrew/bin`, or nvm's node folder. Agora appends those when they exist, for both finding the CLIs and running them.
- **No colour codes, no stdin, no auto-update.** Agents get `NO_COLOR=1`, an empty stdin so nothing waits for a keypress, and Claude's auto-updater off so twenty seats do not all try to update at once.

Things Agora cannot fix, which Settings > CLIs warns about when it sees them:

- `ANTHROPIC_API_KEY` in the environment: in non-interactive mode Claude Code always uses it instead of your login. Unset it before starting Agora if you want the login.
- Agora started over SSH on a Mac: Claude Code's login lives in the macOS Keychain, which is locked for SSH sessions. Start Agora from a terminal on the Mac.
- `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`: Claude Code talks to that endpoint or cloud instead.

Copilot CLI authenticates from the login saved by `copilot login`, or from `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, or `GITHUB_TOKEN` if one is set. Read-only seats run it without tool permissions, which still allows reading and searching; full-access seats pass `--allow-all-tools`. Its model list in Agora is just `auto`, which lets Copilot choose: which named models an account may pass to `--model` varies by plan, so if you want a specific one, type its ID in the seat's Custom box and press Test on the Copilot card to confirm it is accepted.

Flags:

| Flag | Meaning |
| --- | --- |
| `--port N` | Serve on a different port (default 8765). |
| `--local-only` | Bind to 127.0.0.1 only. By default Agora also listens on your LAN so you can follow along from a phone. |

## How a conversation works

**Two speaking modes.**

- *Take turns.* Agents speak one after another in seat order. Each round every agent speaks once, then everyone gives a ranked closing statement. You can add more rounds afterwards with Continue.
- *Open floor.* No turns. Every message goes to everyone at once and each agent either replies or answers `PASS`. Use `@Name` in a message to demand an answer from someone. The floor closes when everyone passes on the latest message, when a message or time limit you set is reached, or when you press End. Closing statements follow.

**Game framing.** Switching a draft conversation from Council to Game makes it playable without further setup: Agora seats the World as referee if there is none, gives every character without a stance a stat block, seats four characters if none had one, sets the arena topic if the topic was still the default, and switches to six rounds of turns. Everything it added is editable. Switching back to Council removes the World and the characters Agora seated, blanks borrowed stat blocks, and restores the topic. Seats you named or wrote a stance for are kept in both directions.

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

Agora can message you on Telegram when a conversation finishes, and send you the phone link. Open Settings and press **Connect Telegram**; a three-step wizard walks you through it in about three minutes:

1. **Create a bot.** In Telegram, message @BotFather, send `/newbot`, pick a name and a username ending in `bot`, and paste the token it gives you into the wizard. Press Check and Agora confirms the bot's name.
2. **Tell the bot who you are.** Open the chat with your new bot (the wizard links to it), press Start, send it any message, then press Find my chat. If another program is already reading that bot, the wizard says so and you can paste your id from @userinfobot instead.
3. **Test and save.** Send yourself a test message, choose whether to be notified when conversations finish, and press Save.

The wizard opens whenever you press the Telegram button while nothing is connected, including after you press Disconnect or blank the file. Once connected the same button sends the phone link, and Change reopens the wizard.

Everything is stored in `agora_telegram.json` next to the script (the file `agora_telegram.example.json` shows the shape). The token is only ever sent to Telegram and never to the browser. Agora deliberately does not poll the bot for updates, so a bot you already use with another program keeps working.

## Codex notes

Codex refuses to run non-interactively in a folder it has not been told to trust. When a Codex seat is present, Agora adds the working folder to `~/.codex/config.toml` as trusted (a backup of the file is saved alongside) and records a note in the chat. Codex is also run with `--skip-git-repo-check` so folders that are not git repositories work.

## Configuration in the source

A few defaults live at the top of `agora.py`:

- `DEFAULT_REPO`: the folder new conversations start in. Change it to your own project, or just pick a folder in the dashboard.
- `PROVIDERS`: the CLI command templates and model lists for each provider. Add a provider or model here. Any seat can also use a custom model name typed in the UI. A provider with `"gate": True` starts one process at a time.
- `TURN_TIMEOUT`: how long one agent may take per turn, in seconds.
- `FRAMING`, `OPENING`, `REPLY`, `OPEN_*`, `VOTE`: the council prompts.
- `GAME_FRAMING`, `WORLD_STANCE`, `GAME_OPENING`, `GAME_REPLY`, `GAME_VOTE`: the game prompts.
- `TEMPLATES`: the built-in seat layouts.

## Files not committed

`.gitignore` excludes the access token, the Telegram config, your saved templates, your CLI paths, and all saved conversations and run output. Those stay on your machine.

## License

Copyright (c) 2026 Hillside Ventures LLC. All rights reserved. No license is granted; see [LICENSE](LICENSE).
