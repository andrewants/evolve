# Evolve

A Home Assistant add-on repository containing **Momentum** — a
self-improvement dashboard for the whole household.

![Add-on](https://img.shields.io/badge/Home%20Assistant-add--on-41BDF5)

Days-since counters, Mi scale body composition, Hevy gym sync,
affirmations with Telegram reminders, habits and a journal. Multi-user,
PIN-protected, and reachable over Home Assistant ingress or a Cloudflare
tunnel.

## Install

1. **Settings → Add-ons → Add-on Store**
2. ⋮ menu → **Repositories** → add:
   ```
   https://github.com/andrewants/evolve
   ```
3. Install **Momentum**, start it, enable **Show in sidebar**.
4. Open it and create the first member.

There is no prebuilt image, so Supervisor builds the container on first
install — under a minute on x86, several on a Raspberry Pi.

## Repository layout

```
repository.json              add-on repository manifest
evolve-dashboard/
  config.yaml                add-on manifest (options, ingress, ports)
  build.yaml                 per-architecture base images
  Dockerfile                 alpine + python3, no third-party packages
  run.sh                     bashio entrypoint
  app/
    server.py                routing, PIN sessions, static files
    storage.py               crash-safe multi-user JSON store in /data
    integrations.py          Home Assistant, Hevy and Telegram clients
    scheduler.py             sync, reminders, milestones
    www/                     the app (index.html, styles.css, app.js,
                             icons.js, fonts/)
  tests/                     stdlib unittest suite
```

## Development

Run it outside Home Assistant:

```bash
MOMENTUM_DATA_DIR=/tmp/momentum-data \
MOMENTUM_WWW_DIR="$PWD/evolve-dashboard/app/www" \
python3 evolve-dashboard/app/server.py
# http://127.0.0.1:8099
```

Run the tests:

```bash
python3 -m unittest discover -s evolve-dashboard/tests -v
```

No runtime or build dependencies beyond the Python standard library, so the
container builds identically on every supported architecture.

See [`evolve-dashboard/DOCS.md`](evolve-dashboard/DOCS.md) for integration
setup and the security notes for cloudflared.

## Design

The UI implements the Claude Design handoff in
`Self Improvement Dashboard.dc.html`, built on the **Nocturne** design
system. Its tokens are reproduced verbatim in `app/www/styles.css`; the
prototype's CDN dependencies (Phosphor Icons, Inter) are inlined and
self-hosted instead, so the app works offline and under a strict CSP.
