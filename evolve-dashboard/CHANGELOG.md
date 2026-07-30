# Changelog

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
