# Changelog

## 1.0.0

Initial release.

- Habit tracking with weekly targets, per-habit colours, current streak, and
  a 12-week consistency heatmap.
- Goals with progress meters, units, due dates, and completion state.
- Daily reflection entries with mood and energy on a 1–5 scale.
- Optional read-only Home Assistant sensor tiles, configured via the
  `sensors` add-on option and cached for 20 seconds.
- Crash-safe JSON storage in `/data`, included in add-on backups; an
  unreadable store is quarantined rather than blocking startup.
- Served through ingress: no exposed port, no separate authentication.
