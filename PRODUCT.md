# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

FastAPI + Jinja server-rendered templates, static CSS/JS in `coach/web/static/`, SQLite. Hosted on Lapo's Hetzner machine, reachable only over Tailscale (`tailscale serve`, no public ports, no app login). Decided in `docs/plans/2026-11-03-001-feat-personal-cycling-coach-plan.md` (U12, R28).

## Users

One user: Lapo. Busy parent of a young child, sleeps badly some nights, rides ~2×45 min/week on Zwift at 07:00–07:30 plus a weekend long ride, two gym sessions/week for posture and shoulder rehab. Opens the dashboard mostly as a **morning glance on his phone**, before or after the early ride. UI language: English.

## Product Purpose

A lifelong n=1 AI cycling coach. It reads sleep/recovery (Garmin via intervals.icu), calendar (Google Workspace), travel and training history, decides what to train, when and how hard, and places the workout in intervals.icu → Zwift/Garmin. Telegram is the conversational channel; the dashboard is the place to see the plan and progress.

First goal: Strade Bianche Gran Fondo short route (87 km, 1,350 m, 6 gravel sectors), Sunday 7 March 2027, Siena, finish under 3h30. Supporting goal: 82 → 74 kg at 0.5–0.7 kg/week.

Success for the dashboard: at a glance, the top of the screen answers **"what do I do today?"** (session, slot, why the coach chose it); below, whether he is on track for the race.

## Positioning

Specificity to one person: every number and choice comes from Lapo's own data, calendar and constraints, not a generic plan. Inspired by Raphael Schaad's n=1 AI marathon coach dashboard (goal header with A/B/C targets, course profile with fuelling markers, weekly volume bars, daily session cards).

## Operating Context

- Phone over Tailscale, often half-awake, early morning; occasionally desktop.
- Coach acts autonomously; dashboard is read-mostly. Actions (A/B choices, gym done, undo) happen in Telegram.
- Training calendar Jan–Feb is travel-heavy: runs substitute for rides.

## Capabilities and Constraints

Dashboard data (U12 endpoints): goal + A/B/C targets and projected finish; course profile (smoothed GPX elevation, gravel sectors, fuelling markers); fitness series (CTL/ATL/TSB); power curve; weight series vs target; weekly volume; adherence; sessions by day (ride, run, gym, rest; status planned/done/missed/moved); past activities with segment comparisons.

Readiness exists only after 14 days of baseline data; early states must be honest about missing data.

## Brand Commitments

None beyond the name of the goal event. No logos or brand assets.

## Evidence on Hand

GPX `SB_MF_26_CRT.gpx` (official 87 km / 1,350 m; raw GPS overstates ~1,740 m). No real training data in the repo yet; mockups use clearly labelled sample data and must never be presented as real.

## Product Principles

1. Today first: the answer to "what now" beats every chart.
2. Context-aware or useless: show *why* the coach chose something.
3. Honest about trajectory: say plainly if the goal looks out of reach.
4. Quiet: no nagging, no gamified noise.
5. Lifelong: Strade Bianche is goal #1, the structure must hold future goals.
