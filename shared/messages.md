# Message protocol

Every message between React, Rust and Python has the same JSON shape:

```json
{ "id": "uuid", "type": "kind", "payload": {} }
```

- `id`: correlates a reply with its request. A reply reuses the request's `id`. Rust generates one if it's missing.
- `type`: what the message is. Use dotted names: `user.message`, `brain.reply`, `agent.delta`.
- `payload`: anything, as long as it's a JSON object.

## Flow

```
React --invoke('send_to_brain')--> Rust --WebSocket--> Python
React <--------- reply ----------- Rust <--WebSocket-- Python
```

Messages from Python that don't answer a pending request (e.g. streamed tokens later) are
forwarded to React as the Tauri event `brain-message`. Connection changes arrive as the
`brain-status` event: `{ "connected": true | false }`.

## Types (Phase 0)

| type           | direction       | payload                                     |
|----------------|-----------------|---------------------------------------------|
| `ping`         | → brain         | `{}`                                        |
| `pong`         | ← brain         | `{}`                                        |
| `user.message` | → brain         | `{ "text": string }`                        |
| `brain.reply`  | ← brain         | `{ "text", "chars", "python", "at" }`       |
| `error`        | ← brain         | `{ "message": string }`                     |

## Voice

Requests (React → brain), each answered with `voice.ack`:

| type          | payload               | does                                             |
|---------------|-----------------------|--------------------------------------------------|
| `voice.greet` | `{}`                  | Speaks the greeting once; returns `capabilities` |
| `voice.mute`  | `{ "muted": bool }`   | Mutes / unmutes the mic                          |
| `voice.status`| `{}`                  | Returns `muted` and `capabilities`               |

Events (brain → React, empty `id`, arrive as the `brain-message` Tauri event):

| type               | payload                                                                                   |
|--------------------|-------------------------------------------------------------------------------------------|
| `voice.state`      | `{ "state": idle/listening/hearing/transcribing/thinking/speaking/muted/error, "detail"? }` |
| `voice.level`      | `{ "source": "mic" / "voice", "level": 0..1 }` (~20/s while active)                       |
| `voice.transcript` | `{ "text" }`: what the boss said                                                          |
| `voice.reply`      | `{ "text" }`: what he's saying back                                                       |
| `voice.spectrum`   | `{ "source": "mic" / "voice", "bands": [24 × 0..1] }`: log-spaced 80 Hz–8 kHz             |
| `ui.mode`          | `{ "mode": "work" / "normal" }`                                                           |
| `ui.devil`         | `{ "on": true / false }` — devil mode                                                     |
| `ui.help`          | `{ "show": true }` — open the commands card                                               |
| `ui.background`    | `{ "cmd": "next" / "sea" / "nebula" / "off" / "on" }`                                     |
| `task.*`           | research progress: start, step, queries, source, reading, metric, finding, done (see `brain/research.py`) |
| `app.shutdown`     | `{}`: he has said goodbye; the UI powers down and quits                                    |
| `memory.updated`   | `{ "facts": [{id, text, at}], "turns" }`                                                  |
| `study.answer`     | `{ "question", "answer", "why" }`: study mode read a question off the screen             |

Memory requests: `memory.get` → `memory.updated`; `memory.forget {id}` → `memory.updated`.

Phase 1 adds `agent.delta`, `agent.tool_call`, `agent.done`, `agent.error`.
