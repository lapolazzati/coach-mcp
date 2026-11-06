# Using the coach from the Claude app

The coach exposes an MCP server at `https://coach-mcp.lapolazzati.com/mcp`, protected by
GitHub OAuth. Only one GitHub account (`COACH_MCP_ALLOWED_GITHUB_ID`) can connect.
Server-side setup: `deploy/runbook-auth.md` and `deploy/cloudflared-ingress.md`.

## 1. Add the connector (once)

Custom connectors are added on claude.ai (web or desktop) and then show up on the phone.

1. claude.ai → Settings → Connectors → **Add custom connector**.
2. Name `Coach`, URL `https://coach-mcp.lapolazzati.com/mcp`. Leave the advanced OAuth fields empty
   (the server supports dynamic client registration).
3. **Connect** → GitHub login → authorize the app. A GitHub account other than the allowed one
   gets "access denied".
4. On the phone (Claude iOS/Android): open a chat → the `+`/tools menu → enable **Coach**.

Check: ask "ping the coach". The `ping` tool should answer.

## 2. What you can ask

Read tools: `get_today`, `get_wellness`, `get_training`, `get_trends`, `get_plan`, `get_notes`,
`get_meals`, `get_supplements`, `get_week_summary`.

Write tools (all reversible with `undo_change`): `log_meal`, `log_weight`, `add_note`,
`log_supplement`, `set_supplement`, `save_weekly_review`, `move_session`, `skip_session`,
`swap_sessions`, `save_month_plan`.

Examples:

- "How did I sleep and what's on the plan today?"
- "Plan November: 4 rides a week, long ride Saturday, I'm away 20–22." → `save_month_plan`
- "Move tomorrow's ride to Sunday." / "Skip today's session, I'm sick."
- "Log lunch: 120 g pasta, tomato sauce, 30 g parmigiano."

## 3. How planned workouts reach Zwift / Garmin

Saving or moving a session marks it dirty. Every 30 minutes the coach's scheduler pushes dirty
sessions to intervals.icu as WORKOUT events. intervals.icu then syncs them to Zwift (needs
"Upload workouts to Zwift" on in intervals settings) and to Garmin Connect.
So a change appears on Zwift/Garmin within ~30 minutes, plus the provider's own sync delay.
Plan sessions the evening before at the latest.

The morning job (every 10 min, 05:30–17:50) can ease today's session when readiness is low
(and re-push it the same way).

## 4. Scheduled meal-reminder chats

The coach has no push channel to the app. Reminders come from the Claude app itself:

1. In the Claude app create a **Project** "Coach" with instructions such as:
   "You are my cycling and nutrition coach. Use the Coach connector. When I send a meal, log it
   with log_meal; at the end of the day summarise kcal vs target with get_meals and get_today."
2. Use the app's scheduled tasks / reminders (where available on your plan) to start a chat in
   that project at meal times, e.g. 13:00 "What did you eat for lunch?" and 20:30 "Dinner?".
   If scheduled tasks aren't available on your plan, use a phone reminder that opens the project.
3. Reply in the chat. Claude logs the meal through the connector. Check later with "what did I eat today?".

## Troubleshooting

- **Connector shows "error" / 401 loop**: reconnect it (Settings → Connectors → Coach → Disconnect →
  Connect). After a `COACH_MCP_JWT_KEY` rotation every client must reconnect.
- **"Database schema too old"**: the `coach` container hasn't migrated yet; check
  `docker compose ps` on catone and the `coach` logs.
- **Workout not in Zwift**: look at intervals.icu calendar first. If it's there, the problem is the
  intervals→Zwift sync; if not, check `coach` logs for `reconcile` errors.
