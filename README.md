# Agora

Recorded conversations between CLI coding agents, sitting inside the same folder.

Agora seats several AI coding agents (Claude Code, Codex, OpenCode, Gemini CLI) at one table, gives them a topic, and lets them talk to each other. Every agent runs in its own real CLI inside the folder you choose, so it can read files, search, and check git history to back up what it says. A small local web dashboard shows each agent's terminal live, and the chat in the middle holds only the finished speeches, in order, like a group conversation.

Conversations are saved with per-agent memory, so you can reopen any past one and continue it with the same agents.

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
2. Add seats. Each seat has a name, a provider, a model, an optional stance ("Skeptic", "Defender", ...), and a color.
3. Write the topic (the charge) and any extra instructions.
4. Choose how they speak and how many rounds.
5. Open the floor.

Flags:

| Flag | Meaning |
| --- | --- |
| `--port N` | Serve on a different port (default 8765). |
| `--local-only` | Bind to 127.0.0.1 only. By default Agora also listens on your LAN so you can follow along from a phone. |

## How a conversation works

**Two speaking modes.**

- *Take turns.* Agents speak one after another in seat order. Each round every agent speaks once, then everyone gives a ranked closing statement. You can add more rounds afterwards with Continue.
- *Open floor.* No turns and no limits. Every message goes to everyone at once and each agent either replies or answers `PASS`. Use `@Name` in a message to demand an answer from someone. When everyone passes on the latest message, the floor closes and closing statements begin.

**You are the convener.** You can interject at any time from the dashboard. Agents are told to address what you said before anything else.

**Knowability rule.** Agents must label claims about themselves as OBSERVED (visible in their context or environment right now), DOCUMENTED (public docs or source they can cite), or GUESS. They are told to read files, cite paths, and disagree openly.

**Read only by default.** With Read only ticked, seats can read, search, and inspect git history but cannot run or change anything (Claude gets a restricted tool allowlist, Codex runs with `--sandbox read-only`, Gemini runs without `--yolo`). Untick it for full access with permission prompts off. If you do, use a folder with a clean git status.

**Memory and resuming.** Each agent has its own memory file that only it reads. The prompt for a turn carries only what was said since that agent last spoke, and the agent is told where its full history lives. Claude seats also resume their CLI session between turns. You can swap a seat's model mid-conversation and the change applies on its next turn, with a note written into that agent's memory.

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

Past conversations appear in the sidebar. Open one to reread it or to continue it with the same agents.

## Phone and away-from-home access

Unless you pass `--local-only`, Agora prints a LAN URL you can open on a phone on the same wifi. If [Tailscale](https://tailscale.com) is installed, it also prints a Tailscale URL that works from anywhere.

Access is protected by a token stored in `agora_token.txt` next to the script. It is generated on first run. Delete the file to rotate it.

## Telegram notifications (optional)

Copy `agora_telegram.example.json` to `agora_telegram.json` and fill in a bot token and your chat id:

```json
{"token": "123456:ABC...", "chat_id": "123456789", "notify_on_finish": true}
```

Message `@userinfobot` on Telegram to get your chat id. Agora will send you a message when a conversation finishes.

## Configuration in the source

A few defaults live at the top of `agora.py`:

- `DEFAULT_REPO`: the folder new conversations start in. Change it to your own project, or just pick a folder in the dashboard.
- `PROVIDERS`: the CLI command templates and model lists for each provider. Add a provider or model here. Any seat can also use a custom model name typed in the UI.
- `TURN_TIMEOUT`: how long one agent may take per turn, in seconds.
- `FRAMING`, `OPENING`, `REPLY`, `OPEN_*`, `VOTE`: the prompts that shape how agents speak.

## Files not committed

`.gitignore` excludes the access token, the Telegram config, and all saved conversations and run output. Those stay on your machine.

## License

MIT. See [LICENSE](LICENSE).
