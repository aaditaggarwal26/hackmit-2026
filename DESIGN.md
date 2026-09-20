---
name: Orbit
description: A live test-report sheet for satellites deciding which images go home first.
colors:
  stock: "#edf0f3"
  plate: "#ffffff"
  plate-sunk: "#f4f6f8"
  ink: "#11161b"
  ink-2: "#4e5a66"
  rule: "#c9d2da"
  rule-soft: "#e2e8ed"
  blue: "#1b54c8"
  blue-wash: "rgba(27, 84, 200, .10)"
  blue-soft: "rgba(27, 84, 200, .16)"
  pencil: "#6e7a85"
  red: "#c0271f"
  red-wash: "rgba(192, 39, 31, .10)"
  green: "#15704a"
  amber: "#8a5a00"
  amber-wash: "rgba(138, 90, 0, .10)"
typography:
  display:
    fontFamily: "Archivo, -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
    fontSize: "clamp(34px, 3.6vw, 54px)"
    fontWeight: 800
    lineHeight: "0.88"
    letterSpacing: "-.035em"
    fontVariation: "'wdth' 108"
    fontFeature: "proportional-nums"
  headline:
    fontFamily: "{typography.display.fontFamily}"
    fontSize: "clamp(24px, 2.3vw, 31px)"
    fontWeight: 800
    lineHeight: "1"
    letterSpacing: "-.025em"
    fontVariation: "'wdth' 106"
    fontFeature: "proportional-nums"
  figure:
    fontFamily: "{typography.display.fontFamily}"
    fontSize: "clamp(23px, 2.3vw, 31px)"
    fontWeight: 800
    lineHeight: "1"
    letterSpacing: "-.025em"
    fontVariation: "'wdth' 106"
    fontFeature: "proportional-nums"
  title:
    fontFamily: "{typography.display.fontFamily}"
    fontSize: "24px"
    fontWeight: 800
    lineHeight: "1"
    letterSpacing: "-.012em"
    fontVariation: "'wdth' 112"
  standfirst:
    fontFamily: "{typography.display.fontFamily}"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: "1.45"
    letterSpacing: "-.005em"
  score:
    fontFamily: "{typography.display.fontFamily}"
    fontSize: "20px"
    fontWeight: 700
    lineHeight: "1.45"
    letterSpacing: "-.02em"
  body:
    fontFamily: "{typography.display.fontFamily}"
    fontSize: "14.5px"
    fontWeight: 400
    lineHeight: "1.45"
    letterSpacing: "normal"
    fontFeature: "tabular-nums"
  lede:
    fontFamily: "{typography.display.fontFamily}"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: "1.26"
    letterSpacing: "normal"
  control:
    fontFamily: "{typography.display.fontFamily}"
    fontSize: "13px"
    fontWeight: 600
    lineHeight: "1.45"
    letterSpacing: "normal"
  caption:
    fontFamily: "{typography.display.fontFamily}"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: "1.35"
    letterSpacing: "normal"
  plate-heading:
    fontFamily: "{typography.display.fontFamily}"
    fontSize: "11px"
    fontWeight: 700
    lineHeight: "1.45"
    letterSpacing: ".11em"
  label:
    fontFamily: "{typography.display.fontFamily}"
    fontSize: "10px"
    fontWeight: 700
    lineHeight: "1.45"
    letterSpacing: ".1em"
  log:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "11px"
    fontWeight: 400
    lineHeight: "1.5"
    letterSpacing: "normal"
rounded:
  hair: "1px"
  inner: "2px"
  surface: "3px"
  pill: "5px"
  round: "50%"
spacing:
  hair: "2px"
  tight: "3px"
  xs: "5px"
  sm: "7px"
  md: "8px"
  gap: "12px"
  pad: "15px"
  page: "16px"
components:
  plate:
    backgroundColor: "{colors.plate}"
    textColor: "{colors.ink}"
    rounded: "{rounded.surface}"
    padding: "{spacing.pad}"
  button-transport:
    backgroundColor: "{colors.plate-sunk}"
    textColor: "{colors.ink}"
    typography: "{typography.control}"
    rounded: "{rounded.surface}"
    padding: "5px 11px"
  button-transport-hover:
    backgroundColor: "{colors.stock}"
    textColor: "{colors.ink}"
  button-transport-pressed:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.plate}"
  button-sheet:
    backgroundColor: "transparent"
    textColor: "{colors.ink-2}"
    rounded: "{rounded.surface}"
    padding: "5px 11px"
  button-sheet-selected:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.plate}"
  input-field:
    backgroundColor: "{colors.plate-sunk}"
    textColor: "{colors.ink}"
    typography: "{typography.control}"
    rounded: "{rounded.surface}"
    padding: "5px 7px"
  chip-measured:
    backgroundColor: "transparent"
    textColor: "{colors.green}"
    typography: "{typography.label}"
    rounded: "{rounded.inner}"
    padding: "2px 6px"
  chip-modelled:
    backgroundColor: "transparent"
    textColor: "{colors.amber}"
    typography: "{typography.label}"
    rounded: "{rounded.inner}"
    padding: "2px 6px"
  chip-fault:
    backgroundColor: "{colors.red-wash}"
    textColor: "{colors.red}"
    typography: "{typography.label}"
    rounded: "{rounded.inner}"
    padding: "2px 6px"
  chip-sending:
    backgroundColor: "{colors.red}"
    textColor: "{colors.plate}"
    typography: "{typography.label}"
    rounded: "{rounded.inner}"
    padding: "2px 6px"
  phase-badge:
    backgroundColor: "{colors.plate-sunk}"
    textColor: "{colors.ink}"
    typography: "{typography.control}"
    rounded: "{rounded.surface}"
    padding: "4px 10px 4px 8px"
  phase-badge-live:
    backgroundColor: "{colors.red-wash}"
    textColor: "{colors.red}"
  tooltip:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.plate}"
    typography: "{typography.caption}"
    rounded: "{rounded.surface}"
    padding: "6px 9px"
---

# Design System: Orbit

## Overview

**Creative North Star: "The Test Report, Written Live"**

Orbit is a semiconductor datasheet that happens to be updating ten times a
second. Its surface is cool bond stock with white plates laid on it, lifted by a
single whisper of ambient shadow and separated by hairline rules. Everything a
console would do to look technical — neon on black, gradients, glow, gauges,
hover lift — is refused. What makes it read as instrumentation instead is the
discipline of the numbers: every quantity on the sheet carries a plain-English
name, a unit, and a provenance tag saying how we know it, and anything nobody
has measured says `not measured yet` in the same breath rather than going quiet.

The palette is five pens and nothing else. Blue is Orbit's own result, pencil
grey is the baseline it beats, red is live-right-now or an intervention the
ground forced, green is measured on real hardware, amber is modelled or not yet
measured. A colour never means "satellite 3"; satellites are numbered lanes and
positions, so the display carries twelve of them without inventing a twelfth
hue. Archivo carries the whole page alone — the width axis for plate headings
and large figures, weight and size for everything else — with one deliberate
exception: the raw device log on sheet 2 is set in a system monospace stack,
because a wrapped log of fixed-width fields is less legible in a proportional
face than the divergence from Archivo-alone costs.

Density is high but no longer bought at the cost of legibility. Sheet 1 fits one
screen above 1180×759 and every supporting type step sits a notch above the
minimum that would fit, because the audience reads it from two metres. Sheets 2
and 3 gave up fixed-height instrument framing entirely and became scrolling
reference spreads, where a row that needs more height simply takes it.

**Key Characteristics:**
- Cool bond stock, white plates, hairline rules; one ambient shadow and no second step.
- Five pens with one job each; satellites are numbered lanes, never hues.
- Archivo alone, width axis reserved for plate headings and large figures.
- Six literal type steps (10, 11, 12, 13, 14.5, 20px) plus five responsive `:root` tokens.
- Every number carries a name, a unit, and a provenance tag.
- Sheet 1 is one fixed screen; sheets 2 and 3 scroll.

## Colors

A cool, near-neutral paper with five saturated inks, each rationed to a single
meaning and each also carried by a second, non-colour channel.

### Primary
- **Orbit Blue** (`{colors.blue}`): the system's own result, everywhere it
  appears — the gain figure, the filtered curve in Fig 1, the delivered curve in
  Fig 4, won slots in the ledger, used capacity in the contact-window meter,
  filled score bars, the top row of each priority queue, and the sweep progress
  track. Nothing that is not Orbit's result is blue.
- **Blue Wash** (`{colors.blue-wash}`) and **Blue Soft** (`{colors.blue-soft}`):
  the same ink at 10% and 16%. The wash is the shaded gap between the two curves
  — the quantity the figure exists to show. The soft tint fills secondary bars
  (score components, non-leading queue rows) and the text selection highlight.

### Secondary
- **Pencil Grey** (`{colors.pencil}`): the counterfactual baseline — first-in
  first-out, no scoring. It is the ink of the line Orbit beats, and it is always
  dashed as well as grey. Also the idle lamp in the pass badge.

### Tertiary
- **Live Red** (`{colors.red}`): two meanings that are one meaning — this is
  happening right now, or the ground forced it. The breathing lamp on a live
  pass, the ring on the current slot, the forced-switch cell in the ledger, the
  `Sending now` chip, a fault chip, a failed timing tag, and the lost-link
  banner.
- **Measured Green** (`{colors.green}`): a number that came off real hardware —
  a counter on the board, a stopwatch on the link, an instrumented power rail.
  It appears
  only as a tag or a provenance chip, never as a fill or a curve.
- **Modelled Amber** (`{colors.amber}`): a number that came from a model, a
  simulation, a scaling estimate, or does not exist yet. The dashed vertical rule
  marking where the contact window fills on sheets 3's two figures, the
  highlighted row in a sweep table (`{colors.amber-wash}`), the `sim` chip on a
  simulated satellite, and the literal `not measured yet` tag.

### Neutral
- **Bond Stock** (`{colors.stock}`): the page itself; the cool grey the plates
  sit on.
- **Plate** (`{colors.plate}`): the white of every panel, and the stroke colour
  behind direct chart labels and end dots.
- **Sunk Plate** (`{colors.plate-sunk}`): the recessed well — empty track, empty
  image slot, control field, badge ground.
- **Ink** (`{colors.ink}`) / **Second Ink** (`{colors.ink-2}`): primary text and
  supporting text. `ink` is also the ground of a selected tab and of the tooltip.
- **Rule** (`{colors.rule}`) / **Soft Rule** (`{colors.rule-soft}`): the two
  hairlines. `rule` divides sections and heads tables; `rule-soft` is the plate
  border, the row divider, and the unfilled portion of any track.

A dark scheme exists with a parallel set of the same seventeen tokens (honoring
`prefers-color-scheme` and an explicit `data-theme` override). The roles do not
change; only the values do.

### Named Rules

**The Five Pens Rule.** Blue, pencil, red, green, amber. Each has exactly one
job and no ink may be borrowed for a second meaning. If a new distinction needs
a colour, it does not get one — it gets a position, a dash pattern, a ring, or a
word.

**The Numbered Lane Rule.** A satellite is a numbered lane and a fixed row
position, never a hue. Twelve satellites must look the same as two.

**The Never Colour Alone Rule.** Every distinction carried by colour is carried
by a second channel too: the baseline curve is dashed as well as pencil, the
live slot is ringed as well as red, the window-fill rule is dashed as well as
amber, a provenance tag carries the literal word as well as the ink.

**The Ink-Only Rule.** A colour value is always an ink. The three washes
(`blue-wash`, `red-wash`, `amber-wash`, 10% of their pen) are the only fills the
palette permits, and they only ever tint an area that already belongs to that
pen's meaning. No gradient, no glow, no tinted surface for decoration.

## Typography

**Display Font:** Archivo (variable, wght 400–800, wdth 62–125), self-hosted
from `/static/fonts/archivo-latin.woff2`, falling back to the platform UI stack.
**Body Font:** Archivo — the same face carries everything.
**Label/Mono Font:** a system monospace stack (`ui-monospace, SFMono-Regular,
Menlo, Consolas`), used only for the raw device log.

**Character:** Archivo is a grotesque with a real width axis, which is what makes
a one-typeface page possible: headings get their authority from width and weight
rather than from a second face, and large figures can be squeezed or extended to
sit on the grid without a size change. Body text is tabular by default so live
digits do not shimmy as they update; the two big proportional figures opt out,
because at 34–54px tabular spacing looks gappy.

### Hierarchy
- **Display** (800, `--t-hero` clamp 34–54px, line-height .88, tracking -.035em,
  wdth 108, proportional figures): the gain number — the one thing a stranger
  reads at two metres. One per screen. Overridden to 42px under 860px tall and
  *up* to 46px under 720px wide, where the page scrolls and there is room.
- **Headline** (800, `--t-pass` clamp 24–31px, line-height 1, tracking -.025em,
  wdth 106, proportional figures): the pass number on the contact-window plate.
  Drops to clamp 22–27px under 800px tall.
- **Figure** (800, clamp 23–31px, wdth 106): the three-across statistics that
  lead sheets 2 and 3. Same voice as the headline at a smaller weight of
  attention.
- **Title** (800, `--t-title` 24px, wdth 112, tracking -.012em): the wordmark.
  21px under 720px wide.
- **Standfirst** (400, `--t-standfirst` 15px, tracking -.005em, second ink with
  the question set in ink at 600): the one sentence that states the problem,
  beside the wordmark. Collapses to the lede size under 720px.
- **Score** (700, 20px, tracking -.02em): the per-photo score under each of the
  two compared photographs, with its `out of 100` qualifier set at caption size.
- **Body** (400, 14.5px/1.45, tabular): the document default.
- **Lede** (400, `--t-lede` 13px/1.26, max 40ch): the sentence beside the gain
  figure that says what the gain means.
- **Control** (600, 13px): buttons, selects, number and range fields, table
  cells, score-bar rows, the pass-phase badge.
- **Caption** (400, 12px/1.3–1.35, second ink): teaching captions, chart axis
  text, key/legend rows, hints, metering figures, queue rows.
- **Plate heading** (700, 11px, tracking .11em, uppercase, second ink; the
  figure number set in ink at wdth 118): every plate's `h2`, and the satellite
  strip head. A `.thin` variant (500, 12px, sentence case) carries the trailing
  explanatory phrase in the same line.
- **Label** (700, 10px, tracking .1em, uppercase, second ink): rail group
  labels, conditions keys, the photo-column headers, table column heads.
  Provenance chips and tags use the same size at .07em.
- **Log** (400, 11px/1.5, monospace, second ink, `white-space: pre`): the device
  log only.

### Named Rules

**The Six Step Rule.** The literal ramp is six sizes — 10, 11, 12, 13, 14.5 and
20px — plus five `:root` tokens (`--t-hero`, `--t-pass`, `--t-title`,
`--t-standfirst`, `--t-lede`) for the roles that resize. That is the whole ramp.
A new size must be an existing step; if a value sits within half a pixel of a
step doing the same job, it *is* that step.

**The One Declaration Rule.** A role that changes size at a breakpoint owns a
`--t-` token and declares `font-size` exactly once. The media query overrides the
token on `:root`, never the component. This is why the hero can grow at the
narrow breakpoint and shrink at the short one without either rule knowing about
the other.

**The Caps Tier Rule.** 10 and 11px exist only as uppercase, letter-spaced type;
12 and 13px only as sentence case. A letter-spaced caps tier reads optically
larger than a sentence-case tier at the same pixel size, so the ramp's two
smallest steps are legible precisely because they are never set in sentence case.

**The Width-Axis Rule.** Hierarchy is carried by size and weight. The width axis
(106–118%) is reserved for plate headings, the wordmark, and large figures; every
other element stays at the default width. Width is a signal, not a texture.

**The Named Quantity Rule.** A number never appears alone. It carries a
plain-English name, a unit, and a provenance tag — `measured` in green with
where, `estimate`/`modelled` in amber with what the model assumed, or the literal
`not measured yet`. A bare figure reads as measured whether it is or not.

## Layout

The page is a full-height grid of three rows — title block, control rail with
the conditions line, then the active sheet — inset 11px top, 16px sides, 13px
bottom, with an 11px row gap (dropping to a flat 10px inset and an 8px gap under
720px). Plates carry 15px of padding and sit 12px apart (`--gap`). The small
rhythm inside a plate runs 2 / 3 / 5 / 7 / 8px; 22px is the one wide gutter,
between the three score-component meters.

**Sheet 1 (live run)** is a fixed three-column, three-row grid:
`1.5fr / minmax(244px, .8fr) / minmax(246px, .74fr)` across, and
`minmax(300px, 1fr) / auto / minmax(200px, .85fr)` down. Fig 1 and its gain sit
left, the two compared photographs centre, the pass and contact window right;
the slot ledger spans the full width beneath them; the satellite strips span the
full width below that. Plates clip their overflow, and a list that overruns fades
out under a mask gradient rather than being cut off square.

**Sheets 2 and 3 (hardware, scaling)** are two-column scrolling spreads with
`max-content` rows, aligned and packed to the start. They are reference
documents, not instruments: they scroll as a whole.

**Responsive behaviour.** Four height breakpoints and two width breakpoints, in
a stated order. At 900px tall the already-sent photo strip goes. At 800px the
compared-photos caption goes and the pass number steps down. At 860px the hero
drops to 42px and the per-satellite thumbnails shrink to 88px. Below 759px tall
the live sheet stops being fixed and the page scrolls, with every row becoming
content-sized. Below 1180px wide the whole page scrolls and the sheets restack in
source order; below 720px everything is a single column, the satellite strips
lose their thumbnail column, and the gain figure gets *bigger*.

**Satellite density.** Up to three satellites get full strips: thumbnail, score
bars, the three score components, the priority queue, and the already-sent haul.
Beyond three, the strips collapse to fixed 46px single-line rows inside their own
scroller, keeping only the identity, the provenance chips, and one queue line.

### Named Rules

**The One Screen Rule.** Above 1180px wide and 759px tall the live sheet fits the
viewport with no page scroll. Overflow is handled inside plates — clipped, with a
mask fade — never by growing the document.

**The Order of Sacrifice Rule.** When height runs short, things are dropped whole
in a fixed order, never shrunk: the figures row has a hard floor because a chart
and a pair of photographs cannot shrink gracefully, so the queue absorbs the
squeeze first, the already-sent photo strip is the first whole element dropped,
and the caption that teaches what a score is goes last.

**The Definite Scroller Rule.** Inside a scrolling sheet of definite height,
`auto` grid rows compress toward min-content and let plates overflow into each
other; those sheets use `max-content` rows. And a scrolling wrapper nested
inside an already-scrolling sheet hides rows behind a scrollbar nobody looks for,
so an inner `.scroll` inside sheets 2 and 3 is `overflow: visible`.

## Elevation & Depth

The system has one elevation step and no second. A plate is white on cool stock
with a hairline `rule-soft` border and a single two-layer ambient shadow — a 1px
contact shadow plus a wide, heavily-negative-spread ambient
(`0 1px 2px rgba(17,22,27,.05), 0 6px 18px -12px rgba(17,22,27,.30)`). Depth
beyond that is tonal: recessed things are `plate-sunk`, divisions are hairlines,
and the page never uses a shadow to say "on top of". Nothing lifts on hover — a
hover changes a border colour or a background tone, never a `translateY`.

### Shadow Vocabulary
- **Plate lift** (`--sh`): the only elevation token. Applied to plates, rail
  groups, and the lost-link banner. Nothing else takes a shadow for depth.
- **Live ring** (`box-shadow: 0 0 0 2px var(--plate), 0 0 0 3.5px var(--red)`):
  not elevation. A plate-coloured halo and a red hairline ring marking the slot
  being transmitted right now.
- **Now-marker ring** (`box-shadow: inset 0 0 0 2px var(--red)`): the same idea
  inside the contact-window meter, marking the cell currently being spent.

### Named Rules

**The One Elevation Rule.** There is one shadow token and it is ambient. No
second elevation step, no hover lift, no directional or hard-offset shadow.

**The Ring, Not Lift Rule.** Any `box-shadow` beyond the token is a hairline ring
denoting live state, drawn with zero blur and zero offset. If it has a y-offset
or a blur, it is elevation and it is not allowed.

## Shapes

Rectangles with a small, consistent softening, and a strict correspondence
between a fill and the well it sits in. Plates, rail groups, buttons, fields,
badges, tooltips, and the banner are 3px. Anything nested inside them — image
slots, meter cells, ledger cells, chips, tags, key swatches, tracks — is 2px, and
an image inside a 2px slot is 1px. The one exception is the per-satellite score
bar, whose 10px track and fill are 5px, a true pill at exactly half its height.
Fully round (50%) is reserved for status lamps and chart end dots.

Borders are always 1px and always a rule token; there is no 2px border anywhere.
Photographs are always square (`aspect-ratio: 1`, `object-fit: cover`) and
rendered with `image-rendering: pixelated`, because they are low-resolution
frames and pretending otherwise would be a lie about the data. Curves are 2px
with round caps and joins; a dashed curve is `7 5`, a dashed constraint rule is
`4 4`.

**The Matched Radius Rule.** A fill takes the radius of the well it sits in, one
step smaller if it is inset. Never round an inner element more than its
container.

## Components

### Plate
The unit of the whole page. A white panel with a hairline border, 3px corners,
15px padding, the one ambient shadow, and a column flex layout with `min-height:
0` so charts and lists inside it can actually shrink. Its `h2` is a 10/11px
uppercase letter-spaced heading in second ink, with the figure or table number
(`FIG 1`, `TABLE 3`) picked out in ink at wdth 118 and an optional sentence-case
`.thin` tail carrying the explanatory phrase. A plate never has a hover state.

### Buttons
- **Shape:** 3px corners, 1px `rule-soft` border, no shadow.
- **Transport / default** (`.transport button`, `.btn`): sunk plate ground,
  ink text, 13px/600, 5px × 11px padding.
- **Hover:** border firms to `rule`, ground cools to the stock colour. No lift,
  no shadow change. 140ms on the standard ease.
- **Pressed / active** (`aria-pressed="true"`): inverts to solid ink with plate
  text.
- **Disabled:** 40% opacity, default cursor. No colour change.

### Navigation (sheets)
Three numbered tabs plus the theme toggle, right-aligned in the title block.
12px/600 uppercase at .085em tracking in second ink, transparent ground.
Hover fills with the plate white; the selected tab (`aria-selected="true"`)
inverts to ink with plate text. Each tab is titled with its keyboard shortcut.

### Chips (provenance)
A chip says what a *thing* is. 10px/700 uppercase at .07em, 2px corners, 2px × 6px
padding, `border: 1px solid currentColor` so the outline is always the chip's own
ink: `hw` green for a real board, `sim` amber for a simulated one, `plain` second
ink with a `rule` border for neutral state. Two chips break the outline pattern
because they are events, not classifications: `bad` (red on red wash, no border)
and `solid` (white on solid red, for `Sending now`).

### Tags (honesty vocabulary)
A tag says how a *number* is known. 10px/700 uppercase at .07em, no border, no
ground — pure ink inline in a sentence or a table cell: `m` green `measured`,
`e` amber `estimate` / `modelled` / `not measured yet`, `x` red for a failure.
A table row that needs marking as modelled gets an `amber-wash` ground and bold
cells rather than a coloured border.

### Inputs / Fields
Selects and number inputs take the same shape as a default button: sunk plate
ground, `rule-soft` border, 3px corners, 13px text, 5px × 7px padding. Number
inputs are 52px wide and centred; the range slider is 92px with `accent-color`
set to blue. Focus is global, not per-component: a 2px blue outline offset 2px,
with a 2px radius, on `:focus-visible` only.

### Cards / Containers
There are no cards. There are plates (see above) and rail groups — the same
material at a smaller scale: plate ground, `rule-soft` border, 3px corners, the
one shadow, 4px × 9px padding and a 36px minimum height, each opened by a 10px
uppercase label.

### Tables
Column heads are 10px uppercase labels in second ink over a `rule` hairline; body
cells are 13px with a `rule-soft` divider that the last row drops. Numeric cells
are right-aligned and 600 weight; unit and provenance cells fall back to second
ink. Table body cells never get a background except the amber wash marking a
modelled row.

### Slot ledger (signature)
The arbitration record. One row per satellite: a 74px right-aligned name in
second ink (10px when there are many lanes), then a flush row of equal-flex
cells, 2px gaps, 17px tall, 2px corners. An unwon slot is `rule-soft`; a slot
this satellite won is blue; a slot the ground forced through the fairness guard
is red; the slot being transmitted right now carries the live ring. A newly
landed cell animates in from `scaleY(.25)` over 450ms. The axis beneath repeats
the 74px gutter so its range labels line up with the cells.

### Contact-window meter
A 20px track of equal-flex cells representing the pass's capacity. Spent cells
fill blue, the cell being spent carries the inset red ring, unspent cells stay
`rule-soft`. Below it a caption row puts the spent and total figures at opposite
ends. It is a capacity ledger, not a progress bar: the cells are countable.

### Statistics row
Three large figures across (`repeat(3, minmax(0,1fr))`, 10px/24px gaps) leading
sheets 2 and 3: the figure at clamp 23–31px/800/wdth 106, and a 12px supporting
line under it in second ink with the load-bearing words picked out in ink. It
states what the sheet is about before the sheet argues it.

### Sweep run indicator
For a run that takes the better part of a minute: a 6px sunk track with a blue
fill driven by `scaleX()` over 400ms, and a status line beneath splitting the
current step and the elapsed/remaining figures to opposite ends. It exists so a
long computation looks alive without a spinner.

### Charts
Two families, one grammar. Fig 1 (live) and Figs 4–5 (scaling sweep, built from
the shared `sweepFrame` / `crossRule` / `line` / `dots` / `empty` helpers) both
draw two series in one unit on one axis, shade the gap between them in
`blue-wash`, run the baseline in dashed pencil and the Orbit series in solid
blue, mark the last point with a plate-stroked dot, and label both ends directly
rather than with a legend. Gridlines are `rule-soft`, the zero line is `rule`,
axis text is 12px second ink. Sheets 3's figures add one amber dashed vertical
rule marking where the contact window fills — the constraint binding — labelled
in place. An empty chart says what it is waiting for in a centred sentence; it
never renders an empty frame.

### Tooltips
Solid ink ground, plate text, 12px, 3px corners, 6px × 9px padding, translated
above the pointer, fading in over 120ms and never taking pointer events. It
carries an 8px rounded swatch of the series it describes. Everything in a tooltip
is also on the axis or in a direct label; it is a convenience, not a channel.

### Named Rules

**The Stroked Label Rule.** A direct label that sits over its own plot carries
`paint-order: stroke` with a 4px plate-coloured stroke, so it stays readable
wherever the curve happens to run under it. This applies to every direct label in
the system, not just the two it was first needed for.

**The Round Number Rule.** An axis label is a number a person would write down,
and the axis must not tower over the data: `niceScale` tries the round steps,
keeps the one that clears the peak with the least headroom, and labels every
k-th line so labels breathe. A fifth of the plot left empty flattens the very gap
the figure exists to show.

**The Built Once Rule.** The DOM is built once and updated in place — text,
attributes, and `scaleX()` — on the 10 Hz tick. A live region is never rebuilt
with `innerHTML`, and an image element is never created for a frame that does
not exist yet.

## Do's and Don'ts

### Do:
- **Do** give every pen exactly one job: blue = Orbit's result, pencil = the
  baseline, red = live or forced, green = measured on hardware, amber = modelled
  or not yet measured.
- **Do** identify satellites by numbered lane and row position, so the display
  scales to twelve without inventing hues.
- **Do** pair a provenance tag with every figure — `measured` (green) with where
  it was measured, `estimate` / `modelled` (amber) with what the model assumed,
  and the literal `not measured yet` where no number exists.
- **Do** carry any distinction in a second channel besides colour: the baseline
  is dashed as well as pencil, the live slot is ringed as well as red.
- **Do** pick the nearest existing step on the ramp — 10, 11, 12, 13, 14.5 or
  20px, or one of the five `--t-` tokens.
- **Do** give a role that resizes at a breakpoint a `:root` token and one
  `font-size` declaration, and override the token in the media query.
- **Do** keep 10 and 11px for uppercase letter-spaced type and 12 and 13px for
  sentence case; the caps tiers read optically larger than their number.
- **Do** keep the live sheet on one screen above 1180×759 — clip inside plates,
  with a mask fade — and let the page scroll only below 759px tall.
- **Do** drop whole secondary elements in the stated order when height runs
  short: the sent-photo strip first, the teaching caption last.
- **Do** give a scrolling sheet `max-content` rows, and make any inner `.scroll`
  inside it `overflow: visible`.
- **Do** give every direct chart label `paint-order: stroke` with a 4px
  plate-coloured stroke.
- **Do** write mask gradients with the `black` keyword, so an alpha stop is never
  mistaken for a colour token.
- **Do** set the SVG `viewBox` to the box's pixel size so one unit is one pixel.
- **Do** build the DOM once and update text, attributes and `scaleX()` in place
  on the 10 Hz tick.
- **Do** self-host every asset under `viz/static/`; `tools/offline_check.py`
  fails the build on any `https://` reference from `index.html`.
- **Do** set plate headings, the wordmark and large figures on the width axis
  (106–118%) and leave everything else at the default width.

### Don't:
- **Don't** invent a sixth pen, or reuse one of the five for a meaning it does
  not already carry.
- **Don't** give a satellite its own colour, ever.
- **Don't** print a number without a plain-English name, a unit, and a
  provenance tag — a bare figure reads as measured whether it is or not.
- **Don't** add a type step. If a size sits within half a pixel of an existing
  step doing the same job, it *is* that step.
- **Don't** put a `font-size` on a component inside a media query; override the
  role's `--t-` token instead.
- **Don't** add a second elevation step, a hover lift, or a directional or
  hard-offset shadow; there is one ambient shadow token and it applies to plates,
  rail groups and the banner.
- **Don't** use `box-shadow` for anything but that token and zero-offset,
  zero-blur live-state rings.
- **Don't** reach for a second typeface for display or UI text; Archivo and drawn
  SVG marks are the whole vocabulary, and the monospace stack is the log's alone.
- **Don't** use a glyph, emoji, or icon-font character as an icon.
- **Don't** solve a height squeeze by shrinking the gain figure, the chart, or
  the compared photographs — drop a secondary element instead.
- **Don't** nest a scroller inside a scrolling sheet, or leave `auto` rows in a
  definite-height one.
- **Don't** give an inner fill a different radius from the well it sits in.
- **Don't** rebuild a live region with `innerHTML` on the tick, or create image
  elements for frames that do not exist yet.
- **Don't** link a font, script, or image from a CDN.
- **Don't** round a corner past 3px (5px on the half-height bar track) or
  introduce a gradient, glow, or green-on-black telemetry styling; this is a test
  report, not a console.
