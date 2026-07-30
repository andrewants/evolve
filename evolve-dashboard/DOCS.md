# Self Improvement Dashboard

Track habits, goals and daily reflection from the Home Assistant sidebar.

## Installation

1. Add this repository to the Add-on Store (⋮ → **Repositories**):
   `https://github.com/andrewants/evolve`
2. Install **Self Improvement Dashboard**.
3. Start the add-on and enable **Show in sidebar**.

The add-on is reached through Home Assistant ingress, so it inherits your
existing session — there is no second login and no port to expose.

## Configuration

```yaml
log_level: info
week_starts_on: monday
day_rollover_hour: 4
sensors:
  - entity: sensor.steps_today
    label: Steps
  - entity: sensor.sleep_duration
    label: Sleep
```

### `log_level`

How much the add-on writes to its log. One of `trace`, `debug`, `info`,
`notice`, `warning`, `error`, `fatal`. Default `info`.

### `week_starts_on`

`monday` (default) or `sunday`. Controls the weekly pip row next to each
habit, the "this week" statistic, and the column alignment of the
consistency heatmap.

### `day_rollover_hour`

Hour (0–12) at which the dashboard starts counting a new day. Default `4`,
so a check-in at 01:30 still lands on the previous day rather than splitting
a late night across two dates.

### `sensors`

Optional list of Home Assistant entities to surface as tiles alongside the
built-in statistics. Useful for pulling in step counts, sleep duration, or
weight from an existing integration.

| Key      | Required | Description                                        |
| -------- | -------- | -------------------------------------------------- |
| `entity` | yes      | Entity ID, e.g. `sensor.steps_today`               |
| `label`  | no       | Tile label; defaults to the entity's friendly name |
| `icon`   | no       | Reserved for future use                            |

Entity states are read through the Supervisor's core proxy using the token
Home Assistant issues to the add-on, and are cached for 20 seconds. The
add-on only ever **reads** state — it never calls services or writes
entities. If an entity is missing or unavailable, its tile shows `—` rather
than failing the page.

## Data and backups

Everything is stored in a single file, `/data/dashboard.json`, inside the
add-on's persistent volume. It is written atomically (write to a temp file,
`fsync`, then rename), so an unexpected power loss cannot leave a
half-written document behind. If the file is ever unreadable, the add-on
moves it aside as `dashboard.json.corrupt` and starts fresh instead of
refusing to boot.

`/data` is included in Home Assistant add-on backups, so a normal backup
captures your full history. To export the raw data, open the add-on's
**Files** or use the Supervisor CLI.

## Using the dashboard

- **Habits** — click the circle to check a habit off for today. The pip row
  shows the current week; the caption shows progress against the habit's
  weekly target. Checking off is optimistic: it applies immediately and
  rolls back if the write fails.
- **Consistency** — 12 weeks of history, shaded by how much of that day's
  habit list you completed.
- **Goals** — set a target and a unit; step progress with `−` / `+`, or mark
  the goal complete. A due date shows the days remaining and turns amber in
  the final week, red once overdue.
- **Reflection** — a short daily note with mood and energy on a 1–5 scale.
  The 20 most recent entries are shown; 500 are retained.

## API

The same JSON API the page uses is available to scripts and automations
through ingress. All paths are relative to the add-on's ingress URL.

| Method   | Path                        | Purpose                        |
| -------- | --------------------------- | ------------------------------ |
| `GET`    | `/api/bootstrap`            | Full state plus configuration  |
| `GET`    | `/api/sensors`              | Current values of tile sensors |
| `POST`   | `/api/habits`               | Create a habit                 |
| `PUT`    | `/api/habits/{id}`          | Rename / recolour / retarget   |
| `DELETE` | `/api/habits/{id}`          | Delete a habit and its history |
| `POST`   | `/api/habits/{id}/toggle`   | Toggle a day (`{"date": …}`)   |
| `POST`   | `/api/goals`                | Create a goal                  |
| `PUT`    | `/api/goals/{id}`           | Update progress or status      |
| `DELETE` | `/api/goals/{id}`           | Delete a goal                  |
| `POST`   | `/api/journal`              | Add a reflection entry         |
| `DELETE` | `/api/journal/{id}`         | Delete an entry                |

Request bodies are JSON objects and are capped at 256 KiB. Errors come back
as `{"error": "..."}` with a 4xx status.

## Troubleshooting

**The sidebar panel is blank.** Check the add-on log. If it reports
`Web root ... does not exist`, the image did not build correctly — reinstall
the add-on.

**Sensor tiles show `—`.** The entity ID is probably wrong, or the entity is
genuinely unavailable. The tile caption shows the reason. Confirm the ID in
**Developer Tools → States**.

**"Today" looks like yesterday.** That is `day_rollover_hour` doing its job
before the configured hour. Set it to `0` for a plain midnight boundary.

**The date is wrong entirely.** The add-on follows the container timezone,
which Home Assistant sets from **Settings → System → General**.
