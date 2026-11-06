# Personal Cycling Coach — Design

Date: 2026-11-03 · Owner: Lapo (single user, n=1)

## Intent

A personal, context-aware cycling coach for one person. It knows Lapo's sleep, recovery, calendar, travel, training history, weight and eating, and turns that into one clear answer each day: *what to train, when, and how hard* — with the workout already waiting in Zwift.

Inspired by Raphael Schaad's n=1 AI marathon coach: the value is **specificity to me**, not generic training plans.

**Why now:** Lapo has a young child, sleeps badly some nights, rides ~2×45 min/week on Zwift at 7:00–7:30, and skips sessions after bad nights or when the morning gets eaten. Time is lost deciding what to do; sessions are lost because nothing proposes an alternative slot.

**First goal:** Strade Bianche Gran Fondo, short route (GPX `SB_MF_26_CRT.gpx`: official: 87 km, 1,350 m climbing, 6 gravel sectors; raw GPS overstates at ~1,740 m), Sunday **7 March 2027**, start Fortezza Medicea, finish Piazza del Campo, Siena (https://gfstradebianche.it/), finish **under 3h30** (~25 km/h average). Supporting goal: body weight from **82 kg → 74 kg** by race day.
The coach is **lifelong**: Strade Bianche is the first goal, not the product. After it, new goals/events are added.

## Goals

1. **Daily decision, zero friction.** Each training day the right workout is already in Zwift (via intervals.icu). Lapo never has to pick a workout.
2. **Recovery-aware.** Uses Garmin sleep, HRV, body battery, resting HR to adjust intensity/volume (bad night → shorter/easier or rest, week rebalanced).
3. **Calendar-aware rescheduling.** Reads Google Calendar. If the morning slot is lost, finds another gap that fits ("free 15:00–18:00 → gym"). Plans around early commitments (see example below).
4. **Travel-aware.** Detects trips from the calendar; plans no-bike days as **runs**, rest or gym, front/back-loads riding around them. January–February will be travel-heavy with less riding → running substitutes for aerobic load.
5. **Own training calendar.** A plan from today to the goal date (periodised: base → build → peak → taper), including Lapo's 2 upper-body gym sessions/week, continuously adjusted.
6. **Progress & motivation.** Dashboard shows fitness/fatigue trend, power progress, weight vs. target, plan adherence, past rides and segment comparisons; motivating, specific feedback via chat.
7. **Light food coaching.** Photo + short text logging via chat, reminders to log, a daily "did you eat healthy / drink?" check-in, weight trend. Supports the 82→74 kg goal without undermining training (more fuel on hard days, deficit on easy/rest days).

### Key scenario (acceptance example)
Wednesday: calendar has a train at 08:30. The evening before, the coach computes travel time home → station and prep time, works back to the latest finish time, and messages once: *"Train tomorrow 08:30. Option A: 40 min Z2 at 06:40. Option B: skip, moved to Thursday evening. Which?"* Lapo taps one; the workout is placed in intervals.icu and appears in Zwift.

## Non-goals

- Food is **not** the centrepiece. No calorie-perfect tracking, no meal planner. Use an existing open library/model for photo recognition; keep it light.
- Not multi-user, not a product for others.
- Gym is scheduled as time blocks, not programmed in detail.
- No detailed run programming beyond what's needed to keep aerobic fitness during travel blocks (see Travel).

## Anti-goals (would make this a failure even if it works)

- **Not context-aware** → Lapo will avoid it. Never suggest a ride during a meeting, on a plane, or hard intervals after a terrible night.
- **Nagging / too many messages.** Speak rarely, at the right moment, already knowing why. Target: ≤ ~3 proactive messages on a normal day (evening plan, optional morning adjustment, food nudge only if nothing logged).
- **Chore-like logging.** Logging must be one photo + one line, or one tap.
- Blowing the AI budget (see constraints).

## Constraints

- **Hosting:** Lapo's Hetzner machine. Web dashboard + backend + scheduler there, dashboard behind login.
- **Database:** SQLite (Lapo accepts Postgres or SQLite; SQLite chosen — one user, simple backups).
- **AI budget:** Claude via Claude Agent SDK/API, hard cap **$100/month** (Lapo's Max subscription reportedly includes a $100/month API credit — unverified; the app must track spend and cap regardless).
- **Data sources (already connected by Lapo):**
  - **Garmin:** official **Garmin Personal API** (personal API key → token) as primary; `python-garminconnect` (unofficial) as fallback for anything missing. Lapo wears the Garmin constantly.
  - **intervals.icu:** already linked to Garmin and Zwift. It is the training backbone: plan/workouts are written here, Zwift picks them up automatically, fitness/fatigue (CTL/ATL/TSB) comes from here.
  - **Google Calendar:** all calendars are aggregated in Lapo's Google Workspace account (ethon.io); Lapo is the Workspace admin, so OAuth with Calendar read/write scopes is available. Read access needed; write access for the coach's own training calendar is desirable.
- **Messaging:** Telegram bot now. iMessage preferred long-term but needs an always-on Mac relay (no Apple bot API) — deferred.

## Decisions already made (by Lapo)

- **Full coach autonomy:** the coach decides — it swaps, moves and rewrites sessions on its own and tells Lapo what it did and why. It doesn't ask permission. (Still one message per change, not per thought.)
- **Weekly structure:** 2 weekday Zwift sessions (~45 min, 07:00–07:30) + a **weekend long ride** (indoors in winter, outdoors otherwise) + 2 upper-body gym sessions. The coach may add sessions when the calendar and recovery allow.
- **Running** is in scope as the substitute during travel (Jan–Feb especially).

- Telegram + web dashboard (both).
- Hetzner hosting.
- SQLite or Postgres.
- intervals.icu as the hub to Zwift.
- Claude as the brain within the $100/month budget.
- Food: photo + text, reminders are crucial, not the centrepiece.
- Lifelong coach, goal-agnostic.

## My calls (Claude) — open to override

- **Python** backend, **FastAPI**, **SQLite**, scheduler in-process (APScheduler) or cron.
- **Model tiering:** Haiku for routine nudges/check-ins/parsing; Sonnet (or Opus) for weekly plan generation and complex reschedules. Running spend ledger with hard stop + Telegram alert at 80%.
- **Calendar output:** a dedicated Google Calendar "Training" (coach-owned) so sessions show up next to real life.
- **Travel time to calendar locations:** maps lookup (e.g., Google Maps Distance Matrix) from a configured home address; fallback to a default buffer if no location.
- **Segments:** "previous segments" interpreted as the Strade Bianche gravel sectors and Lapo's own repeated routes; source via intervals.icu/Garmin activity streams (Strava only if already in the picture — unconfirmed).
- **Gym:** the coach schedules the 2 weekly upper-body sessions as blocks and treats them as load/recovery inputs; it doesn't write gym programmes.

- **AI auth:** plan for a plain Anthropic API key with the self-imposed $100/month cap; if the Max credit applies to that key, it's a bonus.
- **Wellness data source:** intervals.icu already pulls Garmin wellness (sleep, HRV, resting HR, weight) — use it as primary; Garmin Personal API for extras (body battery, stress, sleep stages); `python-garminconnect` last-resort fallback.
- **Zwift delivery:** relies on intervals.icu → Zwift workout sync, which Lapo has connected. First build milestone verifies a coach-written workout actually appears in Zwift; if not, fallback is writing `.zwo` files.
- **Weight-loss safety:** cap at ~0.5–0.7 kg/week; protect fuelling on hard days and in the last 2 weeks before a goal event. 74 kg is a target, not a hard constraint.
- **Feasibility:** the coach states plainly if a goal looks out of reach on the current trajectory and proposes adjustments, rather than silently pretending.
- **Privacy:** health/calendar data is sent to Claude only as needed per call; meal photos are stored on the Hetzner box only. Telegram locked to Lapo's chat ID; dashboard behind a single-user login.

## Left to the builder

- Data model, project structure, deployment details (Docker/systemd), auth for the dashboard.
- The coaching logic specifics (zone model, periodisation math) — must be grounded in intervals.icu data (FTP, CTL) not invented numbers.
- Food photo recognition library choice.
- Dashboard UI design (should resemble Raphael's: goal header with A/B/C targets, course profile with fueling markers, weekly volume bars, daily session cards).

## Notes

- ~18 weeks from 2026-11-03 to race day, with a travel-heavy Jan–Feb in the middle — the plan should front-load bike volume in Nov–Dec and protect the weekend long ride.
- The race page body still says "domenica 8 marzo" (2026 leftover); header and calendar agree on Sunday 7 March 2027.
