# The Boogeyman

A personal, voice-first desktop AI assistant, in the spirit of Jarvis. You talk, he answers out loud, with a bit of Ghanaian Pidgin. He remembers you, researches the web live on screen, reads your screen, opens your apps and types for you, studies with you, and runs his own UI by voice: work mode, devil mode, backgrounds, shutdown.

> "Welcome back, boss. I'm listening."

**Platform:** developed on Ubuntu 22.04 / 24.04 (other Linux with WebKitGTK 4.1 should work). Windows runs too — the Rust side looks for `.venv/Scripts/python.exe`, and the brain has Windows paths for launching apps, reading keys and grabbing the screen — but the `Makefile` is POSIX-only, so setup there is by hand ([see below](#on-windows)). macOS is untested.

**No API key at all?** He still speaks: his voice falls back to Microsoft Edge's neural voices, which need no key and no account. One free Groq key gets you his ears and his brain as well.

---

## Contents

- [Quick start](#quick-start)
- [1. Install system requirements](#1-install-system-requirements)
- [2. Install project dependencies](#2-install-project-dependencies)
- [3. Get your API keys](#3-get-your-api-keys)
- [4. Run it](#4-run-it)
- [What you can say](#what-you-can-say)
- [How he sounds](#how-he-sounds)
- [Eyes and study mode](#eyes-and-study-mode)
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
# open .env and paste GROQ_API_KEY (free). That alone is enough.
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

### On Windows

The app runs, but `make` doesn't. Do the two setup steps by hand, then use the Tauri dev command:

```powershell
cd app; npm install; cd ..
cd brain
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
cd ..\app
npm run tauri dev
```

Rust finds the brain at `brain/.venv/Scripts/python.exe` on its own. If the Windows Store Python shim is installed, bare `python` may print "Python was not found" — call `.venv\Scripts\python.exe` directly instead. You don't need the apt packages; audio and screen capture use the OS.

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
| `GROQ_API_KEY` | His brain, his ears, and a spare voice | Free tier | **The one to get.** Everything works with just this |
| `ELEVENLABS_API_KEY` | A better-sounding voice and sharper ears | Free plan: 10k characters/month | Optional — without it he uses the free voice |
| `ANTHROPIC_API_KEY` | Brain upgrade: Claude. Used first if set, and reads a screen with its own model rather than a separate vision one | Paid | Optional |
| `LLM_PROVIDER=openai` + `LLM_BASE_URL` + `LLM_API_KEY` + `LLM_MODEL` | Any OpenAI-compatible API (OpenRouter, Gemini, local) | Varies | Optional |

With **no keys at all** he can't hear you, but he can still talk: Edge's voice needs no account. Add `GROQ_API_KEY` and he gets ears and a brain too.

### Groq (brain + ears + a voice, free)

1. Sign up at [console.groq.com](https://console.groq.com).
2. Go to **API Keys → Create API Key**.
3. Copy the key (starts with `gsk_`) into `GROQ_API_KEY=`.

That one key covers three jobs: the replying model, Whisper for hearing you, and optionally Orpheus for speaking.

Groq's free tier limits each model separately, per minute and per day. When one runs out he moves to the next on his own (`openai/gpt-oss-120b` → `llama-3.3-70b-versatile` → `openai/gpt-oss-20b` → `llama-3.1-8b-instant`). He only asks you to wait when all of them are spent.

### ElevenLabs (optional, best voice)

1. Sign up at [elevenlabs.io](https://elevenlabs.io).
2. Click your profile (bottom left), then **API Keys → Create API key**.
3. Give it these permissions:
   - **Text to Speech**: Access
   - **Speech to Text**: Access
   - **Voices**: Read
4. Copy the key (starts with `sk_`) into `ELEVENLABS_API_KEY=`.

The default voice is "Brian". To change it, copy a Voice ID from your ElevenLabs Voices page into `ELEVENLABS_VOICE_ID`.

The free plan is 10,000 characters a month, which a chatty evening can finish. He doesn't go silent when it runs out — at startup he checks what the key is actually allowed to do, and the moment ElevenLabs refuses he switches to the free voice mid-session.

### Which brain he uses

`LLM_PROVIDER=auto` (the default) picks Anthropic if its key is set, otherwise Groq. The defaults are `claude-haiku-5-5` on Anthropic and `openai/gpt-oss-120b` on Groq; override with `LLM_MODEL`.

Two things are tuned for talking rather than reading:

- **Claude's thinking is off** on voice turns (`BOOGEYMAN_THINK=0`). Measured on "how far boss": 146 output tokens of deliberation to produce "doing fine, ready when you are", and 748 ms of silence before the first word — about half the wait on a short turn. Set it to `1` if you'd rather have the reasoning. The research agent keeps its own either way.
- **His system prompt is cached.** Persona, memory and tool definitions are a stable prefix marked with Anthropic's ephemeral `cache_control`, so you pay full price for them once instead of every turn.

He also keeps HTTP connections warm between turns (`BOOGEYMAN_KEEPALIVE`), because on a slow link the TLS handshake was the thing timing out, not the model.

### Rules for keys

- Already have keys in `brain/.env` from an older setup? That still works. If both files exist, `brain/.env` wins.
- `.env` is **gitignored**. Never commit it, and never put keys in `.env.example`, issues or chat.
- Keys are only read by the Python brain. They never reach the UI.
- Without a brain key he still hears and speaks, but only knows a few built-in lines and the instant commands.
- At startup the terminal prints what he settled on, e.g. `brain: groq:openai/gpt-oss-120b` and `voice: edge:en-US-BrianMultilingualNeural | ears: groq:whisper-large-v3-turbo`.

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

Say **"What can you do?"** at any time to see the core set on screen.

**Talk**
- Anything. He answers in voice.
- "Remember that …" / "forget …": saves or removes a fact about you, kept across restarts.
- "Clear our conversation": wipes chat history. Saved facts stay.
- **Talk over him** and he stops mid-sentence to listen. "Pause" / "stop" / "wait" keeps him quiet. "Continue" / "go on" picks up where he stopped.

**Work**
- "Let's get to work": the core slides away and the workspace opens.
- "Research …" / "look up …" / "search for …": live web research in the workspace, with cards for queries, sources, findings and progress. Then he reads you the summary.
- "Back to the normal screen" / "exit work mode" / "go home": closes the workspace.

**Hands**
- "Open Spotify" / "open my notes": launches an app, or a file in his workspace. Never through a shell, so nothing he mis-hears can be read as shell syntax.
- "Close Chrome": closes a running app. He refuses to close the shell, the desktop, or himself.
- "What's in my workspace?" / "make me a file called ideas.txt with …": he can list and write text files, but only inside one folder (`BOOGEYMAN_WORKSPACE`, default `~/Boogeyman`). Anything that resolves outside it is refused, symlinks included.
- "Delete notes.txt": **two steps, always.** The first attempt only arms it and makes him ask; the real delete needs you to say yes, and moves the file to `.trash` rather than destroying it. That gate lives in the code, not in his prompt, so no amount of clever phrasing skips it.
- "Type this in my browser: …": he types on the keyboard into whatever window has focus, after asking. He can tell you which window that is first.

**Eyes**
- "What's on my screen?" / "What does this error say?": he takes a look and tells you. He reaches for this himself whenever a question is about something in front of you.
- "Study mode" / "watch my screen": he goes quiet and watches. When there's a question on screen he gives the answer and one short line of why, then does the same for the next one. He says nothing at all when there's no question in front of him.
- "Further more": opens up the last answer properly — why it's right, why the others aren't.
- "Stop studying" / "exit study mode": he stops watching.

**Screen**
- "Change the background" / "sea background" / "nebula" / "background off" / "background on".
- "Devil mode" / "go devil" / "unleash": the visor drops, everything goes red and the eye opens. "Stand down" / "exit devil mode" ends it. Devil mode is a mood, not permission — every limit above still holds.

**Power**
- "Shut down": he asks first. Say "yes" to confirm or "no" to keep him.
- "Shut down now": closes immediately.

The phrases above are instant commands and work even without a brain key. The AI also has each action as a **tool**, so free phrasing works too, e.g. "abeg close yourself" or "take me back home":

| | |
|---|---|
| UI | `shutdown`, `switch_screen`, `set_background`, `devil_mode`, `show_commands`, `clear_conversation` |
| Hands | `open_app`, `close_app`, `list_files`, `create_file`, `delete_file`, `focused_window`, `type_text` |
| Eyes | `look_at_screen`, `study_mode` |
| Web | `research` |

---

## How he sounds

Three voices, chosen at boot by `BOOGEYMAN_TTS=auto`: ElevenLabs if its key works, otherwise **Edge**, which needs no key. Force one with `elevenlabs`, `edge` or `groq`. The ears work the same way (`BOOGEYMAN_STT`): ElevenLabs' `scribe_v2`, or Whisper large v3 turbo on Groq.

### The free voice

Edge gives you 300-odd neural voices for nothing (`edge-tts --list-voices`). Two settings matter:

```bash
EDGE_VOICE=en-US-BrianMultilingualNeural
EDGE_RATE=+40%
```

Things worth knowing before changing them, all measured rather than guessed:

- **There is no Ghanaian voice in Edge.** The West African locales stop at Nigerian. This started on a Nigerian voice, on the theory that it would carry Pidgin better; it doesn't — a Nigerian accent reading Ghanaian Pidgin lands worse than a neutral one. So there's no accent to match, and the pick comes down to pace and taste.
- **Pace belongs to the voice, not to the rate knob.** Unpushed, the 23 English male voices read between 12.0 and 17.4 characters a second. Brian Multilingual is joint-fastest; Steffan sits eleventh, and needs `+53%` to reach the pace Brian hits at `+30%`. Pick a quick reader first, then push it gently — a slow voice cranked hard starts to sound artificial.
- **The rate knob has diminishing returns.** On a two-sentence reply: `+30%` is 5.57 s, `+40%` 5.16 s, `+50%` 4.82 s, `+60%` 4.51 s. Past `+40%` he starts to sound wound up, so that's the default.
- **Edge charges per request, not per character** — about 1.6 s either way (1.15 s for 17 characters, 1.69 s for 104). That's Microsoft synthesizing, not a connection being set up: pooling a keep-alive connection across turns came out *slower* than reconnecting, so it isn't done. What it does mean is that his first sentence should be short and later ones long, which is what `split_first` does.

Devil mode can have its own voice (`EDGE_DEVIL_VOICE`), which is a better trick than slowing the normal one down: keep a quick reader for talking, and put the slow, deep one (`en-US-ChristopherNeural`) where the menace is wanted and the extra seconds are rare.

### Orpheus, and why it isn't the default

Groq hosts [Orpheus](https://console.groq.com/playground?model=canopylabs/orpheus-v1-english) (open source, Apache-2.0) on the same free key. It was tried as the faster option and it isn't. On the same short reply:

| | Orpheus | Edge |
|---|---|---|
| synth | 954 ms | 1560 ms |
| speech | 4.96 s | 3.53 s |
| **total** | **5.91 s** | **5.09 s** |

It starts talking ~600 ms sooner, then takes 1.43 s longer to finish — so Edge wins by about 800 ms a reply. It reads at 9.5 chars/sec against Edge's 13.3, and it can't be pushed: `speed` returns HTTP 200 and is silently ignored (0.5, 1.5 and the default give byte-identical audio). The rate knob is what made Edge quick, and Orpheus has no equivalent.

If you want it anyway, set `BOOGEYMAN_TTS=groq`. Two gotchas: the model's terms must be accepted once (free) in the Groq playground or every request is a 400, and the free tier allows 100 requests per 4-hour window while each reply spends 2–4 of them. The voices are `autumn diana hannah austin daniel troy` — **not** the open-source model card's list (`tara`, `leo`, `zac`…), which 400s.

### Hearing Pidgin

Whisper leans hard on context, and out of the box it hears "I dey here" as "ID here". It's seeded with the words this boss actually uses — chale, abeg, dey, wahala, Accra, devil mode — which fixes most of that for free. Override with `GROQ_STT_PROMPT` if your vocabulary differs.

---

## Eyes and study mode

Needs `mss` and `pillow` (both in `requirements.txt`) and a brain that can see. Anthropic can. Groq's *text* models are blind, so a look quietly routes to a vision model instead (`GROQ_VISION_MODEL`, default `meta-llama/llama-4-scout-17b-16e-instruct`) rather than riding the usual free chain.

Screenshots go out as JPEG q92 at 1568px, which is where Anthropic downscales anyway. That was measured against the obvious objection to JPEG: asked to transcribe 11-pixel footer text and a random serial number off a rendered quiz, the model read both exactly from q85 up. PNG was 882 KB against JPEG's 289 KB for the same screen, and took 44 ms to encode against 5 ms, so lossless was buying nothing but upload time.

### What study mode actually does

A **look** costs a model call. A **sense** — the screen crushed to a 48×48 grey grid — costs about 50 ms and no money, almost all of it the screen grab itself; the downsample is 2 ms. So the watcher only ever senses, a few times a second, and spends a look when the picture has moved and then gone still. Three things keep it cheap and quiet:

- Every answer is deduplicated on the question itself, so clicking an option, moving the mouse or a timer ticking down re-reads nothing, and he never says the same answer twice.
- His own captions are on the screen he's watching, so answering would otherwise trigger a look at his own words. He holds still while speaking and re-baselines afterwards.
- A failed look is retried, and a page mid-draw is never read half-finished.

### Tuning it

The scale is much finer than it looks, and the case that sets it is the quiet one: a quiz app redrawing the same layout with a new question in it moves the grid by only about **0.0016**, because text is thin and averaging a block of it mostly returns background either way. A blinking caret moves it 0.00003. So:

```bash
BOOGEYMAN_STUDY_MOVED=0.0005     # 8x the worst noise, a third of the weakest real signal
BOOGEYMAN_STUDY_POLL=0.3         # seconds between senses
BOOGEYMAN_STUDY_SETTLE=2         # senses it must hold still before he reads
BOOGEYMAN_STUDY_COOLDOWN=1.2     # floor between looks
BOOGEYMAN_STUDY_SWEEP=30         # he looks anyway this often
BOOGEYMAN_SCREEN=0               # which monitor; 0 is the primary, never the union of all of them
```

**Don't raise `MOVED` to something that "looks safe".** It was first guessed at 0.010, and the result was a Boogeyman that answered question one and then silently never noticed question two — the one failure mode that looks exactly like success. There's a test pinning it inside the measured band for that reason.

`SWEEP` is the safety net for the other half of that problem: two questions can happen to carry the same amount of text, and that swap hides under `MOVED` however it's set. He looks anyway every 30 s, and the dedup means a sweep costs a look, never a repeated answer.

---

## Make him yours

- **Personality:** edit [`brain/persona.md`](brain/persona.md) (tone, languages, rules). It applies on the next reply, with no restart.
- **About you:** copy `brain/boss.example.md` to `brain/boss.md` (gitignored) and write about yourself: name, work, what you care about.
- **Memory:** stored in `~/.local/share/boogeyman/memory.json` (facts plus recent conversation). Delete the file to wipe it.
- **Workspace:** the only folder he can write to. `BOOGEYMAN_WORKSPACE`, default `~/Boogeyman`.
- **Backgrounds:** put `custom.mp4` or `custom.webm` in `app/public/bg/` and it joins the list.

---

## Troubleshooting

Start with the **terminal running `make dev`**. Every turn logs `heard:`, `command:`, `tool:` and `reply:` lines, background videos log `[bg]` lines, and startup prints which voice, ears and brain he settled on.

| Problem | Fix |
|---------|-----|
| VOICE OFFLINE | Check a mic is connected. To pick a specific one, run `brain/.venv/bin/python -m sounddevice` and set `BOOGEYMAN_MIC` to its name or index. For the ears you need `GROQ_API_KEY` or `ELEVENLABS_API_KEY`; the voice itself needs no key. |
| He only says canned lines | No brain key. Set `GROQ_API_KEY` (or another provider) in `.env` and restart. |
| "I hit the free limit…" | Every free Groq model is rate-limited. Wait the time he says, or lower `BOOGEYMAN_HISTORY` so each reply uses fewer tokens. |
| He talks too slowly | Raise `EDGE_RATE`, but read [How he sounds](#how-he-sounds) first — moving `EDGE_VOICE` to a naturally quicker reader is worth more than cranking a slow one. |
| **He doesn't stop when you talk** | The echo gate learned too high. It's capped at `BOOGEYMAN_BARGE_CEILING=0.18` for exactly this reason: speech into a laptop mic is about 0.10 rms and a clearly raised voice about 0.20, so a gate above 0.20 can't be cleared by a human and the feature is silently dead. Very quiet speech still won't interrupt. |
| He cuts himself off while talking | The opposite problem: his own voice from the speakers sounds like you. Use headphones, set `BOOGEYMAN_BARGE_LEVEL=1.5`, or `BOOGEYMAN_BARGE_IN=0`. |
| He says there's no study mode | Old replies in his history. Say "clear our conversation" once. |
| Study mode answers one question then goes quiet | Almost always `BOOGEYMAN_STUDY_MOVED` set too high; see [Tuning it](#tuning-it). Check the log for `study look failed` too — a spent vision quota looks the same from outside. |
| "I can't see your screen" | `pip install mss pillow`, or run `make setup` again. On Linux a locked or Wayland-restricted session can refuse the grab outright. |
| He looks at the screen but reads the wrong half | Multi-monitor. `BOOGEYMAN_SCREEN=0` is the primary; `1`, `2`… are the individual screens as `python -c "import mss;print(mss.MSS().monitors)"` lists them. |
| Orpheus returns 400 | Its terms haven't been accepted — open the model once in the Groq playground (free). If the error says `invalid_voice`, you're using a model-card voice name; use `austin`, `daniel` or `troy`. |
| He refuses to delete a file | By design: say yes to the question he asks. Deleted files go to `.trash` inside the workspace, so nothing is actually gone. |
| Background video black / frozen / not playing | Look for `[bg]` lines. Install `gstreamer1.0-plugins-good gstreamer1.0-libav` and restart. A video that won't play falls back to the nebula, and a notice appears on screen. |
| Blank or white window | Try `WEBKIT_DISABLE_DMABUF_RENDERER=1 make dev` (common with NVIDIA drivers). |
| `make dev` can't find the brain | Run `make setup` again. Rust looks for `brain/.venv/bin/python` (`Scripts/python.exe` on Windows); override it with `BOOGEYMAN_PYTHON=/path/to/python`. |
| Research finds nothing | It uses DuckDuckGo via `ddgs`, so you need internet. Run `make setup` to make sure `ddgs` is installed. |

---

## How it's built

Three languages, one app:

| Layer | Folder | Language | Job |
|-------|--------|----------|-----|
| Face | `app/src` | React + TypeScript + three.js | The HUD: core, curved screens, workspace, devil mode |
| Body | `app/src-tauri` | Rust (Tauri v2) | Window, OS integration, starts and talks to the brain |
| Brain | `brain/` | Python (FastAPI) | Voice loop, AI, memory, commands, research, hands, eyes |

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
  main.py             FastAPI app, the 16 tools the AI can call, UI actions
  voice.py            mic → speech-to-text → think → speech (with barge-in)
  reply.py            the AI: providers, tool loop, Groq fallbacks, vision
  elevenlabs.py       the paid voice and ears
  freevoice.py        the free stack: Edge TTS, Orpheus TTS, Groq Whisper
  commands.py         instant voice commands (no AI needed)
  hands.py            apps, workspace files, typing — with the limits enforced here
  eyes.py             screen capture: the cheap fingerprint and the expensive look
  study.py            the screen watcher behind study mode
  research.py         plan → search → read → analyze
  memory.py           facts + recent conversation
  persona.md          his personality
.env.example          every setting and key, documented (copy to .env)
shared/messages.md    message protocol
docs/OVERVIEW.md      architecture and decisions
tools/                asset generators (the nebula background)
```

A note on `hands.py` and `eyes.py`: he hears you through a microphone and decides with a language model, so both of those can be wrong. The limits that matter — one writable folder, two-step deletes, protected apps, no shell — are enforced in the Python, not asked for in the prompt, and pinned by tests. Devil mode changes his mood and nothing else.

---

## Development

```bash
make test     # Python tests (163), Rust clippy, TypeScript build
make build    # release build (.deb + AppImage); bundling the Python brain is still to come
make clean    # remove build output and the venv
```

- **Workflow:** issue → branch → PR → merge. The roadmap lives in GitHub issues: Skeleton → Agent loop → Voice → Hands → Presence → Memory → Crazy stuff.
- **Env overrides for Rust:** `BOOGEYMAN_BRAIN_DIR` (where `brain/` is) and `BOOGEYMAN_PYTHON` (which Python to run).
- **Adding a voice command:** add a regex in `brain/commands.py`, handle the action in `ui_action()` in `brain/main.py`, and push a UI event the React app listens for in `App.tsx`. To let the AI trigger it too, register a tool with `replier.add_tool(...)` in `main.py`.
- **New message types:** document them in `shared/messages.md`.
- **Measure before you tune.** Most of the numbers in this README replaced a guess that was wrong, and three ideas that sounded obviously good — connection pooling for Edge, Orpheus for speed, a smaller screenshot — were built, measured and thrown away. The docstrings in `freevoice.py`, `eyes.py` and `study.py` carry the tables, so the next person doesn't have to repeat them.
