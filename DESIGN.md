---
name: Coach Dashboard "Settori"
description: One rider's morning-glance cycling dashboard, drawn in Strade Bianche dust, gravel, ink, cypress and terra.
colors:
  dust: "#EEE9DF"
  dust-2: "#E5DFD2"
  gravel: "#CBBDA5"
  gravel-ink: "#6B604E"
  ink: "#24211B"
  ink-2: "#4A453B"
  cypress: "#3F4B30"
  terra: "#B5432A"
  plate-fg: "#F3EEE4"
  plate-mute: "#B9AF9C"
typography:
  projection:
    fontFamily: "Barlow Condensed, Arial Narrow, sans-serif"
    fontSize: "76px"
    fontWeight: 800
    lineHeight: 0.82
    letterSpacing: "-0.02em"
  plate-title:
    fontFamily: "Barlow Condensed, Arial Narrow, sans-serif"
    fontSize: "44px"
    fontWeight: 800
    lineHeight: 0.95
    letterSpacing: "-0.01em"
  h2:
    fontFamily: "Barlow Condensed, Arial Narrow, sans-serif"
    fontSize: "28px"
    fontWeight: 800
    lineHeight: 1
    letterSpacing: "-0.005em"
  h3:
    fontFamily: "Barlow Condensed, Arial Narrow, sans-serif"
    fontSize: "17px"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "0.02em"
  figure-value:
    fontFamily: "Barlow Condensed, Arial Narrow, sans-serif"
    fontSize: "32px"
    fontWeight: 700
    lineHeight: 1
  label:
    fontFamily: "Barlow Condensed, Arial Narrow, sans-serif"
    fontSize: "12px"
    fontWeight: 600
    lineHeight: 1
    letterSpacing: "0.07em"
  body:
    fontFamily: "Barlow, system-ui, sans-serif"
    fontSize: "16px"
    fontWeight: 400
    lineHeight: 1.45
rounded:
  plate: "3px"
  cell: "2px"
spacing:
  gutter: "16px"
  gutter-wide: "32px"
  gap: "28px"
  gap-wide: "36px"
  column-gap: "48px"
  plate-pad: "20px"
components:
  plate:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.plate-fg}"
    rounded: "{rounded.plate}"
    padding: "20px 20px 22px"
  row-today:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.plate-fg}"
  badge-sample:
    textColor: "{colors.terra}"
    padding: "4px 7px 3px"
  cal-cell-done:
    backgroundColor: "{colors.cypress}"
    rounded: "{rounded.cell}"
  cal-cell-missed:
    backgroundColor: "{colors.terra}"
    rounded: "{rounded.cell}"
  cal-cell-moved:
    backgroundColor: "{colors.gravel-ink}"
    rounded: "{rounded.cell}"
  cal-cell-rest:
    backgroundColor: "{colors.dust-2}"
    rounded: "{rounded.cell}"
---

# Design System: Coach Dashboard "Settori"

Source of truth: `coach/web/static/dashboard.css` (cited as css:line), `coach/web/templates/dashboard.html` (html:line), with data contracts in `coach/web/context.py` and `coach/web/sample_context.py`. The CSS header names the world "Settori" (css:1). Tokens live in `:root` (css:15-37).

## Overview

**Creative North Star: "The Gravel Sector Plate"**

The page reads like a race-day route card on Tuscan dust: a pale dust ground, ink-dark plates for what matters now, gravel hairlines for structure, and two signal colors borrowed from the landscape, cypress green for done and terra red for behind, missed or moved. Section titles borrow the stage-board feel of condensed uppercase type; the topline counts "Settore N of M" (html:22). The north star name is descriptive of the build, not a confirmed user phrase.

Density is glance-first. Phone is the primary surface (single column, 16px gutters); the desktop layout widens to a 1180px two-column header, a seven-day week strip and a four-up trends grid (css:101-124). Depth is flat: hairlines and tonal fills, one ink plate, no shadows. The only motion is a one-time route-draw on the course line (css:281).

**Key Characteristics:**
- Two faces only: Barlow Condensed for numbers and headings, Barlow for prose.
- Ink plate for "today"; everything else sits on dust with 1px gravel rules.
- Cypress means done, terra means behind/missed/moved/target, never decoration.
- Tabular, lining numerals everywhere (css:48, 58).
- Charts are server-built inline SVG, no JS and no chart library (`charts.py:1-5`).
- No invented numbers: every section has an explicit empty state.

## Colors

A warm, desaturated earth palette; two accents used sparingly and semantically.

### Primary
- **Terra Clay** (`terra`, #B5432A, 4.6:1 on dust per css:23): behind-target gap (css:221), missed icon, moved rows, target reference lines (css:226, 406), sample badge, focus ring (css:54), selection, caret, stars on, "Today" note chip on the ink row.
- **Cypress** (`cypress`, #3F4B30): done state. Done icon (css:338), done calendar cell, up-deltas in tables (css:498), fuel marks (css:268, 296), `accent-color`.

### Neutral
- **Dust** (`dust`, #EEE9DF): page ground and theme backdrop for text on terra selection.
- **Dust Shade** (`dust-2`, #E5DFD2): gravel-sector bands on the course (css:254), rest cell fill, empty-sector legend key.
- **Gravel** (`gravel`, #CBBDA5): hairline rules (rows, panels, tables, targets), course area fill (css:272), plate subtitle text on ink.
- **Gravel Ink** (`gravel-ink`, #6B604E, 5.0:1 on dust per css:19): all secondary and muted text, chart fatigue line, moved cells, labels.
- **Ink** (`ink`, #24211B): primary text, plate background, chart lines, bar fills, today row background, theme-color meta (html:7).
- **Ink Soft** (`ink-2`, #4A453B): secondary body, done-row text, planned-bar outline.

### Plate variants (on ink only)
- **Plate Foreground** (`plate-fg`, #F3EEE4): text on ink.
- **Plate Mute** (`plate-mute`, #B9AF9C): secondary text on ink.
- One-off plate literals in the CSS, not tokens: `#7D7465` separators (css:164), `#8E8573` empty star stroke (css:166), `#4A443A` plate divider (css:174), `#A9B795` "Waiting in Zwift" green (css:185). Reuse only when extending the plate.

### Named Rules
**The Two Signals Rule.** Cypress = done/gain, terra = behind/missed/target. Neither is used as a decorative accent. A new status needs a neutral (gravel-ink or ink-2) unless it is genuinely good or bad.

**The Plate Rule.** Only the today plate and the today row use the ink-fill inversion. Nothing else on the page is dark-filled except data marks (bars, chart lines, done cells).

## Typography

**Display Font:** Barlow Condensed (500 / 700 / 800; fallback "Arial Narrow", sans-serif), `--display` css:27.
**Body Font:** Barlow (400 / 600; fallback system-ui, sans-serif), `--body` css:28.
Both self-hosted as latin woff2 in `coach/web/static/fonts/` (`barlow-400`, `barlow-600`, `barlow-condensed-500/700/800`), `font-display: swap` (css:4-13). The 800 condensed file is preloaded (html:9).

**Character:** Condensed uppercase headings and numerals read like race signage; Barlow body keeps prose calm. Weights ride the role: 800 for the biggest numbers and section titles, 700 for sub-heads and values, 600 for labels, 500 for day names and plate subtitle.

### Hierarchy
- **Projection value** (800, 76px / 64px at <=380px, lh .82): projected finish (css:207-211, 511). Empty variant 44px gravel-ink (css:212).
- **Plate title** (800, 44px / 38px at <=380px, lh .95, uppercase): today's session and slot (css:133-150). Subtitle 500, 30px.
- **H2** (800, 28px, lh 1, uppercase): section titles (css:65).
- **Target and gap values** (700, 22px): target times, gap (css:220, 246). Tier letters 800, 22px.
- **Plate load line** (600, 19px, .05em, uppercase) (css:158).
- **H3** (700, 17px, .02em, uppercase): panel and ride titles (css:70).
- **Figure value** (700, 32px) with unit `small` 500 15px: key figures (css:398-399).
- **Body** (Barlow 400, 16px, lh 1.45): prose; 15px for subs and tables, 14px for meta and plate note, 13px for captions and legends.
- **Label** (600, 12px, .06-.07em, uppercase): figure `dt`, table headers, row notes (css:316, 392, 492). Smallest sizes: 12px bars/fuel, 11px calendar day letters (css:462).

### Named Rules
**The Numerals Rule.** Every number gets `tabular-nums lining-nums` (`.num`, css:58); body already sets tabular-nums (css:48). Numbers are condensed display weights, prose is Barlow.

**The Condensed-Caps Rule.** Uppercase is reserved for condensed display text (headings, labels, table heads); Barlow body is never uppercased.

## Layout

Single column at phone width: `.page` grid, `--gutter` 16px, `--gap` 28px between sections, max-width 1180px, safe-area insets honored top and bottom (css:77-108). Order: plate, goal, week, trends, rides, course (html:29-288).

At >=960px (css:111-124): gutter 32px, gap 36px, grid `5fr / 7fr` with 48px column gap; plate left, goal right, then week, trends, rides and course span full width.

Responsive details:
- **Week** (css:345-365): vertical rows at phone (52px day | what | status, min-height 48px); at >=960px a seven-column strip of cells (min-height 132px, 1px gravel dividers).
- **Trends** (css:373-389): one column at phone, two at >=640px, four at >=960px. The Power panel spans the full width; at >=960px it splits into figures left, table right (max 560px, 48px gap).
- **Rides** (css:505): single list, two columns at >=960px.
- **<=380px** (css:509-513): plate title 38px, projection 64px, segment table hides its third column (Best before).
- Course labels are HTML positioned by km percentage so they keep size while the SVG stretches (html:267, css:253-262).
- Spacing in use is an ad hoc scale rather than a named one: 4, 6, 8, 10, 12, 14, 16, 18, 20, 28, 36, 48px. Panels use 18px/20px vertical padding; plate uses 20px.

## Elevation & Depth

Flat. No `box-shadow` for elevation. Depth is hierarchy by tone: the ink plate over the dust ground, 1px gravel hairlines between rows, panels and table rows. The only `box-shadow` values are inset 1-2px rings used as calendar cell outlines (planned, rest, today; css:479-481). Course sectors are tonal bands, not raised.

### Named Rules
**The Hairline Rule.** Separation is a 1px `gravel` rule (or the ink fill of the today row), never a shadow or a card border.

## Shapes

Near-square. Plate radius 3px (css:131); calendar cells 2px (css:475); focus ring has 2px radius (css:54). Everything else is square: sample badge has a 1px terra border (css:92), planned-volume bars have a 1px ink-2 outline (dashed for the current week, css:440, 452). Icons are inline SVG symbols (star, check, moved arrow, missed cross, fuel drop; html:13-19), 12-20px, `currentColor`. Chart strokes use `vector-effect: non-scaling-stroke` so lines stay 1-2.5px when the SVG stretches.

## Components

### Today plate (`.plate`, css:127-192)
Ink block, plate-fg text. Title row: session name plus 30px gravel subtitle left, 44px slot time right. Load line: `TSS n · Load` with five stars (terra when on, `#8E8573` outline when off). Then detail, a ruled "why" paragraph in plate-mute, and an optional "Waiting in Zwift" check line. If `session` is empty: title "Rest day / Nothing scheduled" (html:41-42). A plate note states readiness progress ("Readiness starts after 14 days of sleep and HRV data. n of 14 so far.") while readiness is unavailable (html:44-46).

### Goal block (`.goal`, css:194-299)
H2 with place plus muted "· N days". Subline with name, route, date and a stats line. Projection: 76px projected finish, "Projected finish" label, gap line (`+8′ vs 3:30`, terra when positive, ink otherwise), and a 120x28 sparkline of weekly projections with a dashed terra reference at target B (faster is up; domain always includes B, html:71-73). Targets: a three-column ruled strip, tier letter plus time plus label; tier B ("Goal") is full ink and 600, A and C are gravel-ink.

### Week rows (`.row`, css:301-366)
Grid of day (dow bold ink plus date), what (title plus optional note), and end (slot time, or a status icon). States set by `row--{status}` (html:104):
- **planned / rest**: base row, slot time at end. No distinct CSS.
- **done**: title in ink-2; end shows cypress check (20px).
- **missed**: end shows terra cross (18px).
- **moved**: terra title, struck through (`<s>`), moved-arrow icon and a terra note (css:341-343, 366).
- **today** (`.row--today`): ink fill with plate-fg text, bleeds past the gutter by `--row-inset` (default 10px, css:325), a "Today" note chip in terra on plate-fg (css:334); the next row's top border turns ink (css:332). At >=960px the bleed is dropped (css:363-364).
- **today and done** (`.row--today.row--done`, css:336-337): the title color is forced back to plate-fg, because `.row--done .row__what` sets ink-2, which would be unreadable on the ink fill. Status icon colors (`.ico--done` cypress, `.ico--missed` terra) are not overridden on today rows. See Don't list for the contrast flag.

### Trends (`.trends`, `.panel`, css:368-483)
Panels: H3, figure row, chart, caption with a keyed legend. Hairline top rule, no card.
- **Fitness and fatigue**: figures Fitness, Fatigue, Form (signed); 300x90 line chart, solid ink fitness, dashed gravel-ink fatigue, dashed terra race-day target; y-domain padded 3 beyond data and target (html:133-134).
- **Weight**: Now, Target, Pace (kg/wk, or "–" until two weeks span); solid ink line with terra target line, axis ends at target.
- **Weekly volume**: bar per week, outlined = planned, filled ink = done, current week dashed with a gravel-ink fill. Planned is absent until the planner writes sessions; the legend only shows "Planned" when any exists (html:171, 184).
- **Adherence**: Sessions "n of m" and Rate %; a GitHub-style grid, one square per day, one column per week, Monday on top. Cell states: done cypress, moved gravel-ink, missed terra, rest dust-2 with inset hairline, planned inset gravel-ink ring, today inset 2px ink ring. "Moved counts as done."
- **Power** (wide): FTP and W/kg figures; table Best / Last 6 wks / 6 wks before / Change, up deltas cypress, down terra.

### Tables (`.table`, css:485-499)
15px, right-aligned numeric columns, first column left-aligned row header (600). Header row is 12px condensed uppercase gravel-ink; body rows have 1px gravel top rules.

### Recent rides (`.ride`, css:501-507)
H3 title, meta line (date, km, time, optional NP) in gravel-ink 14px, optional segment table (Segment, Today, Best before, Change in signed seconds; negative is cypress).

### Course profile (`.course`, css:252-299)
Sector labels "S1..S6" (700, 14px) above an 84px-tall SVG: dust-2 sector bands, gravel area fill, 2.5px ink line that draws in once over 1.4s (disabled under `prefers-reduced-motion`, css:284). Fuel marks below sit under a 2px cypress rule with a drop icon and km. Legend lists sector key and fuel items. A muted line "Sector and fuel positions are placeholders." shows while `positions_are_placeholders` is true (html:285).

### Topline and badge (css:76-98)
Date and "Settore n of N" in 15px condensed caps; terra outlined "Sample data" badge appears only for sample contexts (html:23).

## Empty and day-one states

Rule: no number is ever fabricated (`context.py:3-6`). Each section reads real rows only, and falls back to a day-one shape (`None` or `[]`) that triggers explicit copy (`.empty`, 15px ink-2, max 34ch, css:416-422). `sample_context("day1")` renders this state (`sample_context.py:139-154`). Copy in use:
- **Projection**: "Not yet" (44px, gravel-ink) plus "Needs two weeks of rides with power." (html:84-88). The finish-time model is unbuilt, so `projected` is always None (`context.py:86`).
- **Readiness**: plate note with days collected of needed (html:45).
- **Fitness**: "Fitness and fatigue build from intervals.icu once the first rides sync." (needs 2 points, `context.py:132`).
- **Weight**: day one shows the stated starting weight as the single point, Pace "–", and "One weigh-in so far. The trend appears after the second." Pace needs two weeks of span (`context.py:163-171`).
- **Volume**: "Weekly hours appear after the first full week." (`[]` if every week is zero).
- **Adherence**: "Adherence starts counting after the first planned week." (currently `None` in the live context, `context.py:55`).
- **Power**: "Power curve appears once rides with a power meter or Zwift sync." (requires a 20-minute best and a curve row with both windows).
- **Rides**: "No rides synced yet. They arrive from intervals.icu after each session."
- **Week**: past days without activity read "Nothing logged", future days "Not planned yet"; nothing is invented as planned.
- **Sparklines and charts** need more than one point; `line_path` breaks the line at `None` rather than interpolating (`charts.py:27-41`).

## Do's and Don'ts

### Do:
- **Do** render every new number from a real row and give its section explicit empty copy that says what will make it appear.
- **Do** use Barlow Condensed (700-800, uppercase for headings and labels) for numbers and titles, Barlow 400/600 for prose, and `.num` on every figure.
- **Do** separate content with 1px gravel hairlines; use ink fill only for "now" (plate, today row).
- **Do** keep muted text at `gravel-ink` (5.0:1) and not lighter `gravel`, except on ink.
- **Do** draw charts as server-side inline SVG with `vector-effect: non-scaling-stroke`, with an `aria-label` that states the range.
- **Do** keep status as icon plus color (check, cross, arrow), with `aria-label`s, so color is never the only cue.
- **Do** keep layout single column at phone width and add columns at 640px and 960px only.

### Don't:
- **Don't** add shadows, rounded cards or gradients; the system is flat and near-square.
- **Don't** use cypress or terra as decoration.
- **Don't** hardcode new colors; the four plate-only literals (css:164, 166, 174, 185) are existing one-offs.
- **Don't** fill a chart, projection or table cell with a plausible-looking number when data is missing.

### Not canonized (defects the build carries)
- Today plus done: `.ico--done` (cypress #3F4B30) and `.ico--missed` (terra) render on the ink fill with roughly 1.7:1 and 2.9:1 contrast. Estimated, not measured in a browser. The title was fixed (css:337) but the icons were not.
- With `session` None (the live state until the planner exists), the plate states "The coach left today open. Recovery counts as training." (html:42), which asserts a coach decision that was never made. Not a design-system rule.
- `!important` on legend key margins (css:411-414), and the `.key.cal__d--*` selectors (css:482) are workarounds.
- Power FTP is an estimate (95% of best 20 min, `context.py:218-219`) displayed without an "estimated" label.
