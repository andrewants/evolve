# Evolve

A Home Assistant add-on repository containing the **Self Improvement Dashboard** —
habit tracking, goals and daily reflection, served into the Home Assistant
sidebar over ingress.

![Add-on](https://img.shields.io/badge/Home%20Assistant-add--on-41BDF5)

## Install

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store**.
2. Open the ⋮ menu → **Repositories**, and add:
   ```
   https://github.com/andrewants/evolve
   ```
3. Find **Self Improvement Dashboard** in the store and click **Install**.
4. **Start** it, enable **Show in sidebar**, and open it from the sidebar.

No ports to forward and no separate login — ingress serves the dashboard
through the Home Assistant session you are already authenticated with.

## Repository layout

```
repository.json              add-on repository manifest
evolve-dashboard/
  config.yaml                add-on manifest (options, ingress, arch)
  build.yaml                 per-architecture base images
  Dockerfile                 alpine + python3, no third-party packages
  run.sh                     bashio entrypoint; maps options to env vars
  app/
    server.py                HTTP server: static files + JSON API
    storage.py               crash-safe JSON store in /data
    ha.py                    read-only Home Assistant core API client
    www/                     the dashboard (index.html, styles.css, app.js)
  tests/                     stdlib unittest suite
```

## Development

Run the dashboard outside Home Assistant:

```bash
EVOLVE_DATA_DIR=/tmp/evolve-data \
EVOLVE_WWW_DIR="$PWD/evolve-dashboard/app/www" \
python3 evolve-dashboard/app/server.py
# http://127.0.0.1:8099
```

Run the tests:

```bash
python3 -m unittest discover -s evolve-dashboard/tests -v
```

There are no runtime or build dependencies beyond the Python standard library,
so the container builds identically on every supported architecture.

See [`evolve-dashboard/DOCS.md`](evolve-dashboard/DOCS.md) for configuration
options and the API reference.

## Design source

The dashboard UI in `app/www/` is a hand-built implementation. It was **not**
imported from the Claude Design project
`f428446a-861a-41dd-bfbe-b2e7a2b5656a` (`Self Improvement Dashboard.dc.html`)
— that project could not be read from the build environment, which has no
interactive `/design-login`. To swap in the exported design, replace the
markup and styles in `app/www/` and keep the `fetch` calls in `app.js`
resolving against the document's own directory so ingress keeps working.
