# Momentum

A self-improvement dashboard for the whole household: days-since counters,
Mi scale body composition, Hevy gym sync, affirmations, habits and a
journal — with reminders over Telegram.

## Installation

1. Add this repository to the Add-on Store (⋮ → **Repositories**):
   `https://github.com/andrewants/evolve`
2. Install **Momentum**, start it, and enable **Show in sidebar**.
3. Open it and create the first household member (name + 4-digit PIN).

## Signing in

Momentum authenticates on its own — it does **not** rely on the Home
Assistant session. Every member has a 4-digit PIN, and the app issues a
signed, `HttpOnly` session cookie that lasts `session_days` (default 14).

This matters because of how you plan to reach it. Over ingress, Home
Assistant has already authenticated you, and the PIN is a second, mild
gate. Over a **Cloudflare tunnel the app is on the public internet with no
other protection**, and the PIN is the only thing standing in front of your
data.

A 4-digit PIN is 10,000 combinations, so the server rate limits attempts:
four free tries, then an exponential lockout (2s, 4s, 8s … capped at 15
minutes) keyed on both the account and the client IP. That makes online
guessing impractical, but it is not a substitute for a real front door.

> **If you expose Momentum with cloudflared, put Cloudflare Access in front
> of it.** A Cloudflare Access policy (email OTP, Google, whatever you
> already use) authenticates before the request ever reaches the add-on.
> The PIN then becomes the "which member am I" switch it was designed to
> be, rather than your only defence.

## Exposing via cloudflared

The add-on listens on port 8099. Ingress needs no port mapping; a tunnel
does.

1. In the add-on's **Configuration → Network**, map host port `8099`.
2. Point your tunnel's public hostname at `http://<ha-host>:8099`.
3. Add a Cloudflare Access policy for that hostname (see above).

The app sets `Secure` on its session cookie whenever it sees
`X-Forwarded-Proto: https`, which cloudflared sends, so the cookie will not
leak over plaintext.

## Configuration

Add-on options (Configuration tab):

| Option              | Default | Meaning                                          |
| ------------------- | ------- | ------------------------------------------------ |
| `log_level`         | `info`  | `trace` … `fatal`                                |
| `session_days`      | `14`    | How long a PIN sign-in lasts                     |
| `day_rollover_hour` | `4`     | Hour a new day starts, so 01:30 counts as "last night" |

Everything else is configured in the app's **Settings** tab, because it is
per-member.

### Home Assistant — Mi Body Composition Scale

Settings → **Home Assistant entities**. Prefer the single composite entity
created by legacy BodyMiScale versions, for example `bodymiscale.your_name`.
Momentum reads all metrics and all Recorder history from its attributes.

If your BodyMiScale version exposes separate sensors instead, map one entity
per metric:

| Metric       | Typical entity                       |
| ------------ | ------------------------------------ |
| Weight       | `sensor.mi_scale_weight`             |
| Body fat     | `sensor.mi_scale_body_fat`           |
| Muscle       | `sensor.mi_scale_muscle_mass`        |
| BMI          | `sensor.mi_scale_bmi`                |
| Water        | `sensor.mi_scale_water`              |
| Visceral fat | `sensor.mi_scale_visceral_fat`       |
| Steps        | `sensor.<your_phone>_steps`          |

A Mi scale creates a **separate set of entities per recognised person**, so
each household member maps their own. Only weight is required; the rest
enrich the Body composition screen.

Saving also imports all history retained by the Home Assistant recorder, so
your chart is populated immediately. After that the add-on polls every 10 minutes and
keeps its own copy — which means your history survives the recorder's purge
window.

The add-on only ever **reads** entities. It never calls services.

### Hevy

Settings → **Hevy**. Get an API key from hevy.com → Settings → Developer,
paste it, and save. Workouts sync on save and then hourly. Volume is
computed from each set's reps × weight, and the week streak counts
Monday-aligned weeks that met your sessions-per-week goal.

The Hevy API key is per member, since each person has their own account.

### Telegram

Settings → **Telegram bot**.

1. Create a bot with [@BotFather](https://t.me/botfather) and paste the
   token. Momentum validates it and shows the bot's username.
2. Have each person send `/start` to your bot.
3. They appear under **Bot users** as pending — approve the ones you
   recognise, decline the rest.
4. Tap an approved user's **Approved** chip to use their chat as *your*
   reminder destination, or set a **Default chat ID** for the household.

The bot token is stored in `/data` and is never sent back to the browser —
the UI only learns whether one is set.

What gets sent:

- **Daily affirmation** — your pinned affirmation, at the time set on the
  Affirmations tab.
- **Counter milestones** — at 7, 14, 30, 60, 100, 180, 270, 365, 500 and
  730 days. Resetting a counter re-arms them.

### Apple Health

Apple Health has no server-side API — nothing can pull from it. The phone
has to push instead.

Settings → **Data sources** → Apple Health → **Set up** shows a webhook
URL containing a per-install key. Install
[Health Auto Export](https://apps.apple.com/app/health-auto-export/id1115567069),
add a REST API automation, set the format to JSON, and point it at that
URL. Step counts and body-mass metrics are ingested.

The key in the URL is the only credential on that endpoint — treat the URL
as a secret. If you are not exposing the add-on publicly, the phone must be
on the same network as Home Assistant for this to reach it.

## Household

Settings → **Household** → Add member. Each member gets their own counters,
habits, affirmations, journal, weight history, gym data and goals — nothing
is shared except the Telegram bot and its approved chats.

Members are peers, not admins: anyone signed in can add or remove *other*
members, but only you can change your own PIN. The last remaining member
cannot be removed.

## Data and backups

Everything lives in `/data/momentum.json`, written atomically (temp file →
`fsync` → rename) and `chmod 600`. An unreadable file is moved aside as
`momentum.json.corrupt` and the add-on starts fresh rather than refusing to
boot.

`/data` is included in Home Assistant add-on backups, so a normal backup
captures your full history, PINs (hashed with PBKDF2-SHA256, 200k
iterations) and integration keys.

## API

The app's own JSON API is available to scripts, relative to the add-on URL.
All routes need the session cookie except `/api/session`, `/api/setup` and
`/api/health-webhook`.

| Method   | Path                             | Purpose                       |
| -------- | -------------------------------- | ----------------------------- |
| `GET`    | `/api/session`                   | Members, and who is signed in |
| `POST`   | `/api/session`                   | Sign in with a PIN            |
| `DELETE` | `/api/session`                   | Sign out                      |
| `GET`    | `/api/bootstrap`                 | Everything the app renders    |
| `POST`   | `/api/counters`                  | Create a counter              |
| `POST`   | `/api/counters/{id}/reset`       | Reset to today, keeping best  |
| `POST`   | `/api/counters/{id}/dash`        | Toggle dashboard slot (max 3) |
| `POST`   | `/api/affirmations/{id}/pin`     | Pin to the dashboard          |
| `POST`   | `/api/habits/{id}/toggle`        | Tick a habit for a day        |
| `POST`   | `/api/journal`                   | Add an entry                  |
| `PUT`    | `/api/settings`                  | Goals, reminder, entities     |
| `POST`   | `/api/telegram/test`             | Send a test message           |
| `POST`   | `/api/sync/hevy`                 | Sync workouts now             |
| `POST`   | `/api/sync/backfill`             | Import weight history from HA |
| `POST`   | `/api/health-webhook?key=…`      | Apple Health ingest           |

Bodies are JSON objects capped at 512 KiB. Errors are `{"error": "..."}`.

## Troubleshooting

**Weight card says "Connect a scale entity".** Map at least the weight
entity in Settings → Home Assistant entities, then save.

**Chart is empty but the current weight shows.** You have one reading. Hit
**Save & import history** to backfill from the recorder, or wait for more
weigh-ins.

**Hevy says "invalid api key".** The key is wrong or revoked — regenerate
it at hevy.com → Settings → Developer.

**Nobody appears under Bot users.** They must message the bot first, and
the token must be saved. Polling runs every 60 seconds. Note that Telegram
only retains updates for 24 hours.

**Reminders never arrive.** Check the reminder toggle is on, that you have
a pinned affirmation, and that either your chat ID or the default chat is
set to an approved chat.

**Locked out by the PIN throttle.** Wait for the delay shown, or restart
the add-on — the throttle is in memory only.

**"Today" looks like yesterday.** That is `day_rollover_hour` before the
configured hour. Set it to `0` for a plain midnight boundary. The add-on
follows the container timezone, which Home Assistant sets from
**Settings → System → General**.
