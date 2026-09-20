# Product

<!-- impeccable:product-schema 1 -->

<!-- Written from confirmed repo truth (CLAUDE.md, README.md, DEMO_SCRIPT.md,
JUDGE_QA.md, FINDINGS.md, orbit/, viz/) plus the user's brief for the dashboard
redesign. The user delegated the ask round explicitly ("your call on pretty much
everything"), so no interview was run; every line below traces to a repo file or
that brief. Lines marked (inferred) are the exception and need confirming. -->

## Platform

web

## Stack

Existing: a single-file dashboard, `viz/static/index.html` — hand-written
HTML/CSS/vanilla JS, no build step, no framework, no npm. Served by FastAPI
(`viz/server.py`) which pushes a full state snapshot over a WebSocket at 10 Hz
and mirrors every control as a REST endpoint. Must keep working with zero
network access (`tools/offline_check.py`): no CDN fonts, no CDN scripts, no
remote images.

## Users

Two audiences, one screen, and they arrive in opposite ways.

1. **The operator** (Ritvik or a teammate) drives the demo live from a laptop
   during a three-minute judging slot. They know the system cold, and they need
   to change scenario, pause, step, and re-run without hunting.
2. **The judge / passer-by** stands about two metres away, has never seen the
   project, gets no narration if the operator is mid-sentence, and leaves in
   under a minute. They have no space-systems background. They must work out
   what this is, what is happening right now, and why it is better than the
   obvious alternative — without anyone explaining it.

The redesign exists because audience 2 currently fails: the owner's own report
is that the app "looked like a mess" until an AI explained it.

## Product Purpose

Orbit shows a satellite deciding, onboard, which captured images are worth
sending home — and a ground station arbitrating which satellite gets the next
transmission slot. Three ESP32-S3 boards stand in for three Earth-observation
satellites, sharing one ground station over a UDP multicast bus; a laptop
watches and is never on the control path.

Success is a live, running demonstration that a first-time viewer understands
unaided, and that survives hostile technical questioning without a single
unmeasured number being presented as measured. HackMIT 2026, Sustainability
track, 19–20 September 2026.

## Positioning

Satellites capture far more imagery than they can transmit. Downlink only
happens during a ground-station pass — a few short windows a day, set by line of
sight. Most captured frames are worthless (cloud, blur, nothing changed) but the
satellite cannot tell, so the scarce window is spent on whatever is next in the
buffer.

Orbit scores every frame *where it was captured*, onboard, in one streaming
pass, and spends the window best-first. The defensible claim is **usable frames
delivered per byte of contact window** — `run_end` in every run file, Orbit's
priority queue against an unfiltered FIFO baseline given the same byte budget,
where "usable" is cloud fraction ≤ 0.35 and never the score that did the
ranking. **The project explicitly does not claim to beat a datacentre GPU on
throughput and must never invite that comparison.**

## Operating Context

- **The judging slot**: three minutes, a laptop screen or a projector, judges at
  ~2 m. Large clear numbers beat decoration. The demo must run fully offline.
- **The run**: the simulation advances in *passes* (orbits). Each pass has an
  ingest phase (every satellite captures and scores 12 frames) and a contact
  window (the ground grants transmission slots one at a time until the window's
  byte budget is spent — 3 slots per pass at the demo's scaled pacing). Default
  4 passes; `filtered_vs_fifo` runs 8.
- **Five named scenarios** the operator switches between live: `nominal`,
  `lead_change` (the demo default), `starvation`, `filtered_vs_fifo`, `scaling`.
- **Two policies run side by side on the same frames and the same window**: the
  scored priority queue, and an unfiltered FIFO baseline computed on the ground.
  The gap between them is the pitch.
- **Fallbacks are part of the scene**: a satellite can be a real ESP32-S3 on
  the multicast bus or a simulated `orbit sat` running the same scoring kernel.
  The display must say which, per satellite, at all times — a board may die on
  stage and the demo continues, because nothing about arbitration depends on
  who is real.
- Energy-bench results land in `results/` and may be absent; the display reads
  them live and must show `TBD` rather than an estimate dressed as a
  measurement.

## Capabilities and Constraints

- Every number on screen arrives on the WebSocket. The browser computes nothing
  the server does not send, except display arithmetic (score/65535×100, ratios).
- Scores are `u16`; the UI shows them as 0–100.
- The corpus is **real NASA GIBS MODIS Terra imagery**, 194 frames, 14 scenes ×
  16 dates in 2024, 128×128 grayscale, committed to the repo. `synthetic: false`.
  Thumbnails are served from `/corpus/<id>.png`.
- The contact window is **modelled, not performed**: pixels never cross the UART.
  The link carries frame rows in, scores/grants/accounting out. Window capacity
  is enforced by byte accounting. The demo window is **scaled down** (3 frames
  per pass instead of 45,776) and must be labelled as scaled wherever shown.
- **Cloud does not block the radio link.** Cloud degrades the captured image and
  therefore lowers its score. What gates transmission is line of sight — the
  pass. The UI must keep these two separate; conflating them is a known failure
  the team fixed in the pitch and must not reintroduce.
- Terminology is settled: **orchestrator / edge node**, never master/slave.
- The starvation guard: after N consecutive wins by one satellite, the ground
  forces a switch. N is a live knob, default 3, and its value is an open
  parameter the team wants visible.
- Unresolved and must not be invented: camera hardware, live capture vs replay,
  contact-window duration in the real system.

## Brand Commitments

- The name is **Orbit**. Lowercase-ish, plain, no logotype exists.
- No existing visual identity worth preserving: the current dashboard is a
  utility layout the owner has asked to replace outright.
- Voice: plain, exact, unhyped. The repo's own writing style — short declarative
  sentences, no marketing adjectives, a measurement or a `TBD`, never a vibe.

## Evidence on Hand

- `corpus/png/*.png` — 194 real MODIS thumbnails, live in the UI today.
- `corpus/manifest.json` — provenance and NASA's usage statement verbatim.
- `corpus/gate_result.json` — the measured pacing decision (12 frames per pass,
  3 slots per pass, 10 s window).
- `results/bench.{md,jsonl}` — the identical scoring kernel timed on the GX10
  CPU and GPU over the same 194 MODIS frames, every figure carrying `method`,
  `scope` and `measured`.
- `docs/event_stream.md` — the contract between the ground station and the
  display; `docs/security.md` — how keys are provisioned.
- **Absent, must not be fabricated**: CPU and whole-board power on the GX10
  (only GPU-die power is instrumented, via NVML; the rest needs an inline USB-C
  PD meter), and per-frame energy on the satellite itself (no board is
  instrumented). These read `unavailable`, with the reason, and that is correct
  behaviour.

## Product Principles

1. **Never invent a number.** Every performance, power or resource figure comes
   from a measurement, a recorded benchmark run, or a cited source. Otherwise
   it says `TBD` or `unavailable`, with the reason.
2. **Label what is modelled, simulated or scaled** — in the interface, at the
   place the number appears, not in a footnote or a README.
3. **Legible at two metres.** The one thing a stranger must take away gets the
   largest type on the screen; everything else is subordinate.
4. **Plain words over domain jargon.** A viewer with no space background should
   not need a glossary. The precise term may follow the plain one, never
   replace it.
5. **Boring and debuggable beats clever.** This gets fixed at 3 AM by someone
   who did not write it, on a venue network that may not exist.

## Accessibility & Inclusion

- Must be readable at ~2 m on an unknown laptop screen or projector, which means
  real type sizes and contrast, not hairlines. Target WCAG AA contrast for every
  text/background pair, including the muted secondary text the current build
  leans on.
- Colour must never be the only carrier of meaning: satellites and policies are
  distinguished by label and position as well as hue, so the display survives
  colour-vision deficiency and a badly calibrated projector.
- Keyboard operation for the live controls (inferred, not requested): the
  operator drives from a laptop mid-sentence and cannot hunt for a mouse target.
