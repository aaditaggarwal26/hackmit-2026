---
name: Orbit
description: A live test-report sheet for an FPGA deciding which satellite images go home first.
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
  title:
    fontFamily: "{typography.display.fontFamily}"
    fontSize: "23px"
    fontWeight: 800
    lineHeight: "1"
    letterSpacing: "-.012em"
    fontVariation: "'wdth' 112"
  standfirst:
    fontFamily: "{typography.display.fontFamily}"
    fontSize: "14.5px"
    fontWeight: 400
    lineHeight: "1.4"
    letterSpacing: "-.005em"
  readout:
    fontFamily: "{typography.display.fontFamily}"
    fontSize: "19px"
    fontWeight: 700
    lineHeight: "1.2"
    letterSpacing: "-.02em"
  body:
    fontFamily: "{typography.display.fontFamily}"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: "1.4"
    fontFeature: "tabular-nums"
  lede:
    fontFamily: "{typography.display.fontFamily}"
    fontSize: "12.5px"
    fontWeight: 400
    lineHeight: "1.26"
  caption:
    fontFamily: "{typography.display.fontFamily}"
    fontSize: "11.5px"
    fontWeight: 400
    lineHeight: "1.35"
  small:
    fontFamily: "{typography.display.fontFamily}"
    fontSize: "11px"
    fontWeight: 400
    lineHeight: "1.25"
  label:
    fontFamily: "{typography.display.fontFamily}"
    fontSize: "10px"
    fontWeight: 700
    lineHeight: "1.2"
    letterSpacing: ".11em"
  micro:
    fontFamily: "{typography.display.fontFamily}"
    fontSize: "9.5px"
    fontWeight: 700
    lineHeight: "1.2"
    letterSpacing: ".1em"
  mono:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "11px"
    fontWeight: 400
    lineHeight: "1.5"
rounded:
  hair: "1px"
  chip: "2px"
  plate: "3px"
  pill: "5px"
spacing:
  hair: "2px"
  tight: "3px"
  xs: "4px"
  sm: "8px"
  grid: "9px"
  gap: "10px"
  pad: "12px"
  rail: "14px"
components:
  plate:
    backgroundColor: "{colors.plate}"
    textColor: "{colors.ink}"
    rounded: "{rounded.plate}"
    padding: "{spacing.pad}"
  plate-heading:
    textColor: "{colors.ink-2}"
    typography: "{typography.label}"
  control-group:
    backgroundColor: "{colors.plate}"
    textColor: "{colors.ink}"
    rounded: "{rounded.plate}"
    padding: "4px 9px"
    height: "36px"
  transport-button:
    backgroundColor: "{colors.plate-sunk}"
    textColor: "{colors.ink}"
    rounded: "{rounded.plate}"
    padding: "5px 11px"
    typography: "{typography.lede}"
  transport-button-hover:
    backgroundColor: "{colors.stock}"
    textColor: "{colors.ink}"
  transport-button-pressed:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.plate}"
  sheet-tab:
    backgroundColor: "transparent"
    textColor: "{colors.ink-2}"
    rounded: "{rounded.plate}"
    padding: "5px 11px"
    typography: "{typography.small}"
  sheet-tab-selected:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.plate}"
  chip-hw:
    backgroundColor: "transparent"
    textColor: "{colors.green}"
    rounded: "{rounded.chip}"
    padding: "2px 6px"
    typography: "{typography.micro}"
  chip-sim:
    backgroundColor: "transparent"
    textColor: "{colors.amber}"
    rounded: "{rounded.chip}"
    padding: "2px 6px"
    typography: "{typography.micro}"
  chip-bad:
    backgroundColor: "{colors.red-wash}"
    textColor: "{colors.red}"
    rounded: "{rounded.chip}"
    padding: "2px 6px"
    typography: "{typography.micro}"
  chip-plain:
    backgroundColor: "transparent"
    textColor: "{colors.ink-2}"
    rounded: "{rounded.chip}"
    padding: "2px 6px"
    typography: "{typography.micro}"
  chip-solid:
    backgroundColor: "{colors.red}"
    textColor: "{colors.plate}"
    rounded: "{rounded.chip}"
    padding: "2px 6px"
    typography: "{typography.micro}"
  tag-measured:
    textColor: "{colors.green}"
    typography: "{typography.micro}"
  tag-estimate:
    textColor: "{colors.amber}"
    typography: "{typography.micro}"
  tag-failed:
    textColor: "{colors.red}"
    typography: "{typography.micro}"
  score-bar-track:
    backgroundColor: "{colors.plate-sunk}"
    rounded: "{rounded.pill}"
    height: "10px"
  score-bar-fill:
    backgroundColor: "{colors.blue}"
    rounded: "{rounded.pill}"
    height: "10px"
  lane-cell:
    backgroundColor: "{colors.rule-soft}"
    rounded: "{rounded.chip}"
    height: "17px"
  lane-cell-won:
    backgroundColor: "{colors.blue}"
  lane-cell-forced:
    backgroundColor: "{colors.red}"
---

# Design System: Orbit

## Overview

**Creative North Star: "The Test Report, Written Live"**

Orbit is a semiconductor datasheet and test-report spread that happens to be
updating ten times a second. Every quantity on the sheet carries a plain-English
name, a unit, and how we know it; anything nobody has measured says so in the
same breath, in the same size type, without apology. The surface is cool bond
stock (#edf0f3) carrying white plates (#ffffff), hairline rules doing nearly all
the separation, and Archivo alone across the whole ramp — width axis for plate
headings and big figures, weight and size for everything else.

The density is high and deliberately unpadded: a 9–12px rhythm, 9.5–12.5px
supporting type, and figures that jump to 34–54px so a stranger two metres away
reads the gain before they read anything else. Nothing is decorative. There is no
hue that does not mean something, no icon, no gradient, no glass, no neon. The
build explicitly refuses the mission-control console with green-on-black
telemetry that every space demo ships, and the rounded-card dashboard it
replaced.

Elevation is one token and one token only (`--sh`), a two-stop ambient lift
carried by every plate and control group. It is diffuse, near-invisible in
isolation, and it exists to separate a white plate from a nearly-white stock
where a 1px rule alone read as a seam. It is not a card shadow: there is no hard
offset, no hover lift, and no second step. A future surface that wants depth
reuses this token or goes without.

**Key Characteristics:**
- Cool bond stock, white plates, hairline rules, 3px corners at the largest.
- Five semantic pens, each with exactly one job; no sixth pen may be invented.
- Archivo variable alone, width axis carrying the datasheet voice.
- A closed type ramp: seven literal steps and four responsive tokens.
- Every figure is named, united, and tagged measured / modelled / failed.
- One screen above 1180×759; panels clip, the page never does.
- Fully self-hosted — no CDN font, script, or image, enforced by the build.

## Colors

A cool grey paper stock and five working inks, where hue is a claim about
provenance rather than a decoration.

### Primary
- **Result Blue** (`{colors.blue}`): Orbit's own result, and only that. The gain
  figure, the Orbit curve in Fig 1, the filled score bars, a won slot in the
  ledger, the used segments of the contact-window meter, the focus ring. Its
  10% and 16% washes shade the gap between the two curves and fill secondary
  bars, so the wash always means "the same claim, quieter".

### Secondary
- **Counterfactual Pencil** (`{colors.pencil}`): the baseline Orbit is beating —
  send-oldest-first. Always drawn dashed as well as grey, never grey alone.
- **Live Red** (`{colors.red}`): something happening right now, or something the
  ground forced. The breathing pass lamp, the ring on the live slot, a forced
  fairness switch, a fault chip, the lost-link banner, a failed test tag.

### Tertiary
- **Measured Green** (`{colors.green}`): this number came off real hardware —
  a Vivado report, an on-board counter, a live wire measurement. Nothing else.
- **Modelled Amber** (`{colors.amber}`): modelled, scaled, simulated, or not
  measured yet. Its wash marks the highlighted row of a scaling table and the
  dashed line where the contact window saturates.

### Neutral
- **Bond Stock** (`{colors.stock}`): the page itself; also the hover fill of a
  raised control.
- **Plate** (`{colors.plate}`): every panel, and the halo stroke ringing chart
  end-dots so a mark reads against its own plate.
- **Sunk Plate** (`{colors.plate-sunk}`): recessed wells — input fields, bar
  tracks, image placeholders, the meter trough.
- **Ink** (`{colors.ink}`) / **Ink Two** (`{colors.ink-2}`): primary text and
  the quieter register for units, captions, labels, and axis text.
- **Rule** (`{colors.rule}`) / **Soft Rule** (`{colors.rule-soft}`): the hairline
  at 1px that does the separating — table heads and the baseline axis take the
  stronger rule, plate borders and gridlines the softer one.

A dark mode redefines all sixteen tokens at the same names (both via
`prefers-color-scheme` and an explicit `data-theme`), lifting each pen to a
brighter form against #10151a. Nothing downstream knows which mode it is in.

### Named Rules

**The Five Pens Rule.** Blue, pencil, red, green, amber. Each has exactly one
job: result, baseline, live-or-forced, measured, modelled. A new colour is a new
claim, and there are no more claims to make. Never add a sixth pen, and never
borrow an existing pen for a meaning it does not already carry.

**The Numbered Lane Rule.** A satellite is identified by its lane position and
its number, never by a hue. This is what lets the ledger show twelve satellites
without generating twelve colours, and what keeps blue meaning "Orbit's result"
in a panel full of satellites. Never assign a per-satellite colour.

**The Never Colour Alone Rule.** The baseline curve is dashed as well as grey;
the window-saturation line is dashed as well as amber; a live slot gets a ring as
well as red. Any distinction carried by one of the five pens must also be carried
by a second channel — dash, ring, weight, or position.

**The Ink-Only Rule.** A colour value in this build is always ink. The two
overflow masks on the queue and the sent-photo strip are written with the `black`
keyword precisely because they are alpha channels, not paint — reading them as an
undocumented colour token would be the mistake they are written to prevent.

## Typography

**Display Font:** Archivo variable (wght 400–800, wdth 62–125), self-hosted at
`/static/fonts/archivo-latin.woff2`, with a system sans fallback.
**Body Font:** Archivo — the same face, the whole ramp.
**Mono Font:** the platform monospace stack, used only inside the raw wire log.

**Character:** One grotesque doing every job, with the width axis as the sheet's
accent. Headings and big figures are set wider (106–118%) and tight-tracked;
supporting labels are set small, bold, and widely letter-spaced in caps. Tabular
numerals are the body default so a live-updating column never shivers; the two
big proportional figures opt out because they are read as a shape, not a column.

The ramp is closed and small: seven literal steps — 9.5, 10, 11, 11.5, 12.5, 14
and 19px — plus two clamps and four responsive tokens. Four of those steps live
inside two pixels of each other, which is deliberate rather than sloppy: 9.5 and
10 are always uppercase and letter-spaced, and caps at .07–.11em tracking read
optically larger and wider than the same pixel size set lowercase. The caps tiers
and the small-text tiers are therefore not interchangeable, and 11px lowercase
sits comfortably below 10px caps in the hierarchy despite the number.

### Hierarchy
- **Display** (`--t-hero`, 800, clamp 34–54px, lh .88, wdth 108%, tracking
  −.035em): the gain figure in Fig 1, in Result Blue. The one thing readable at
  two metres.
- **Headline** (800, clamp 24–31px, wdth 106%, tracking −.025em): the pass number;
  its trailing unit drops to .46em and Ink Two.
- **Title** (`--t-title`, 800, 23px, wdth 112%, tracking −.012em): the ORBIT
  wordmark, once.
- **Standfirst** (`--t-standfirst`, 400, 14.5px, tracking −.005em): the sentence
  beside the wordmark, with the question bolded to 600.
- **Readout** (700, 19px, tracking −.02em): a compared photo's score, with its
  `/ 100` denominator at 11px/500 Ink Two so the number carries the weight.
- **Body** (400, 14px/1.4, tabular): the sheet default and the boot message.
- **Lede** (`--t-lede`, 12.5px): two jobs at one size — the sentence beside the
  gain figure at lh 1.26 and max 40ch, and the entire control tier (transport
  buttons at 600, fields, the speed value at 700, the pass phase pill at 700,
  score-bar rows, table bodies, the satellite name at wdth 112%, the lost-link
  banner at 700).
- **Caption** (400, 11.5px/1.35, Ink Two): plate captions, control hints, the
  conditions line, the figure legend, key/value rows, the meter caption, the
  tooltip, the sentence-case descriptor in a plate heading at 500.
- **Small** (400, 11px, Ink Two): the dense readout tier — chart and axis text,
  queue rows, triplet meters, satellite metadata, the ledger lane name at
  700/.05em, the sheet tabs at 600/.085em uppercase, the gain total.
- **Label** (700, 10px, tracking .11em, uppercase, Ink Two): plate headings and
  satellite-strip headings, plus `.micro` section labels at .09em. The figure
  number inside a heading takes Ink and wdth 118%.
- **Micro** (700, 9.5px, tracking .07–.1em, uppercase): chips, honesty tags,
  control-rail labels, table heads, the compare plate's who-line, the sent-photo
  caption, and the ledger lane name once past three satellites.
- **Mono** (400, 11px/1.5, Ink Two, `white-space: pre`): the wire log only.

Responsive type changes are values on this ramp, not new steps: `--t-hero` drops
to 40px below 860px tall and 46px below 720px wide; `--t-title` to 20px and
`--t-standfirst` to `--t-lede` below 720px wide.

### Named Rules

**The Closed Ramp Rule.** Seven literal steps and four tokens are the whole ramp.
A new surface picks the nearest existing step; it does not add one. If two steps
would sit within half a pixel of each other doing the same job, they are the same
step — that is how 13px and 12px were absorbed into 12.5, and 10.5 into 11.

**The One Declaration Rule.** A role that changes size at a breakpoint owns a
`:root` token (`--t-hero`, `--t-title`, `--t-lede`, `--t-standfirst`) and exactly
one `font-size` declaration. Media queries override the token, never the element.
Never reintroduce a per-breakpoint `font-size` on a component.

**The Caps Tier Rule.** 9.5 and 10px exist only as uppercase, letter-spaced type;
11 and 11.5px exist only as sentence-case text. Letter-spaced caps read larger
than their pixel size, so the two pairs are separate tiers and swapping one for
the other inverts the hierarchy. Never set a caps label at 11px or body text at 10px.

**The Width-Axis Rule.** Hierarchy is carried by size and weight; the width axis
is carried by role. Figures and headings widen (106–118%); nothing else touches
`font-stretch`. Never substitute a second family for emphasis — Archivo is the
only face on the sheet.

**The Named Quantity Rule.** Every number on the sheet appears with a
plain-English name a non-specialist can read, its unit in Ink Two beside it, and
a provenance tag. "Photos scored per second · photos/s · MEASURED at 100 MHz",
never a bare number under an acronym.

## Layout

The sheet is a full-viewport three-row grid — title block, control rail with
conditions line, then the active sheet — at 9px gaps and 9/14/11px page padding.
Three sheets (Live run, Hardware, How far it scales) swap in place as tab panels;
only one is ever laid out.

The live sheet is a fixed 3×3 grid: Fig 1 (gain plus the two curves, gap shaded)
at 1.5fr on the left, the two compared photos in the centre column
(min 244px), the pass and contact window on the right (min 246px); the slot
ledger spans full width; the satellite strips span full width beneath, with
rows at minmax(262px,.9fr) / auto / minmax(196px,1.1fr). The hardware sheet is a
2×2 (engine, link, log full width); the scaling sheet is a 1.12fr/1fr pair over a
full-width notes plate.

Responsive behaviour is height-first, because the constraint is one screen, not
one column:
- **≤860px tall:** `--t-hero` drops to 40px, satellite thumbnails from 96px to
  84px, and the grid rows tighten.
- **≤780px tall (wide):** the already-sent photo strip and the compare caption
  are dropped — the priority queue is the story, the haul is not.
- **≤759px tall (wide):** the page starts scrolling and every fixed row becomes
  content-sized, deliberately, rather than clipping a figure in half.
- **≤1180px wide:** the sheet stacks in source order at two columns, every
  explicit grid row is cleared, plates stop clipping, and the page scrolls.
- **≤720px wide:** single column, `--t-title` to 20px and `--t-standfirst` to
  `--t-lede`, `--t-hero` to 46px, triplet metrics stack, the scenario hint is
  dropped, satellite thumbnails inline at 76px.

Spacing rhythm is fine and datasheet-tight: 2/3/4px inside components, 8–9px
between them, `--gap` 10px between plates, `--pad` 12px inside a plate. There is
no 16px+ step in the system; generous padding is not part of this world.

### Named Rules

**The One Screen Rule.** Above 1180px wide and 759px tall the live sheet fits the
viewport with `body { overflow: hidden }`. Panels clip their own overflow — the
queue fades out under a 9px mask, the sent-photo strip under a 22px horizontal
mask — and the page never scrolls. Below 759px tall the page is allowed to
scroll on purpose. Audit test: at 1440×800, nothing on sheet 1 is reachable by
scrolling, and no figure is cut.

**The Drop, Don't Shrink Rule.** When height runs out, whole secondary elements
are removed by media query and the primary ones keep their size. Never solve a
height squeeze by shrinking the gain figure below 40px or the queue below 38px.

## Elevation & Depth

Depth is carried almost entirely by tone and hairline rules: white plates on cool
stock, sunk wells in `plate-sunk` for anything that receives a value, and 1px
rules in two strengths. There is exactly one shadow token, applied uniformly, and
it is ambient rather than directional — a hairline contact plus a wide, deeply
negative-spread haze that reads as separation, not as lift. Nothing in the system
rises on hover; hover changes fill and border colour instead.

### Shadow Vocabulary
- **Plate lift** (`box-shadow: 0 1px 2px rgba(17,22,27,.05), 0 6px 18px -12px rgba(17,22,27,.30)`;
  dark: `0 1px 2px rgba(0,0,0,.40), 0 8px 22px -14px rgba(0,0,0,.70)`): every
  plate, every control group, the lost-link banner. There is no second step.
- **Ring, not shadow** (`box-shadow: 0 0 0 2px var(--plate), 0 0 0 3.5px var(--red)`):
  the live slot cell in the ledger; a plate-coloured halo then a red ring, so the
  mark survives against neighbouring cells. This is identification, not elevation.

### Named Rules

**The One Elevation Rule.** There is a single shadow token and it is ambient.
Never introduce a second elevation step, a hover lift, a directional or hard
offset shadow, or a shadow on anything smaller than a plate.

## Shapes

Corners are effectively square: 3px on plates, controls and tabs; 2px on chips,
tags, image frames and ledger cells; 1px inside a framed image; 5px only on the
bar track, its fill, and the scrollbar thumb. Nothing is a circle except the
9px status lamp, the chart end-dots, and the crosshair swatches.

Borders are the primary form device: 1px `rule-soft` around plates, controls and
image frames; 1px `rule` under table heads and along the chart baseline; 1px
`currentColor` around a chip, so a chip's outline is always its own meaning.
Recurring silhouettes are the plate (bordered rectangle, heading row, content,
optional caption), the track-and-fill bar, and the row of equal cells — the
ledger lane and the window meter are the same geometry at two scales, which is
the sheet's strongest repeated shape. Compared photos are locked square with
`aspect-ratio: 1` and rendered `image-rendering: pixelated`, because they are
data, not photography.

**The Matched Radius Rule.** A fill takes the radius of the well it sits in: the
score-bar fill is 5px because its track is 5px, not because 4px looked close
enough. Never invent a radius step for an inner element.

## Components

### Plate
The sheet's only container. White fill, 1px soft-rule border, 3px corners, 12px
padding, the single ambient shadow, and a column flex body. The heading is a
micro-caps label carrying a figure number in Ink at wdth 118%, then a
sentence-case plain-language descriptor at 500/11.5px, then optionally a
provenance chip pushed to the far end. An optional caption sits below the body at
11.5px Ink Two with its key noun bolded.

### Chips (provenance)
Character: a stamp, not a badge. 9.5px caps, 2px corners, 2×6px padding, 1px
`currentColor` border so the outline is the meaning.
- **hw** — green outline: a real board.
- **sim** — amber outline: a simulated satellite or a modelled figure.
- **bad** — red on a 10% red wash, borderless: a fault.
- **plain** — Ink Two on the stronger rule: neutral state.
- **solid** — white on solid red, borderless: happening right now ("Sending now").

### Tags (honesty vocabulary)
Bare uppercase micro type, no box, inline in a table cell or caption, immediately
followed by the provenance in Ink Two.
- **measured** (green) — followed by where it was measured.
- **estimate / modelled / not measured yet** (amber) — including the literal
  fallback `not measured yet` used wherever a number does not exist.
- **failed** (red) — a test that did not pass, stated as loudly as one that did.

### Controls
- **Control group:** white plate-let, soft-rule border, 3px corners, 36px min
  height, holding a micro-caps label plus its inputs. All controls live in one rail.
- **Transport button:** sunk fill, soft-rule border, 12.5px/600 Ink, 5×11px
  padding. Hover strengthens the border and takes the stock fill; the active
  state inverts to Ink fill with plate text; disabled drops to 40% opacity.
- **Fields:** sunk fill, soft-rule border, 3px corners, 12.5px; number inputs are
  52px wide and centred; ranges take blue as their accent colour.
- **Focus:** a 2px Result Blue outline at 2px offset, globally, on `:focus-visible`.

### Navigation (sheets)
Three tab buttons plus a theme toggle in the title block, at 11px/600 caps with
.085em tracking. Rest is Ink Two on transparent; hover takes Ink on a plate fill;
the selected tab inverts fully to Ink fill with plate text. No underline, no
indicator bar — the inversion is the indicator.

### Slot ledger (signature)
The sheet's clearest borrowing from a drum-machine step row: one right-aligned
74px lane name per satellite at 11px/700, then a flex row of equal cells at 17px
tall with 2px gaps. An empty cell is soft rule; a won slot is Result Blue; a
forced fairness switch is Live Red; the slot happening now carries the
plate-then-red ring. New cells land with a 450ms `scaleY` from .25. Above three
satellites the lane names drop to the 9.5px caps tier; the shape never changes.

### Contact-window meter
The same geometry rotated into a single 20px trough: equal segments, 2px gaps,
2px padding, sunk fill. Used capacity fills blue; the current slot is an unfilled
segment with a 2px inset red ring. A two-part caption underneath states what is
used and what is left.

### Charts (Fig 1 and the scaling sweep)
The SVG `viewBox` is set to the box's own pixel size, so one SVG unit is one CSS
pixel: axis text is never stretched and the plot is never letterboxed. Gridlines
are 1px soft rule with the zero baseline at full rule strength; axis values are
11px tabular Ink Two, direct labels the same size at 700. The Orbit curve is a
2px round-capped Result Blue polyline; the counterfactual is 2px Counterfactual
Pencil dashed `7 5`; the area between them is filled with the 10% blue wash,
because that gap is the argument. Each curve ends in a 4–4.5px dot filled with
its own colour and stroked 2px in the plate colour, and is labelled directly at
its end in its own colour, nudged apart only when the two labels would collide.
The sweep chart adds a dashed amber vertical where the contact window saturates,
labelled in words. The hover crosshair tooltip is Ink-filled with plate text and
repeats the values that are already on the axis.

### Named Rules

**The Round Number Rule.** `niceScale` picks the axis step from
0.1/0.2/0.25/0.5/1/2/2.5/5 × a power of ten, keeps 3–7 steps, and among the
candidates keeps the one clearing the peak with the *least* headroom — an axis
towering over the data flattens the very gap the figure exists to show. Then it
labels every ⌈ticks/4⌉th line so labels breathe. Never hand-pick an axis maximum.

**The Built Once Rule.** The DOM is constructed once and updated in place on the
10 Hz tick — text content, attributes, and `transform: scaleX()` on a bar fill
with a .3s ease. Never re-render a live region with `innerHTML` on tick, and
never create an `<img>` before a real corpus frame exists for it. This is why the
display does not flicker while a judge is reading it.

## Do's and Don'ts

### Do:
- **Do** give every pen exactly one job: blue = Orbit's result, pencil = the
  baseline, red = live or forced, green = measured on hardware, amber = modelled
  or not yet measured.
- **Do** identify satellites by numbered lane and position, so the display scales
  to twelve without inventing hues.
- **Do** pair a provenance tag with every figure — `measured` (green) with where
  it was measured, `estimate` / `modelled` (amber) with what the model assumed,
  and the literal `not measured yet` where no number exists.
- **Do** carry any distinction in a second channel besides colour: the baseline
  is dashed as well as grey, the live slot is ringed as well as red.
- **Do** pick the nearest existing step on the ramp — 9.5, 10, 11, 11.5, 12.5, 14,
  19px, or one of the four `--t-` tokens.
- **Do** give a role that resizes at a breakpoint a `:root` token and one
  `font-size` declaration, and override the token in the media query.
- **Do** keep 9.5 and 10px for uppercase letter-spaced type and 11 and 11.5px for
  sentence case; the caps tiers read larger than their number.
- **Do** keep the live sheet on one screen above 1180×759 — clip inside panels,
  with a mask fade, and let the page scroll only below 759px tall.
- **Do** write mask gradients with the `black` keyword, so an alpha stop is never
  mistaken for a colour token.
- **Do** set the SVG `viewBox` to the box's pixel size so one unit is one pixel.
- **Do** build the DOM once and update text, attributes and `scaleX()` in place
  on the 10 Hz tick.
- **Do** self-host every asset under `viz/static/` — `tools/offline_check.py`
  fails the build on any `https://` reference from `index.html`.
- **Do** set plate headings and large figures on the width axis (106–118%) and
  leave everything else at the default width.

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
- **Don't** add a second elevation step, a hover lift, or a directional or hard
  offset shadow; there is one ambient shadow token and it applies to plates.
- **Don't** reach for a second typeface, an icon font, or a glyph icon; Archivo
  and drawn SVG marks are the whole vocabulary.
- **Don't** solve a height squeeze by shrinking the gain figure or the queue —
  drop a secondary element instead.
- **Don't** give an inner fill a different radius from the well it sits in.
- **Don't** rebuild a live region with `innerHTML` on the tick, or create image
  elements for frames that do not exist yet.
- **Don't** link a font, script, or image from a CDN.
- **Don't** round a corner past 3px (5px on bar tracks) or introduce a gradient,
  glow, or green-on-black telemetry styling; this is a test report, not a console.
