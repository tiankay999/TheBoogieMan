# The Boogeyman

A personal, voice-first desktop AI assistant for Linux, in the spirit of Jarvis. You talk, he answers out loud, with a bit of Ghanaian Pidgin. He remembers you, researches the web live on screen, reads your screen when you ask, and runs his own UI by voice: work mode, devil mode, backgrounds, shutdown.

> "Welcome back, boss. I'm listening."

**Platform:** Ubuntu 22.04 / 24.04 (other Linux distros with WebKitGTK 4.1 should work). macOS and Windows aren't supported yet.

---

## Contents

- [Quick start](#quick-start)
- [1. Install system requirements](#1-install-system-requirements)
- [2. Install project dependencies](#2-install-project-dependencies)
- [3. Get your API keys](#3-get-your-api-keys)
- [4. Run it](#4-run-it)
- [What you can say](#what-you-can-say)
- [Make him yours](#make-him-yours)
- [Troubleshooting](#troubleshooting)
- [How it's built](#how-its-built)
- [Development](#development)

---

## Quick start

If you already have Rust, Node 20+ and the Tauri system packages:

```bash
git clone https://github.com/Hidden-Umah/TheBoogieMan.git
cd TheBoogieMan
make setup        # npm install + Python venv
make env          # creates .env from .env.example
# open .env and paste ELEVENLABS_API_KEY and GROQ_API_KEY
make dev          # opens the app
```

The first `make dev` compiles Rust and takes a few minutes. After that it's quick.

---

## 1. Install system requirements

**System packages** (Tauri, audio, video playback):

```bash
sudo apt update
sudo apt install -y libwebkit2gtk-4.1-dev build-essential curl wget file \
  libxdo-dev libssl-dev libayatana-appindicator3-dev librsvg2-dev \
  python3 python3-venv libportaudio2 \
  gstreamer1.0-plugins-good gstreamer1.0-libav
```

- `libportaudio2` is for the microphone and speakers.
- The `gstreamer` packages play the background videos.

**Rust** (stable):

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source "$HOME/.cargo/env"
```

**Node.js 20 or newer**, e.g. with [nvm](https://github.com/nvm-sh/nvm):

```bash
nvm install 22
```

**Python 3.10 or newer.** Ubuntu 22.04+ already has it.

You also need a **microphone and speakers**. Headphones are best, so he doesn't hear himself.

---

## 2. Install project dependencies

```bash
make setup
```

This runs `npm install` in `app/` and creates the Python virtualenv in `brain/.venv` with everything in `brain/requirements-dev.txt`.

---

## 3. Get your API keys

All keys live in **`.env`**. Create it from the template:

```bash
make env          # same as: cp .env.example .env
```

Then open `.env` (in the project root) and paste your keys. [`.env.example`](.env.example) has every setting, with step-by-step instructions inside.

| Key | What it does | Cost | Required? |
|-----|--------------|------|-----------|
| `ELEVENLABS_API_KEY` | His ears and voice (speech-to-text + text-to-speech) | Free plan works | **Yes**, for voice |
| `GROQ_API_KEY` | His brain (the AI that replies) | Free tier | One brain key is needed |
| `ANTHROPIC_API_KEY` | Brain alternative: Claude. Used first if set. | Paid | Optional |
| `LLM_PROVIDER=openai` + `LLM_BASE_URL` + `LLM_API_KEY` + `LLM_MODEL` | Any OpenAI-compatible API (OpenRouter, Gemini, local) | Varies | Optional |

### ElevenLabs (voice)

1. Sign up at [elevenlabs.io](https://elevenlabs.io).
2. Click your profile (bottom left), then **API Keys → Create API key**.
3. Give it these permissions:
   - **Text to Speech**: Access
   - **Speech to Text**: Access
   - **Voices**: Read
4. Copy the key (starts with `sk_`) into `ELEVENLABS_API_KEY=`.

The default voice is "Brian". To change it, copy a Voice ID from your ElevenLabs Voices page into `ELEVENLABS_VOICE_ID`.

### Groq (brain, free)

1. Sign up at [console.groq.com](https://console.groq.com).
2. Go to **API Keys → Create API Key**.
3. Copy the key (starts with `gsk_`) into `GROQ_API_KEY=`.

Groq's free tier has per-minute and per-day limits. When one model runs out, he switches to the next free one on his own (`gpt-oss-120b` → `llama-3.3-70b-versatile` → `gpt-oss-20b` → `llama-3.1-8b-instant`). He only asks you to wait if all of them are used up.

### Rules for keys

- Already have keys in `brain/.env` from an older setup? That still works. If both files exist, `brain/.env` wins.
- `.env` is **gitignored**. Never commit it, and never put keys in `.env.example`, issues or chat.
- Keys are only read by the Python brain. They never reach the UI.
- Without a brain key he still hears and speaks, but only knows a few built-in lines and the instant commands.
- At startup the terminal prints which brain he's using, e.g. `brain: groq:openai/gpt-oss-120b`.

---

## 4. Run it

```bash
make dev
```

Vite starts, Rust builds, and the window opens. Rust then starts the Python brain, and after the boot screen he greets you out loud. Say "hello".

The status under the core shows what he's doing: **LISTENING**, **HEARING YOU**, **THINKING** or **SPEAKING**. If it says **VOICE OFFLINE**, the reason is shown next to it (usually a missing key or no microphone).

### Keyboard

| Keys | Action |
|------|--------|
| `Ctrl+H` | Commands card (everything you can say). `Esc` closes it |
| `Ctrl+M` | Mute / unmute the mic |
| `Ctrl+B` | Next background |
| `Ctrl+D` | Devil mode |

---

## What you can say

Say **"What can you do?"** at any time to see this on screen.

**Talk**
- Anything. He answers in voice.
- "Remember that …" / "forget …": saves or removes a fact about you, kept across restarts.
- "Clear our conversation": wipes chat history. Saved facts stay.
- **Talk over him** and he stops mid-sentence to listen. "Pause" / "stop" / "wait" keeps him quiet. "Continue" / "go on" picks up where he stopped.

**Work**
- "Let's get to work": the core slides away and the workspace opens.
- "Research …" / "look up …" / "search for …": live web research in the workspace, with cards for queries, sources, findings and progress. Then he reads you the summary.
- "Back to the normal screen" / "exit work mode" / "go home": closes the workspace.

**Eyes**
- "What's on my screen?" / "What does this error say?": he takes a look and tells you. He
  reaches for this himself whenever a question is about something in front of you.
- "Study mode" / "watch my screen": he goes quiet and watches. When there's a question on
  screen he gives the answer and one short line of why, then does the same for the next one.
  He says nothing at all when there's no question in front of him.
- "Further more": opens up the last answer properly - why it's right, why the others aren't.
- "Stop studying" / "exit study mode": he stops watching.

**Screen**
- "Change the background" / "sea background" / "nebula" / "background off" / "background on".
- "Devil mode" / "go devil" / "unleash": the visor drops, everything goes red and the eye opens. "Stand down" / "exit devil mode" ends it.

**Power**
- "Shut down": he asks first. Say "yes" to confirm or "no" to keep him.
- "Shut down now": closes immediately.

The phrases above are instant commands and work even without a brain key. The AI also has each action as a **tool** (`shutdown`, `switch_screen`, `set_background`, `devil_mode`, `show_commands`, `clear_conversation`, `research`, `look_at_screen`), so free phrasing works too, e.g. "abeg close yourself" or "take me back home".

---

## Make him yours

- **Personality:** edit [`brain/persona.md`](brain/persona.md) (tone, languages, rules). It applies on the next reply, with no restart.
- **About you:** copy `brain/boss.example.md` to `brain/boss.md` (gitignored) and write about yourself: name, work, what you care about.
- **Memory:** stored in `~/.local/share/boogeyman/memory.json` (facts plus recent conversation). Delete the file to wipe it.
- **Backgrounds:** put `custom.mp4` or `custom.webm` in `app/public/bg/` and it joins the list.

---

## Troubleshooting

Start with the **terminal running `make dev`**. Every turn logs `heard:`, `command:`, `tool:` and `reply:` lines, and background videos log `[bg]` lines.

| Problem | Fix |
|---------|-----|
| VOICE OFFLINE | Check `ELEVENLABS_API_KEY` and its permissions (above). Check a mic is connected. To pick a specific mic, run `brain/.venv/bin/python -m sounddevice` and set `BOOGEYMAN_MIC` to its name or index. |
| He only says canned lines | No brain key. Set `GROQ_API_KEY` (or another provider) in `.env` and restart. |
| "I hit the free limit…" | Every free Groq model is rate-limited. Wait the time he says, or lower `BOOGEYMAN_HISTORY` so each reply uses fewer tokens. |
| He cuts himself off while talking | His own voice from the speakers sounds like you talking. Use headphones, set `BOOGEYMAN_BARGE_LEVEL=1.5`, or `BOOGEYMAN_BARGE_IN=0`. |
| He insists he "can't" do something | Old replies in his history. Say "clear our conversation" once. |
| Background video black / frozen / not playing | Look for `[bg]` lines. Install `gstreamer1.0-plugins-good gstreamer1.0-libav` and restart. A video that won't play falls back to the nebula, and a notice appears on screen. |
| Blank or white window | Try `WEBKIT_DISABLE_DMABUF_RENDERER=1 make dev` (common with NVIDIA drivers). |
| `make dev` can't find the brain | Run `make setup` again. Rust looks for `brain/.venv/bin/python`; override it with `BOOGEYMAN_PYTHON=/path/to/python`. |
| Research finds nothing | It uses DuckDuckGo via `ddgs`, so you need internet. Run `make setup` to make sure `ddgs` is installed. |

---

## How it's built

Three languages, one app:

| Layer | Folder | Language | Job |
|-------|--------|----------|-----|
| Face | `app/src` | React + TypeScript + three.js | The HUD: core, curved screens, workspace, devil mode |
| Body | `app/src-tauri` | Rust (Tauri v2) | Window, OS integration, starts and talks to the brain |
| Brain | `brain/` | Python (FastAPI) | Voice loop, AI, memory, commands, research, eyes |

```
React --invoke--> Rust --WebSocket--> Python
                   ^                    |
                   +---- events --------+
```

Every message uses one shape, `{ id, type, payload }`. The full list is in [`shared/messages.md`](shared/messages.md). For the why and the architecture, read [`docs/OVERVIEW.md`](docs/OVERVIEW.md).

```
app/
  src/                React UI
    App.tsx           wires brain events to the screen
    components/       WorkMode (workspace cards), CommandsCard, Visor, ...
    three/            3D scene: DotCore, DataArcs, eye, HudScreens, VideoBackground
    lib/              brain bridge, audio levels, curve math
  src-tauri/src/      Rust: lib.rs (commands), sidecar.rs (starts Python), brain.rs (WebSocket)
  public/bg/          background videos
brain/
  main.py             FastAPI app, tools the AI can call, UI actions
  voice.py            mic → speech-to-text → think → speech (with barge-in)
  reply.py            the AI: providers, tool loop, Groq fallbacks
  commands.py         instant voice commands (no AI needed)
  research.py         plan → search → read → analyze
  memory.py           facts + recent conversation
  persona.md          his personality
.env.example          every setting and key, documented (copy to .env)
shared/messages.md    message protocol
docs/OVERVIEW.md      architecture and decisions
tools/                asset generators (the nebula background)
```

---

## Development

```bash
make test     # Python tests, Rust clippy, TypeScript build
make build    # release build (.deb + AppImage); bundling the Python brain is still to come
make clean    # remove build output and the venv
```

- **Workflow:** issue → branch → PR → merge. The roadmap lives in GitHub issues: Skeleton → Agent loop → Voice → Hands → Presence → Memory → Crazy stuff.
- **Env overrides for Rust:** `BOOGEYMAN_BRAIN_DIR` (where `brain/` is) and `BOOGEYMAN_PYTHON` (which Python to run).
- **Adding a voice command:** add a regex in `brain/commands.py`, handle the action in `ui_action()` in `brain/main.py`, and push a UI event the React app listens for in `App.tsx`. To let the AI trigger it too, register a tool with `replier.add_tool(...)` in `main.py`.
- **New message types:** document them in `shared/messages.md`.
