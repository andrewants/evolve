# Changelog

## 2.5.0

- Pad the app out of the Dynamic Island and the notch, so screen headers and
  the sub-screen back buttons are reachable on the home-screen web app.
- Sit the tab bar flush on the bottom edge at its natural height, instead of
  floating above a gap with stacked home-indicator padding.
- Stop iOS zooming the page on double taps and on focused fields: every
  editable control renders at 16px on touch, and the shell disables the
  double-tap gesture.
- Pin the shell so iOS can no longer rubber-band the document and drag the tab
  bar off the bottom of the screen.
- Unwind sub-screens and dialogs with the iOS back-swipe gesture and the
  Android back button.
- Lift dialogs and the scroller clear of the software keyboard.
- Ship a web app manifest and home-screen icons, so installing the app gives
  it a real icon, name and standalone launch.
- Date the last weight reading by recency — `Today 08:22`, `Yesterday`,
  `3 days ago` — falling back to the date only once it is over a week old,
  and drop the "Mi Scale via Home Assistant" caption from both weight views.
- Show every clock time on a 24-hour clock regardless of the device's locale.
- Keep the time a weight reading was taken, so today's weigh-in can show one.
  Readings synced before this upgrade have no stored time and show a bare
  `Today` until the next sync replaces them.

## 2.4.3

- Make the primary gym streak count consecutive active weeks rather than
  requiring every week to hit the configurable session goal.
- Keep consecutive goal-completed weeks as a separate statistic alongside
  current `sessions / goal` progress.
- Preserve an active streak during an unfinished week before its first
  session, just as goal streaks remain open until Sunday.

## 2.4.2

- Correct Monday-aligned workout buckets so Tuesday–Sunday sessions are no
  longer dropped from the current week or shifted into the wrong week.
- Keep a completed weekly streak active while the current week is still in
  progress and below its goal.
- Calculate the streak from the complete workout history instead of capping it
  to the eight weeks displayed by the bar chart.
- Show current-week session progress and clarify that the week remains open
  through Sunday.

## 2.4.1

- Parse flat and nested BodyMiScale measurements, including `value`/`state`
  objects and common weight, fat, water, muscle and visceral-fat aliases.
- Fall back to the composite entity's current state and any separately mapped
  metric sensors when Recorder has no composite history.
- Report how many Home Assistant days were found, added and enriched, with an
  actionable Recorder or entity warning instead of a silent zero.
- Preserve a saved Hevy key when its blank password field is submitted.
- Verify paginated Hevy results against the API workout count, retry temporary
  failures and never erase stored workouts when a sync returns no data.

## 2.4.0

- Expand days-since counters to a curated library of 140 locally bundled
  Phosphor icons, with one consistent visual style and no CDN dependency.
- Add instant icon search with human-readable names and keywords.
- Rank and automatically select relevant icon suggestions while a new
  counter name is typed; manual icon choices are preserved.

## 2.3.0

- Replace the compact body-composition sparkline with a detailed,
  time-proportional chart, dated axes, reading counts and 6-month, 1-year,
  3-year and all-history views.
- Add a timeline slider for moving fixed-length chart windows through years
  of imported measurements.
- Send the complete body-measurement history to the browser instead of only
  the latest 120 samples.
- Add an authenticated Telegram backup action that ZIPs the complete `/data`
  folder and sends it as a document to the member or default bot chat.

## 2.2.1

- Add the required `repository.yaml` descriptor so current Home Assistant
  Supervisor versions refresh the custom repository and detect updates.

## 2.2.0

- Import Zepp Life export ZIPs directly from Settings for the signed-in
  household member.
- Map BODY history into weight, BMI, body fat, water, muscle and visceral-fat
  timelines while ignoring the optional private `user` folder.
- Merge imported dates and missing fields without overwriting existing HA or
  Momentum values; repeated imports are idempotent.
- Validate archives in memory without extracting files to disk.

## 2.1.0

- Prefer one legacy `bodymiscale.<person>` entity and read all body metrics
  from its attributes, while retaining separate metric sensors as a fallback.
- Import the complete history retained by Home Assistant Recorder, including
  BodyMiScale attributes, with no fixed day or local sample limit.
- Fetch every Hevy workout page and retain/return the complete workout list.
  A failed page no longer replaces stored history with a partial result.

## 2.0.0

Rebuilt as **Momentum**, implementing the Claude Design handoff.

- **Multi-user household** with 4-digit PIN login, signed `HttpOnly` session
  cookies, and an exponential lockout on failed attempts (keyed on both
  account and client IP).
- **Days-since counters** — up to three on the dashboard, best-streak
  tracking, and Telegram milestone nudges at 7/14/30/60/100/180/270/365/500/730 days.
- **Body composition** from a Mi scale via Home Assistant: weight, body fat,
  muscle, BMI, water and visceral fat, charted with a metric switcher.
  History is imported from the recorder once and then kept locally, so it
  survives the recorder's purge window.
- **Hevy sync** — hourly workout pull, computed volume, Monday-aligned week
  streak against a per-member sessions-per-week goal.
- **Affirmations** with pin-to-dashboard and a daily Telegram reminder at a
  configurable time.
- **Telegram bot management** — poll for people who message the bot, approve
  or decline them, send a test message.
- **Apple Health ingest** over a keyed webhook, for Health Auto Export.
- Habits, journal, and a step ring against a daily goal.
- Self-hosted Inter and inlined Phosphor icons — no CDN, so the app works on
  an offline LAN and under a strict CSP.

Breaking: the 1.0 data file (`dashboard.json`) is not migrated. 2.0 starts a
new store at `momentum.json`.

## 1.0.0

Initial release — habit tracking, goals, and daily reflection.
