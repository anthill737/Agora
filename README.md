# Agora

Recorded conversations between CLI coding agents, sitting inside the same folder.

Agora seats several AI coding agents (Claude Code, Codex, OpenCode, Gemini CLI) at one table, gives them a topic, and lets them talk to each other. Every agent runs in its own real CLI inside the folder you choose, so it can read files, search, and check git history to back up what it says. A small local web dashboard shows each agent's terminal live, and the chat in the middle holds only the finished speeches, in order, like a group conversation.

Conversations are saved with per-agent memory, so you can reopen any past one and continue it with the same agents. Several conversations can run at the same time.

Agora can also run as a game instead of a council: characters with fixed stats, a World that referees, and one action per message.

## Quick start

Requirements:

- Python 3.10 or newer. Standard library only, nothing to `pip install`.
- At least one agent CLI. Agora installs them and signs in to them from Settings, Connections, so they do not have to be set up first:
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

1. Open the gear, then Connections, and connect at least one CLI.
2. Decide where the agents sit. The default is **Nowhere in particular**: an empty folder Agora keeps for itself, always read-only, right for most councils and for every game. Choose **A folder I choose** when the conversation is about code the agents should read.
3. Press **Agents** and seat them. Each seat has a name, a CLI, a model, a colour, and an optional stance ("Skeptic", "Defender", a character sheet).
4. Press **Topic** for the charge, Council or Game, how they speak, rounds or limits, where they sit, and templates.
5. Press **Start**.

## The screen

The header holds the conversation and nothing else: the conversations toggle, the title, what is happening now, one button that reads Start, Pause, Resume or Continue depending on where the conversation is, then **Agents**, **Topic**, and **End** while it is running. Everything else lives behind the gear, in this order: Connections, Export, Phone, Display, and at the bottom, in red, Stop this conversation and Quit Agora.

Agents and Topic open as panels over the page. One opens at a time, Escape or the X closes it, and edits save as you type. On a phone the tabs at the bottom are Chat, Terminals, Conversations, and More, where More holds Agents, Topic, End, and everything from the gear.

## Connections

Every agent is a CLI you would otherwise run yourself, so Agora needs it installed and signed in. Settings, Connections has one row per CLI with its name, its version, and its state, taken from that CLI's own status command:

| CLI | How Agora asks |
| --- | --- |
| Claude Code | `claude auth status` |
| Codex | `codex login status` |
| Codex (latest) | the same, since the npx seat shares `~/.codex/auth.json` |
| OpenCode | `opencode auth list` |
| Gemini CLI | `gemini auth status`, then the credentials in `~/.gemini` |
| Copilot CLI | nothing, because it has no status command and keeps its token in the operating system credential store. The row stays amber until you press Check, which asks it one tiny question |

Every installed CLI is checked when Agora starts, so the rows are already coloured when you open them. Green is connected, amber is checking or unknown, red says what is wrong.

Each row offers only what it needs:

- **Install**, when the CLI is missing. Claude Code and Codex use their vendors' own installers; the rest use npm into `agora_tools` beside the script, so nothing needs an administrator and nothing already on the machine is touched. The installer's output streams under the row. If Node.js is missing for an npm install, the row links to nodejs.org instead. Agora never reinstalls or updates a CLI that is already there.
- **Sign in**, which opens a real terminal window with that CLI's own sign-in already running, the way you would run it yourself. The browser opens from there. Agora then asks the CLI every fifteen seconds for ten minutes and turns the row green by itself.
- **API key**, for the CLIs that take one. It is masked, it sets that vendor's environment variable for the CLIs Agora starts, it lives in memory for this run only, and it is never written to disk. Codex has no key field on purpose: Agora drives Codex through the ChatGPT subscription sign-in.
- **Already installed?**, for pasting the full path when a CLI exists but is not on `PATH`.

Nothing is offered when a CLI is connected.

Connection state appears in exactly one other place: pressing Start with a seat whose CLI is not connected does not start the conversation and puts a single line under the header naming the CLI, with a link into Connections.

### What Agora does so the CLIs behave

Each agent is started with your own environment, adjusted in a few ways that matter in practice:

- **Claude Code seats start one at a time.** Claude Code keeps one login token per machine and rotates it on refresh, so several copies starting at the same moment log each other out. Agora holds a gate from launch until each Claude seat has authenticated, then starts the next.
- **No nested-session markers.** If Agora itself was started from inside a Claude Code session, the `CLAUDECODE` and `CLAUDE_CODE_ENTRYPOINT` variables it inherits would make every Claude seat hang at startup. Agora removes them for its agents.
- **The installers' folders are on `PATH`.** A CLI installed after Agora started is not on the PATH this process captured at launch, so Agora also looks in `~/.local/bin`, the npm prefix, Homebrew, nvm, volta, fnm, and its own `agora_tools`.
- **No colour codes, no stdin, no auto-update.** Agents get `NO_COLOR=1`, an empty stdin so nothing waits for a keypress, and Claude's auto-updater off so twenty seats do not all try to update at once.

Things Agora cannot fix, which the Connections rows warn about when they are true: `ANTHROPIC_API_KEY` in the environment, which Claude Code always prefers over your login in non-interactive mode; Agora started over SSH on a Mac, where the Keychain holding the Claude login is locked; and `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_USE_BEDROCK` or `CLAUDE_CODE_USE_VERTEX`, which send Claude somewhere else entirely.

## How a conversation works

**Two speaking modes.**

- *Take turns.* Agents speak one after another in seat order. Each round every agent speaks once, then everyone gives a ranked closing statement. You can add more rounds afterwards with Continue.
- *Open floor.* No turns. Every message goes to everyone at once and each agent either replies or answers `PASS`. Use `@Name` in a message to demand an answer from someone. The floor closes when everyone passes on the latest message, when a message or time limit you set is reached, or when you press End. Closing statements follow.

**Where the agents sit.** Every CLI needs a working folder, so "nowhere in particular" is really an empty, disposable folder named `agora_room` next to the script. Agora creates it on demand, recreates it if you delete it, and marks it trusted for Codex once. Conversations in the room are read-only with no full-access option: there is nothing there to write, and the only thing full access could do is let an agent reach the rest of the disk. The council prompt drops its "read this folder and cite files" lines in the room. Templates and saved conversations remember the choice.

**Game framing.** Switching a draft conversation from Council to Game makes it playable without further setup: Agora seats the World as referee if there is none, gives every character without a stance a stat block, seats four characters if none had one, sets the arena topic if the topic was still the default, moves the game into the room, and switches to six rounds of turns. Everything it added is editable. Switching back to Council removes the World and the characters Agora seated, blanks borrowed stat blocks, and restores the topic. Seats you named or wrote a stance for are kept in both directions.

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

**Trust.** Codex refuses to run non-interactively in a folder it has not been told to trust. When a Codex seat is present, Agora adds the working folder to `~/.codex/config.toml` as trusted (a backup of the file is saved alongside) and records a note in the chat. Codex is also run with `--skip-git-repo-check` so folders that are not git repositories work.

**Rotating tokens.** Codex refresh tokens rotate: the moment one process refreshes, the token every other process holds is dead. Several Codex seats starting at once therefore race, all but one lose with a 401, and a losing writer can leave `~/.codex/auth.json` half written. Agora avoids the race rather than retrying through it.

- Before any turn or open-floor batch that includes Codex seats, one short priming prompt runs through Codex alone, so exactly one process performs the refresh. The credentials it leaves behind are copied to `agora_codex_auth.bak` beside the script.
- If a Codex seat is refused as signed out anyway, the conversation pauses at once, the copy goes back, the priming prompt runs again, and the conversation resumes by itself if that worked.
- If it did not work, the conversation stays paused and says so: "Codex signed out, sign in and press Resume", with a link into Connections. Pressing Resume primes again before carrying on.
- One conversation uses one Codex install. If seats mix the pinned global `codex` with the npx latest, Agora moves them all onto one at Start and records a note, because two programs sharing one rotating token is the same race again.

Seats still run fully in parallel. Only the priming prompt runs on its own.

## Configuration in the source

A few defaults live at the top of `agora.py`:

- `DEFAULT_REPO`: the folder offered when a conversation picks "A folder I choose". New conversations start in the room instead.
- `PROVIDERS`: the CLI command templates and model lists for each provider. Add a provider or model here. Any seat can also use a custom model name typed in the UI. A provider with `"gate": True` starts one process at a time.
- `TURN_TIMEOUT`: how long one agent may take per turn, in seconds.
- `FRAMING`, `OPENING`, `REPLY`, `OPEN_*`, `VOTE`: the council prompts.
- `GAME_FRAMING`, `WORLD_STANCE`, `GAME_OPENING`, `GAME_REPLY`, `GAME_VOTE`: the game prompts.
- `TEMPLATES`: the built-in seat layouts.

## Files not committed

`.gitignore` excludes the access token, the Telegram config, your saved templates, your CLI paths, the empty room, the CLIs Agora installs into `agora_tools`, the Codex credential copy, and all saved conversations and run output. Those stay on your machine.

## License

Copyright (c) 2026 Hillside Ventures LLC. All rights reserved. No license is granted; see [LICENSE](LICENSE).
