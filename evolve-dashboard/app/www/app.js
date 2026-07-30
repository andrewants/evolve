/* Momentum — client.
 *
 * Reached two ways: Home Assistant ingress (which mounts the app under an
 * opaque /api/hassio_ingress/<token>/ prefix) and a Cloudflare tunnel. Every
 * request therefore resolves against the document's own directory, never the
 * server root.
 */
import ICONS from "./icons.js";

const BASE = (() => {
  const path = window.location.pathname;
  return path.endsWith("/") ? path : path.replace(/[^/]*$/, "");
})();
const url = (endpoint) => BASE + endpoint.replace(/^\//, "");

const DAY_MS = 86400000;
const WEEK_COUNT = 8;
const SPARK_W = 110;
const SPARK_H = 48;

const TABS = [
  { key: "home", label: "Home", icon: "house" },
  { key: "weight", label: "Weight", icon: "scales" },
  { key: "gym", label: "Gym", icon: "barbell" },
  { key: "affirm", label: "Affirm", icon: "sparkle" },
  { key: "settings", label: "Settings", icon: "gear-six" },
];

const state = {
  phase: "loading", // loading | setup | picking | pin | app
  users: [],
  loginUser: null,
  pin: "",
  pinError: "",
  busy: false,
  tab: "home",
  sub: null, // counters | journal
  metric: "weight",
  modal: null,
  editId: null,
  form: {},
  toast: null,
  data: null,
};

// ─────────────────────────────────────────────────────────────── utilities

const $ = (sel, root = document) => root.querySelector(sel);

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "style") node.setAttribute("style", value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of [].concat(children)) if (child) node.append(child);
  return node;
}

/** Phosphor glyph as inline SVG. `fill` picks the filled variant. */
function icon(name, { fill = false, cls = "icon", size = null } = {}) {
  const body = ICONS[(fill ? "f-" : "") + name] ?? ICONS[name];
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 256 256");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", cls);
  if (size) svg.style.fontSize = size;
  svg.innerHTML = body || "";
  return svg;
}

function svgEl(tag, props = {}, children = []) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    node.setAttribute(key, String(value));
  }
  for (const child of [].concat(children)) if (child) node.append(child);
  return node;
}

const pad = (n) => String(n).padStart(2, "0");
const toIso = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const fromIso = (iso) => {
  const [y, m, d] = String(iso).split("-").map(Number);
  return new Date(y, m - 1, d);
};

/** "Jul 30" — matches the mockup's date formatting. */
const fmtShort = (iso) =>
  fromIso(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
const fmtLong = (iso) =>
  fromIso(iso).toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
const fmtSession = (iso) =>
  fromIso(iso).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });

function delta(current, previous, unit) {
  if (current === null || previous === null || current === undefined || previous === undefined) return "—";
  const diff = current - previous;
  return `${diff > 0 ? "+" : ""}${diff.toFixed(1)}${unit ? ` ${unit}` : ""}`;
}

function toast(message, isError = false) {
  state.toast = { message, isError };
  render();
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => {
    state.toast = null;
    render();
  }, 2400);
}

// ─────────────────────────────────────────────────────────────────── api

async function api(endpoint, { method = "GET", body } = {}) {
  const init = { method, headers: {}, credentials: "same-origin" };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(url(endpoint), init);
  } catch {
    throw new Error("Cannot reach the server");
  }
  const payload = await response.json().catch(() => ({}));
  if (response.status === 401 && state.phase === "app") {
    state.phase = "picking";
    state.data = null;
    loadSession();
    throw new Error("Session expired — sign in again");
  }
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

async function guard(action, successMessage) {
  if (state.busy) return;
  state.busy = true;
  try {
    await action();
    if (successMessage) toast(successMessage);
  } catch (error) {
    toast(error.message, true);
  } finally {
    state.busy = false;
    render();
  }
}

// ────────────────────────────────────────────────────────────── selectors

const data = () => state.data;

function metricSeries(key) {
  const samples = data().weight_samples || [];
  return samples.filter((s) => s[key] !== null && s[key] !== undefined);
}

function weekCounts() {
  // Eight Monday-aligned buckets; the last one is the week in progress.
  const now = new Date();
  const monday = new Date(now);
  monday.setHours(0, 0, 0, 0);
  monday.setDate(monday.getDate() - ((monday.getDay() + 6) % 7));

  const counts = new Array(WEEK_COUNT).fill(0);
  for (const workout of data().workouts || []) {
    const diffWeeks = Math.floor((monday - fromIso(workout.date)) / (7 * DAY_MS));
    const bucket = WEEK_COUNT - 1 - diffWeeks;
    if (bucket >= 0 && bucket < WEEK_COUNT) counts[bucket] += 1;
  }
  return counts;
}

function gymStreak(counts) {
  const goal = data().settings.weekly_gym_goal;
  let streak = 0;
  for (let i = counts.length - 1; i >= 0 && counts[i] >= goal; i -= 1) streak += 1;
  return streak;
}

// ──────────────────────────────────────────────────────────────── charts

function sparkline(values) {
  if (values.length < 2) return null;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const points = values
    .map((v, i) => {
      const x = 4 + i * ((SPARK_W - 8) / (values.length - 1));
      const y = 42 - ((v - min) / range) * 36;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return svgEl(
    "svg",
    { width: SPARK_W, height: SPARK_H, viewBox: `0 0 ${SPARK_W} ${SPARK_H}`, style: "flex:none;margin-top:6px" },
    [svgEl("polyline", { points, fill: "none", stroke: "var(--color-accent)", "stroke-width": "1.5" })]
  );
}

function metricChart(values) {
  if (values.length < 2) {
    return el("div", { class: "empty", text: "Not enough readings yet for a chart." });
  }
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const px = (i) => 16 + i * (304 / (values.length - 1));
  const py = (v) => 110 - ((v - min) / range) * 90;

  const points = values.map((v, i) => `${px(i).toFixed(1)},${py(v).toFixed(1)}`).join(" ");
  const area = `M${values.map((v, i) => `${px(i).toFixed(1)} ${py(v).toFixed(1)}`).join(" L")} L320 110 L16 110 Z`;

  return svgEl(
    "svg",
    { width: "100%", height: "130", viewBox: "0 0 326 130", preserveAspectRatio: "none", style: "margin-top:8px" },
    [
      svgEl("line", { x1: 16, y1: 110, x2: 320, y2: 110, stroke: "var(--color-neutral-700)", "stroke-width": 1 }),
      svgEl("line", { x1: 16, y1: 60, x2: 320, y2: 60, stroke: "var(--color-neutral-800)", "stroke-width": 1 }),
      svgEl("line", { x1: 16, y1: 15, x2: 320, y2: 15, stroke: "var(--color-neutral-800)", "stroke-width": 1 }),
      svgEl("path", { d: area, fill: "var(--color-accent-900)", opacity: "0.5" }),
      svgEl("polyline", { points, fill: "none", stroke: "var(--color-accent)", "stroke-width": 2 }),
      svgEl("circle", {
        cx: px(values.length - 1).toFixed(1),
        cy: py(values[values.length - 1]).toFixed(1),
        r: 3.5,
        fill: "var(--color-accent-200)",
      }),
    ]
  );
}

function stepsRing(steps, goal) {
  const CIRC = 213.6;
  const offset = (CIRC * (1 - Math.min(1, steps / goal))).toFixed(1);
  return svgEl("svg", { width: 84, height: 84, viewBox: "0 0 84 84" }, [
    svgEl("circle", { cx: 42, cy: 42, r: 34, fill: "none", stroke: "var(--color-neutral-700)", "stroke-width": 6 }),
    svgEl("circle", {
      cx: 42, cy: 42, r: 34, fill: "none",
      stroke: "var(--color-accent)", "stroke-width": 6, "stroke-linecap": "round",
      "stroke-dasharray": CIRC, "stroke-dashoffset": offset, transform: "rotate(-90 42 42)",
    }),
    svgEl("text", {
      x: 42, y: 40, "text-anchor": "middle", fill: "var(--color-text)",
      "font-size": 15, "font-weight": 500, "font-family": "var(--font-heading)",
    }, [document.createTextNode(`${(steps / 1000).toFixed(1)}k`)]),
    svgEl("text", {
      x: 42, y: 54, "text-anchor": "middle", fill: "var(--color-neutral-400)", "font-size": 9,
    }, [document.createTextNode(`of ${goal / 1000}k`)]),
  ]);
}

// ───────────────────────────────────────────────────────────────── login

function screenSetup() {
  return el("div", { class: "login" }, [
    el("div", { style: "margin-top:36px" }, [
      el("div", { class: "brand-bar" }),
      el("div", { class: "brand-name", text: "Momentum" }),
      el("div", { class: "brand-sub", text: "Self-improvement dashboard" }),
    ]),
    el("form", {
      style: "margin-top:44px;display:flex;flex-direction:column;gap:14px",
      onsubmit: (event) => {
        event.preventDefault();
        const form = new FormData(event.target);
        guard(async () => {
          await api("/api/setup", {
            method: "POST",
            body: { name: form.get("name"), pin: form.get("pin") },
          });
          await loadApp();
        });
      },
    }, [
      el("div", { class: "section-label", text: "Create the first member" }),
      el("label", { class: "field" }, [
        el("span", { text: "Name" }),
        el("input", { class: "input", name: "name", maxlength: "40", required: true, autocomplete: "name" }),
      ]),
      el("label", { class: "field" }, [
        el("span", { text: "4-digit PIN" }),
        el("input", {
          class: "input", name: "pin", required: true, inputmode: "numeric",
          pattern: "\\d{4}", maxlength: "4", autocomplete: "new-password",
        }),
      ]),
      el("button", { class: "btn btn-primary", type: "submit", style: "min-height:44px", text: "Create" }),
    ]),
  ]);
}

function screenLogin() {
  const head = el("div", { style: "margin-top:36px" }, [
    el("div", { class: "brand-bar" }),
    el("div", { class: "brand-name", text: "Momentum" }),
    el("div", { class: "brand-sub", text: "Self-improvement dashboard" }),
  ]);

  if (state.phase === "picking") {
    return el("div", { class: "login" }, [
      head,
      el("div", { style: "margin-top:44px" }, [
        el("div", { class: "section-label", style: "margin-bottom:12px", text: "Who's this?" }),
        el("div", { style: "display:flex;flex-direction:column;gap:10px" },
          state.users.map((user) =>
            el("button", {
              class: "card user-btn",
              type: "button",
              onclick: () => {
                state.loginUser = user;
                state.pin = "";
                state.pinError = "";
                state.phase = "pin";
                render();
              },
            }, [
              el("div", { class: "avatar", text: user.initials }),
              el("div", { style: "flex:1;min-width:0" }, [
                el("div", { style: "font-size:15px;font-weight:500", text: user.name }),
                el("div", { class: "muted-sm", text: user.meta }),
              ]),
              icon("caret-right", { cls: "icon", size: "16px" }),
            ])
          )
        ),
      ]),
    ]);
  }

  const keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "", "0", "back"];
  return el("div", { class: "login" }, [
    head,
    el("div", { style: "margin-top:36px;display:flex;flex-direction:column;align-items:center;flex:1" }, [
      el("div", { style: "font-size:14px;color:var(--color-neutral-300)" }, [
        document.createTextNode("Enter PIN for "),
        el("span", { style: "color:var(--color-text);font-weight:500", text: state.loginUser.name }),
      ]),
      el("div", { class: "pin-dots" },
        [0, 1, 2, 3].map((i) => el("div", { class: `pin-dot${i < state.pin.length ? " pin-dot--on" : ""}` }))
      ),
      el("div", { class: "pin-error", text: state.pinError }),
      el("div", { class: "keypad" },
        keys.map((key) => {
          if (key === "") return el("div", { class: "key key--blank" });
          return el("button", {
            class: "key",
            type: "button",
            "aria-label": key === "back" ? "Delete" : key,
            onclick: () => pressKey(key),
          }, key === "back" ? [icon("backspace")] : [document.createTextNode(key)]);
        })
      ),
      el("button", {
        style: "color:var(--color-accent-300);font-size:13px;padding:14px;margin-top:12px",
        type: "button",
        text: `Not ${state.loginUser.name}? Switch user`,
        onclick: () => {
          state.phase = "picking";
          state.loginUser = null;
          state.pin = "";
          state.pinError = "";
          render();
        },
      }),
    ]),
  ]);
}

function pressKey(key) {
  if (state.busy) return;
  if (key === "back") {
    state.pin = state.pin.slice(0, -1);
    state.pinError = "";
    render();
    return;
  }
  if (state.pin.length >= 4) return;
  state.pin += key;
  state.pinError = "";
  render();

  if (state.pin.length === 4) {
    const pin = state.pin;
    const user = state.loginUser;
    state.busy = true;
    api("/api/session", { method: "POST", body: { user_id: user.id, pin } })
      .then(async () => {
        state.busy = false;
        state.pin = "";
        await loadApp();
        toast(`Welcome back, ${user.name}`);
      })
      .catch((error) => {
        state.busy = false;
        state.pin = "";
        state.pinError = error.message;
        render();
      });
  }
}

// ───────────────────────────────────────────────────────────── dashboard

function screenHome() {
  const d = data();
  const hour = new Date().getHours();
  const greeting = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";

  const dashCounters = d.counters.filter((c) => c.on_dash).slice(0, 3);
  const weights = metricSeries("weight");
  const last = weights[weights.length - 1];
  const prev = weights[weights.length - 2];
  const counts = weekCounts();
  const goal = d.settings.weekly_gym_goal;
  const pinned = d.affirmations.find((a) => a.pinned) || d.affirmations[0];
  const lastWorkout = d.workouts[0];
  const habitsDone = d.habits.filter((h) => h.done).length;

  const children = [
    el("div", { class: "dash-head" }, [
      el("div", {}, [
        el("div", { class: "date", text: fmtLong(d.today) }),
        el("div", { class: "greet", text: `${greeting}, ${d.me.name}` }),
      ]),
      el("button", {
        class: "avatar-btn", type: "button", title: "Log out",
        "aria-label": "Log out", text: d.me.initials,
        onclick: () => guard(async () => {
          await api("/api/session", { method: "DELETE" });
          state.data = null;
          state.tab = "home";
          state.sub = null;
          await loadSession();
        }),
      }),
    ]),

    el("div", { class: "row-between", style: "align-items:baseline;margin:0 4px 8px" }, [
      el("span", { class: "section-label", text: "Days since" }),
      el("button", {
        style: "color:var(--color-accent-300);font-size:12px;padding:6px",
        type: "button", text: "Manage",
        onclick: () => { state.sub = "counters"; render(); },
      }),
    ]),
  ];

  if (dashCounters.length) {
    children.push(
      el("div", { class: "counter-grid" },
        dashCounters.map((counter) =>
          el("div", { class: "card counter-card" }, [
            icon(counter.icon),
            el("div", { class: "days", text: String(counter.days) }),
            el("div", { class: "name", text: counter.name }),
          ])
        )
      )
    );
  } else {
    children.push(
      el("div", { class: "card", style: "padding:16px;margin-bottom:14px" }, [
        el("div", { class: "empty", text: "No counters pinned yet — tap Manage to add one." }),
      ])
    );
  }

  // Weight
  const weightCard = el("button", {
    class: "card weight-card", type: "button", style: "text-align:left;width:100%",
    onclick: () => { state.tab = "weight"; state.sub = null; render(); },
  }, [
    el("div", { class: "top" }, [
      el("div", {}, [
        el("div", { class: "section-label", text: "Weight" }),
        last
          ? el("div", { class: "weight-value" }, [
              el("span", { class: "big", text: last.weight.toFixed(1) }),
              el("span", { class: "unit", text: "kg" }),
              prev ? el("span", { class: "tag tag-accent", text: delta(last.weight, prev.weight, "kg") }) : null,
            ])
          : el("div", { class: "weight-value" }, [el("span", { class: "big", text: "—" })]),
        el("div", {
          class: "weight-meta",
          text: last
            ? `Updated ${fmtShort(last.date)} · Mi Scale via Home Assistant`
            : "Connect a scale entity in Settings",
        }),
      ]),
      last ? sparkline(weights.slice(-12).map((s) => s.weight)) : null,
    ]),
  ]);
  children.push(weightCard);

  // Steps + gym streak
  children.push(
    el("div", { class: "duo-grid" }, [
      el("div", { class: "card steps-card" }, [
        el("div", { class: "section-label", text: "Steps" }),
        stepsRing(d.steps || 0, d.settings.steps_goal),
      ]),
      el("button", {
        class: "card gym-card", type: "button", style: "text-align:left",
        onclick: () => { state.tab = "gym"; state.sub = null; render(); },
      }, [
        el("div", { class: "section-label", text: "Gym streak" }),
        el("div", { class: "streak" }, [
          el("span", { class: "n", text: String(gymStreak(counts)) }),
          el("span", { class: "u", text: "weeks" }),
        ]),
        el("div", { class: "week-bars" },
          counts.map((count) =>
            el("i", {
              class: count >= goal ? "on" : "",
              style: `height:${Math.max(4, Math.min(22, count * 5))}px`,
            })
          )
        ),
        el("div", {
          class: "muted-sm",
          text: lastWorkout
            ? `Last: ${lastWorkout.name} · ${fmtSession(lastWorkout.date).split(",")[0]}`
            : "No sessions synced",
        }),
      ]),
    ])
  );

  // Pinned affirmation
  if (pinned) {
    children.push(
      el("button", {
        class: "card card--accent affirm-card", type: "button", style: "text-align:left;width:100%",
        onclick: () => { state.tab = "affirm"; state.sub = null; render(); },
      }, [
        el("div", { class: "row-between" }, [
          el("span", { class: "section-label", style: "color:var(--color-accent-300)", text: "Pinned affirmation" }),
          icon("push-pin", { fill: true, size: "14px", cls: "icon" }),
        ]),
        el("div", { class: "quote", text: `“${pinned.text}”` }),
      ])
    );
  }

  // Habits
  children.push(
    el("div", { class: "card habits-card" }, [
      el("div", { class: "row-between", style: "margin-bottom:10px" }, [
        el("span", { class: "section-label", text: "Today's habits" }),
        el("span", { class: "tag tag-neutral", text: `${habitsDone}/${d.habits.length}` }),
      ]),
      d.habits.length
        ? el("div", { style: "display:flex;flex-direction:column;gap:2px" },
            d.habits.map((habit) =>
              el("button", {
                class: `habit-row${habit.done ? " done" : ""}`,
                type: "button",
                "aria-pressed": String(habit.done),
                onclick: () => toggleHabit(habit),
              }, [
                icon(habit.done ? "check-circle" : "circle", { fill: habit.done }),
                el("span", { text: habit.name }),
              ])
            )
          )
        : el("div", { class: "empty", text: "No habits yet — add some in Settings." }),
    ])
  );

  // Journal preview
  const lastEntry = d.journal[0];
  children.push(
    el("button", {
      class: "card journal-card", type: "button", style: "text-align:left;width:100%",
      onclick: () => { state.sub = "journal"; render(); },
    }, [
      el("div", { class: "row-between" }, [
        el("span", { class: "section-label", text: "Journal" }),
        icon("pencil-simple-line", { size: "16px" }),
      ]),
      el("div", {
        class: "preview",
        text: lastEntry ? lastEntry.text : "No entries yet — tap to write.",
      }),
      lastEntry ? el("div", { class: "muted-sm", style: "margin-top:6px", text: fmtShort(lastEntry.date) }) : null,
    ])
  );

  return el("div", {}, children);
}

function toggleHabit(habit) {
  // Optimistic: the tick should land instantly, then reconcile.
  habit.done = !habit.done;
  render();
  api("/api/habits/" + habit.id + "/toggle", { method: "POST", body: { date: data().today } })
    .then((payload) => {
      habit.done = payload.checkin.done;
      render();
    })
    .catch((error) => {
      habit.done = !habit.done;
      toast(error.message, true);
      render();
    });
}

// ────────────────────────────────────────────────────────── counters (sub)

function screenCounters() {
  const d = data();
  return el("div", {}, [
    el("div", { style: "display:flex;align-items:center;gap:8px;margin:8px 0 16px" }, [
      el("button", {
        class: "btn btn-ghost btn-icon", type: "button", "aria-label": "Back",
        style: "min-width:44px;min-height:44px",
        onclick: () => { state.sub = null; render(); },
      }, [icon("arrow-left", { size: "18px" })]),
      el("span", { style: "font-family:var(--font-heading);font-size:19px;font-weight:500", text: "Counters" }),
      el("button", {
        class: "btn btn-primary", type: "button", style: "margin-left:auto;min-height:40px",
        onclick: () => openCounterModal(null),
      }, [icon("plus", { size: "14px" }), document.createTextNode("Add")]),
    ]),
    el("div", {
      style: "font-size:12px;color:var(--color-neutral-400);margin:0 4px 12px",
      text: "Toggle which counters appear on the dashboard (max 3).",
    }),
    d.counters.length
      ? el("div", { style: "display:flex;flex-direction:column;gap:10px" },
          d.counters.map((counter) =>
            el("div", { class: "card counter-row" }, [
              icon(counter.icon),
              el("div", { class: "grow" }, [
                el("div", { class: "t", text: counter.name }),
                el("div", { class: "muted-sm", text: `${counter.days} days · since ${fmtShort(counter.date)}` }),
              ]),
              el("button", {
                class: `icon-btn${counter.on_dash ? " on" : ""}`, type: "button",
                title: "Show on dashboard", "aria-label": `Show ${counter.name} on dashboard`,
                onclick: () => guard(async () => {
                  await api(`/api/counters/${counter.id}/dash`, { method: "POST" });
                  await refresh();
                }),
              }, [icon("squares-four", { fill: counter.on_dash, size: "20px" })]),
              el("button", {
                class: "icon-btn", type: "button", "aria-label": `Edit ${counter.name}`,
                onclick: () => openCounterModal(counter),
              }, [icon("pencil-simple", { size: "18px" })]),
              el("button", {
                class: "icon-btn", type: "button", "aria-label": `Reset ${counter.name}`,
                onclick: () => { state.modal = "reset"; state.editId = counter.id; render(); },
              }, [icon("arrow-counter-clockwise", { size: "18px" })]),
            ])
          )
        )
      : el("div", { class: "card", style: "padding:16px" }, [el("div", { class: "empty", text: "No counters yet." })]),
  ]);
}

function openCounterModal(counter) {
  state.modal = "counter";
  state.editId = counter ? counter.id : null;
  state.form = counter
    ? { name: counter.name, date: counter.date, icon: counter.icon, on_dash: !!counter.on_dash }
    : { name: "", date: toIso(new Date()), icon: "prohibit", on_dash: false };
  render();
}

// ─────────────────────────────────────────────────────────── journal (sub)

function screenJournal() {
  const d = data();
  return el("div", {}, [
    el("div", { style: "display:flex;align-items:center;gap:8px;margin:8px 0 16px" }, [
      el("button", {
        class: "btn btn-ghost btn-icon", type: "button", "aria-label": "Back",
        style: "min-width:44px;min-height:44px",
        onclick: () => { state.sub = null; render(); },
      }, [icon("arrow-left", { size: "18px" })]),
      el("span", { style: "font-family:var(--font-heading);font-size:19px;font-weight:500", text: "Journal" }),
    ]),
    el("form", {
      class: "card", style: "padding:14px;margin-bottom:14px",
      onsubmit: (event) => {
        event.preventDefault();
        const field = event.target.elements.text;
        const text = field.value.trim();
        if (!text) return;
        guard(async () => {
          await api("/api/journal", { method: "POST", body: { text, date: d.today } });
          field.value = "";
          await refresh();
        }, "Entry saved");
      },
    }, [
      el("textarea", {
        class: "input", name: "text", rows: "3", maxlength: "8000",
        placeholder: "What's on your mind?", "aria-label": "Journal entry",
      }),
      el("div", { style: "display:flex;justify-content:flex-end;margin-top:10px" }, [
        el("button", { class: "btn btn-primary", type: "submit", style: "min-height:40px", text: "Save entry" }),
      ]),
    ]),
    d.journal.length
      ? el("div", { style: "display:flex;flex-direction:column;gap:10px" },
          d.journal.map((entry) =>
            el("div", { class: "card", style: "padding:14px 16px" }, [
              el("div", { class: "row-between" }, [
                el("span", { class: "muted-sm", text: fmtShort(entry.date) }),
                el("button", {
                  class: "icon-btn", type: "button", "aria-label": "Delete entry",
                  onclick: () => guard(async () => {
                    await api(`/api/journal/${entry.id}`, { method: "DELETE" });
                    await refresh();
                  }),
                }, [icon("trash-simple", { size: "15px" })]),
              ]),
              el("div", { style: "font-size:13px;color:var(--color-neutral-200);line-height:1.5;white-space:pre-wrap", text: entry.text }),
            ])
          )
        )
      : el("div", { class: "empty", text: "Nothing written down yet." }),
  ]);
}

// ────────────────────────────────────────────────────────────────── weight

function screenWeight() {
  const d = data();
  const metric = d.metrics.find((m) => m.key === state.metric) || d.metrics[0];
  const series = metricSeries(metric.key);
  const values = series.map((s) => s[metric.key]);
  const last = series[series.length - 1];
  const prev = series[series.length - 2];
  const weights = metricSeries("weight");
  const lastWeight = weights[weights.length - 1];

  const children = [
    el("div", { class: "screen-head" }, [
      el("div", { class: "h-title", text: "Body composition" }),
      el("div", {
        class: "sub",
        text: lastWeight
          ? `Mi Scale via Home Assistant · synced ${fmtShort(lastWeight.date)}`
          : "Mi Scale via Home Assistant · not connected",
      }),
    ]),

    el("div", { class: "card", style: "padding:16px;margin-bottom:14px" }, [
      el("div", { class: "chips" },
        d.metrics.map((m) =>
          el("button", {
            class: `chip${m.key === state.metric ? " on" : ""}`, type: "button", text: m.label,
            onclick: () => { state.metric = m.key; render(); },
          })
        )
      ),
      el("div", { class: "metric-value" }, [
        el("span", { class: "big", text: last ? last[metric.key].toFixed(1) : "—" }),
        el("span", { class: "unit", text: metric.unit }),
        last && prev
          ? el("span", { class: "tag tag-accent", text: delta(last[metric.key], prev[metric.key], metric.unit) })
          : null,
      ]),
      metricChart(values),
      series.length > 1
        ? el("div", { class: "chart-axis" }, [
            el("span", { text: fmtShort(series[0].date) }),
            el("span", { text: fmtShort(last.date) }),
          ])
        : null,
    ]),
  ];

  if (last) {
    children.push(
      el("div", { class: "metric-grid" },
        d.metrics.map((m) =>
          el("div", { class: "card metric-tile" }, [
            el("div", { class: "l", text: m.label }),
            el("div", { class: "v" }, [
              el("span", { text: last[m.key] !== null && last[m.key] !== undefined ? last[m.key].toFixed(1) : "—" }),
              el("span", { text: m.unit }),
            ]),
          ])
        )
      )
    );
  }

  const rows = weights.slice().reverse();
  children.push(el("div", { class: "section-label", style: "margin:0 4px 8px", text: "History" }));
  children.push(
    rows.length
      ? el("div", { class: "card list-card" },
          rows.slice(0, 30).map((sample, index, arr) => {
            const before = arr[index + 1];
            const diff = before ? sample.weight - before.weight : null;
            return el("div", { class: "list-row" }, [
              el("span", { class: "d", text: fmtShort(sample.date) }),
              el("span", { class: "w" }, [
                el("b", { text: `${sample.weight.toFixed(1)} kg` }),
                el("em", {
                  class: diff !== null && diff <= 0 ? "down" : "",
                  text: diff === null ? "—" : `${diff > 0 ? "+" : ""}${diff.toFixed(1)}`,
                }),
              ]),
            ]);
          })
        )
      : el("div", { class: "card", style: "padding:16px" }, [
          el("div", { class: "empty", text: "No readings yet. Map your scale entities in Settings." }),
          el("div", { style: "display:flex;justify-content:center;padding-bottom:8px" }, [
            el("button", {
              class: "btn btn-secondary", type: "button", style: "min-height:40px", text: "Import history from Home Assistant",
              onclick: () => guard(async () => {
                const result = await api("/api/sync/backfill", { method: "POST" });
                await refresh();
                toast(`Imported ${result.samples} readings`);
              }),
            }),
          ]),
        ])
  );

  return el("div", {}, children);
}

// ───────────────────────────────────────────────────────────────────── gym

function screenGym() {
  const d = data();
  const counts = weekCounts();
  const goal = d.settings.weekly_gym_goal;
  const streak = gymStreak(counts);
  const last = d.workouts[0];

  const children = [
    el("div", { class: "screen-head" }, [
      el("div", { class: "h-title", text: "Gym" }),
      el("div", { class: "sub", text: `Synced from Hevy · goal ${goal} sessions / week` }),
    ]),
    el("div", { class: "card streak-card" }, [
      el("div", { class: "row-between" }, [
        el("div", {}, [
          el("div", { class: "section-label", text: "Week streak" }),
          el("div", { style: "display:flex;align-items:baseline;gap:6px;margin-top:4px" }, [
            el("span", { class: "big", text: String(streak) }),
            el("span", { style: "font-size:13px;color:var(--color-neutral-400)", text: `weeks ≥ ${goal}` }),
          ]),
        ]),
        icon("flame", { fill: true, cls: "icon flame" }),
      ]),
      el("div", { class: "week-bars-big" },
        counts.map((count, index) =>
          el("div", {}, [
            el("div", {
              class: `bar${count >= goal ? " on" : ""}`,
              style: `height:${Math.max(6, Math.min(48, count * 11))}px`,
              title: `${count} session${count === 1 ? "" : "s"}`,
            }),
            el("span", { class: "lbl", text: index === counts.length - 1 ? "now" : `${counts.length - 1 - index}w` }),
          ])
        )
      ),
    ]),
  ];

  if (last) {
    children.push(
      el("div", { class: "card card--accent", style: "padding:16px;margin-bottom:14px" }, [
        el("div", { class: "section-label", style: "color:var(--color-accent-300);margin-bottom:8px", text: "Last session" }),
        el("div", { style: "font-size:16px;font-weight:500", text: last.name }),
        el("div", { class: "session-stats" }, [
          statBlock("Date", fmtSession(last.date)),
          statBlock("Volume", `${last.volume_kg.toLocaleString()} kg`),
          statBlock("Duration", formatDuration(last.duration_min)),
          statBlock("Exercises", String(last.exercises)),
        ]),
      ])
    );
    children.push(el("div", { class: "section-label", style: "margin:0 4px 8px", text: "Recent sessions" }));
    children.push(
      el("div", { style: "display:flex;flex-direction:column;gap:10px" },
        d.workouts.slice(0, 12).map((workout) =>
          el("div", { class: "card session-row" }, [
            el("div", { style: "min-width:0" }, [
              el("div", { class: "n", text: workout.name }),
              el("div", {
                class: "muted-sm", style: "margin-top:2px",
                text: `${fmtSession(workout.date)} · ${formatDuration(workout.duration_min)} · ${workout.exercises} exercises`,
              }),
            ]),
            el("span", { class: "tag tag-neutral", style: "flex:none", text: `${workout.volume_kg.toLocaleString()} kg` }),
          ])
        )
      )
    );
  } else {
    children.push(
      el("div", { class: "card", style: "padding:16px" }, [
        el("div", { class: "empty", text: "No sessions yet. Add your Hevy API key in Settings." }),
      ])
    );
  }

  return el("div", {}, children);
}

function statBlock(label, value) {
  return el("div", {}, [el("div", { class: "k", text: label }), el("div", { class: "v", text: value })]);
}

function formatDuration(minutes) {
  if (!minutes) return "—";
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return hours ? `${hours}h ${pad(rest)}m` : `${rest}m`;
}

// ────────────────────────────────────────────────────────────────── affirm

function screenAffirm() {
  const d = data();
  const reminder = d.settings.reminder;

  return el("div", {}, [
    el("div", { class: "row-between", style: "margin:8px 4px 16px" }, [
      el("div", { class: "h-title", text: "Affirmations" }),
      el("button", {
        class: "btn btn-primary", type: "button", style: "min-height:40px",
        onclick: () => {
          state.modal = "affirm";
          state.editId = null;
          state.form = { text: "" };
          render();
        },
      }, [icon("plus", { size: "14px" }), document.createTextNode("Add")]),
    ]),

    el("div", { class: "card", style: "padding:14px 16px;margin-bottom:14px" }, [
      el("div", { class: "row-between", style: "gap:10px" }, [
        el("div", { style: "display:flex;align-items:center;gap:10px;min-width:0" }, [
          icon("bell", { size: "18px", cls: "icon" }),
          el("div", { style: "min-width:0" }, [
            el("div", { style: "font-size:13px;font-weight:500", text: "Daily read reminder" }),
            el("div", { class: "muted-sm", text: "Sent via Telegram bot" }),
          ]),
        ]),
        el("div", { style: "display:flex;align-items:center;gap:10px;flex:none" }, [
          el("input", {
            class: "input time-input", type: "time", value: reminder.time,
            "aria-label": "Reminder time",
            onchange: (event) => saveSettings({ reminder: { ...reminder, time: event.target.value } }),
          }),
          el("button", {
            class: `switch${reminder.on ? " on" : ""}`, type: "button",
            role: "switch", "aria-checked": String(reminder.on), "aria-label": "Daily reminder",
            onclick: () => {
              const next = !reminder.on;
              saveSettings({ reminder: { ...reminder, on: next } },
                next ? `Reminder on — daily at ${reminder.time}` : "Reminder off");
            },
          }, [el("span", {})]),
        ]),
      ]),
    ]),

    d.affirmations.length
      ? el("div", { style: "display:flex;flex-direction:column;gap:10px" },
          d.affirmations.map((item) =>
            el("div", { class: `card affirm-item${item.pinned ? " card--accent" : ""}` }, [
              item.pinned ? el("div", { class: "pinned-label", text: "Pinned · shown on dashboard" }) : null,
              el("div", { class: "text", text: item.text }),
              el("div", { class: "acts" }, [
                el("button", {
                  class: `icon-btn${item.pinned ? " on" : ""}`, type: "button",
                  "aria-label": `Pin ${item.text.slice(0, 24)}`,
                  onclick: () => guard(async () => {
                    await api(`/api/affirmations/${item.id}/pin`, { method: "POST" });
                    await refresh();
                  }, "Pinned to dashboard"),
                }, [icon("push-pin", { fill: item.pinned })]),
                el("button", {
                  class: "icon-btn", type: "button", "aria-label": "Edit affirmation",
                  onclick: () => {
                    state.modal = "affirm";
                    state.editId = item.id;
                    state.form = { text: item.text };
                    render();
                  },
                }, [icon("pencil-simple")]),
                el("button", {
                  class: "icon-btn", type: "button", "aria-label": "Delete affirmation",
                  onclick: () => guard(async () => {
                    await api(`/api/affirmations/${item.id}`, { method: "DELETE" });
                    await refresh();
                  }),
                }, [icon("trash-simple")]),
              ]),
            ])
          )
        )
      : el("div", { class: "card", style: "padding:16px" }, [
          el("div", { class: "empty", text: "No affirmations yet." }),
        ]),
  ]);
}

// ──────────────────────────────────────────────────────────────── settings

function screenSettings() {
  const d = data();
  const s = d.settings;

  const section = (label) => el("div", { class: "section-label", style: "margin:0 4px 8px", text: label });

  return el("div", {}, [
    el("div", { class: "h-title", style: "margin:8px 4px 16px", text: "Settings" }),

    // ── goals
    section("Goals"),
    el("div", { class: "card stack-card" }, [
      el("label", { class: "field" }, [
        el("span", { text: "Daily step goal" }),
        el("input", {
          class: "input", type: "number", min: "1000", max: "60000", step: "500", value: String(s.steps_goal),
          onchange: (event) => saveSettings({ steps_goal: Number(event.target.value) }),
        }),
      ]),
      el("label", { class: "field" }, [
        el("span", { text: "Gym sessions per week" }),
        el("input", {
          class: "input", type: "number", min: "1", max: "7", value: String(s.weekly_gym_goal),
          onchange: (event) => saveSettings({ weekly_gym_goal: Number(event.target.value) }),
        }),
      ]),
    ]),

    // ── telegram
    section("Telegram bot"),
    el("form", {
      class: "card stack-card",
      onsubmit: (event) => {
        event.preventDefault();
        const form = new FormData(event.target);
        guard(async () => {
          const result = await api("/api/telegram", {
            method: "PUT",
            body: { token: form.get("token"), default_chat: form.get("default_chat") },
          });
          await refresh();
          toast(result.bot.ok ? `Connected to @${result.bot.detail}` : result.bot.detail || "Saved", !result.bot.ok);
        });
      },
    }, [
      el("label", { class: "field" }, [
        el("span", { text: "Bot token" }),
        el("input", {
          class: "input", name: "token", type: "password", autocomplete: "off",
          placeholder: d.telegram.configured ? "•••••••• (saved — type to replace)" : "123456:ABC-...",
        }),
      ]),
      el("label", { class: "field" }, [
        el("span", { text: "Default chat ID" }),
        el("input", { class: "input", name: "default_chat", value: d.telegram.default_chat, placeholder: "-100...", autocomplete: "off" }),
      ]),
      el("div", { class: "row-between" }, [
        d.telegram.configured
          ? el("span", { class: "tag tag-accent" }, [
              icon("plugs-connected", { fill: true, size: "12px", cls: "icon" }),
              el("span", { style: "margin-left:4px", text: "Connected" }),
            ])
          : el("span", { class: "tag tag-neutral", text: "Not configured" }),
        el("div", { style: "display:flex;gap:8px" }, [
          el("button", { class: "btn btn-primary", type: "submit", style: "min-height:40px", text: "Save" }),
          el("button", {
            class: "btn btn-secondary", type: "button", style: "min-height:40px", text: "Send test",
            onclick: () => guard(async () => {
              await api("/api/telegram/test", { method: "POST" });
            }, "Test message sent"),
          }),
        ]),
      ]),
      el("label", { class: "field" }, [
        el("span", { text: "My chat ID (overrides default for my reminders)" }),
        el("input", {
          class: "input", value: s.telegram_chat_id, placeholder: "Approve yourself below to fill this",
          onchange: (event) => saveSettings({ telegram_chat_id: event.target.value }),
        }),
      ]),
    ]),

    // ── bot users
    section("Bot users"),
    el("div", { class: "card rows-card" },
      d.bot_users.length
        ? d.bot_users.map((user) =>
            el("div", { class: "srow" }, [
              el("div", { class: "avatar avatar--sm avatar--neutral", text: (user.name || "?")[0].toUpperCase() }),
              el("div", { class: "grow" }, [
                el("div", { class: "t", text: user.handle ? `${user.name} · ${user.handle}` : user.name }),
                el("div", { class: "s", text: `chat ${user.chat_id}` }),
              ]),
              user.status === "pending"
                ? el("div", { style: "display:flex;gap:6px;flex:none" }, [
                    el("button", {
                      class: "btn btn-primary", type: "button", style: "min-height:36px;padding:6px 12px;font-size:12px",
                      text: "Approve",
                      onclick: () => guard(async () => {
                        await api(`/api/bot-users/${user.id}/approve`, { method: "POST" });
                        await refresh();
                      }, `${user.handle || user.name} approved`),
                    }),
                    el("button", {
                      class: "btn btn-ghost", type: "button", style: "min-height:36px;padding:6px 10px;font-size:12px",
                      text: "Decline",
                      onclick: () => removeBotUser(user),
                    }),
                  ])
                : el("div", { style: "display:flex;gap:6px;align-items:center;flex:none" }, [
                    el("button", {
                      class: "tag tag-accent", type: "button", title: "Use as my chat ID", text: "Approved",
                      onclick: () => saveSettings({ telegram_chat_id: user.chat_id }, "Set as my chat ID"),
                    }),
                    el("button", {
                      class: "icon-btn", type: "button", "aria-label": `Remove ${user.name}`,
                      onclick: () => removeBotUser(user),
                    }, [icon("x", { size: "15px" })]),
                  ]),
            ])
          )
        : [el("div", { class: "empty", text: "Nobody has messaged the bot yet. Send it /start, then refresh." })]
    ),

    // ── data sources
    section("Data sources"),
    el("div", { class: "card rows-card" }, [
      el("div", { class: "srow" }, [
        icon("house-line", { cls: `icon${d.integrations.ha_available ? "" : " off"}` }),
        el("div", { class: "grow" }, [
          el("div", { class: "t", text: "Home Assistant" }),
          el("div", { class: "s", text: s.entities.bodymiscale || s.entities.weight || "Mi Body Composition Scale · not mapped" }),
        ]),
        el("span", {
          class: d.integrations.ha_available && (s.entities.bodymiscale || s.entities.weight) ? "tag tag-accent" : "tag tag-neutral",
          text: d.integrations.ha_available ? ((s.entities.bodymiscale || s.entities.weight) ? "Connected" : "Set up") : "No API",
        }),
      ]),
      el("div", { class: "srow" }, [
        icon("barbell", { cls: `icon${s.hevy_configured ? "" : " off"}` }),
        el("div", { class: "grow" }, [
          el("div", { class: "t", text: "Hevy" }),
          el("div", { class: "s", text: d.gym_synced_at ? `API key · last sync ${new Date(d.gym_synced_at).toLocaleString()}` : "API key · syncs hourly" }),
        ]),
        el("span", { class: s.hevy_configured ? "tag tag-accent" : "tag tag-neutral", text: s.hevy_configured ? "Connected" : "Not set" }),
      ]),
      el("div", { class: "srow" }, [
        icon("heartbeat", { cls: "icon off" }),
        el("div", { class: "grow" }, [
          el("div", { class: "t", text: "Apple Health" }),
          el("div", { class: "s", text: "No direct API — use Health Auto Export → webhook" }),
        ]),
        el("button", {
          class: "btn btn-ghost", type: "button", style: "min-height:36px;padding:6px 10px;font-size:12px",
          text: "Set up",
          onclick: () => { state.modal = "health"; render(); },
        }),
      ]),
    ]),

    // ── entity mapping
    section("Home Assistant entities"),
    el("form", {
      class: "card stack-card",
      onsubmit: (event) => {
        event.preventDefault();
        const form = new FormData(event.target);
        const entities = {};
        for (const [key, value] of form.entries()) entities[key] = value;
        guard(async () => {
          await saveSettingsRaw({ entities });
          const result = await api("/api/sync/backfill", { method: "POST" });
          await refresh();
          toast(`Saved · imported ${result.samples} readings`);
        });
      },
    }, [
      el("label", { class: "field" }, [
        el("span", { text: "BodyMiScale entity (recommended)" }),
        el("input", {
          class: "input", name: "bodymiscale", value: s.entities.bodymiscale || "",
          placeholder: "bodymiscale.your_name", autocomplete: "off", spellcheck: "false",
        }),
        el("small", { text: "Uses weight and body-composition attributes from one entity. Separate sensors below are fallback options." }),
      ]),
      ...d.metrics.map((m) =>
        el("label", { class: "field" }, [
          el("span", { text: `${m.label} entity` }),
          el("input", {
            class: "input", name: m.key, value: s.entities[m.key] || "",
            placeholder: `sensor.mi_scale_${m.key}`, autocomplete: "off", spellcheck: "false",
          }),
        ])
      ),
      el("label", { class: "field" }, [
        el("span", { text: "Steps entity" }),
        el("input", {
          class: "input", name: "steps", value: s.entities.steps || "",
          placeholder: "sensor.phone_steps", autocomplete: "off", spellcheck: "false",
        }),
      ]),
      el("button", { class: "btn btn-primary", type: "submit", style: "min-height:44px", text: "Save & import history" }),
    ]),

    // ── hevy key
    section("Hevy"),
    el("form", {
      class: "card stack-card",
      onsubmit: (event) => {
        event.preventDefault();
        const key = new FormData(event.target).get("hevy_key");
        guard(async () => {
          await saveSettingsRaw({ hevy_key: key });
          const result = await api("/api/sync/hevy", { method: "POST" });
          await refresh();
          toast(`Synced ${result.synced} workouts`);
        });
      },
    }, [
      el("label", { class: "field" }, [
        el("span", { text: "API key" }),
        el("input", {
          class: "input", name: "hevy_key", type: "password", autocomplete: "off",
          placeholder: s.hevy_configured ? "•••••••• (saved — type to replace)" : "From hevy.com → Settings → Developer",
        }),
      ]),
      el("button", { class: "btn btn-primary", type: "submit", style: "min-height:44px", text: "Save & sync" }),
    ]),

    // ── habits
    section("Habits"),
    el("div", { class: "card rows-card" }, [
      ...d.habits.map((habit) =>
        el("div", { class: "srow" }, [
          el("div", { class: "grow" }, [el("div", { class: "t", text: habit.name })]),
          el("button", {
            class: "icon-btn", type: "button", "aria-label": `Delete ${habit.name}`,
            onclick: () => guard(async () => {
              await api(`/api/habits/${habit.id}`, { method: "DELETE" });
              await refresh();
            }),
          }, [icon("trash-simple", { size: "16px" })]),
        ])
      ),
      el("form", {
        style: "display:flex;gap:8px;padding:10px 0",
        onsubmit: (event) => {
          event.preventDefault();
          const field = event.target.elements.name;
          if (!field.value.trim()) return;
          guard(async () => {
            await api("/api/habits", { method: "POST", body: { name: field.value } });
            field.value = "";
            await refresh();
          }, "Habit added");
        },
      }, [
        el("input", { class: "input", name: "name", placeholder: "New habit", maxlength: "80", "aria-label": "New habit" }),
        el("button", { class: "btn btn-primary", type: "submit", style: "min-height:36px", text: "Add" }),
      ]),
    ]),

    // ── household
    section("Household"),
    el("div", { class: "card rows-card" }, [
      ...d.household.map((member) =>
        el("div", { class: "srow" }, [
          el("div", { class: "avatar avatar--sm", text: member.initials }),
          el("div", { class: "grow" }, [
            el("div", { class: "t", text: member.name }),
            el("div", { class: "s", text: member.meta }),
          ]),
          member.active
            ? el("span", { class: "tag tag-outline", style: "flex:none", text: "Active" })
            : el("button", {
                class: "icon-btn", type: "button", "aria-label": `Remove ${member.name}`,
                onclick: () => guard(async () => {
                  if (!window.confirm(`Remove ${member.name} and all their data?`)) return;
                  await api(`/api/household/${member.id}`, { method: "DELETE" });
                  await refresh();
                }),
              }, [icon("x", { size: "15px" })]),
        ])
      ),
      el("div", { style: "padding:10px 0" }, [
        el("button", {
          style: "color:var(--color-accent-300);font-size:13px;padding:6px 0;display:flex;align-items:center;gap:6px",
          type: "button",
          onclick: () => { state.modal = "member"; state.form = { name: "", pin: "" }; render(); },
        }, [icon("plus-circle", { size: "16px" }), document.createTextNode("Add member")]),
      ]),
    ]),

    // ── security
    section("Security"),
    el("form", {
      class: "card stack-card",
      onsubmit: (event) => {
        event.preventDefault();
        const pin = new FormData(event.target).get("pin");
        guard(async () => {
          await api(`/api/household/${d.me.id}`, { method: "PUT", body: { pin } });
          event.target.reset();
        }, "PIN changed");
      },
    }, [
      el("label", { class: "field" }, [
        el("span", { text: "Change my PIN" }),
        el("input", {
          class: "input", name: "pin", inputmode: "numeric", pattern: "\\d{4}", maxlength: "4",
          required: true, placeholder: "4 digits", autocomplete: "new-password",
        }),
      ]),
      el("button", { class: "btn btn-primary", type: "submit", style: "min-height:44px", text: "Update PIN" }),
    ]),

    el("div", { class: "muted-sm", style: "text-align:center;padding:8px 0 4px", text: `Momentum ${d.version}` }),
  ]);
}

function removeBotUser(user) {
  guard(async () => {
    await api(`/api/bot-users/${user.id}`, { method: "DELETE" });
    await refresh();
  }, `${user.handle || user.name} removed`);
}

function saveSettings(patch, message) {
  guard(async () => {
    await saveSettingsRaw(patch);
    await refresh();
  }, message);
}

async function saveSettingsRaw(patch) {
  await api("/api/settings", { method: "PUT", body: patch });
}

// ────────────────────────────────────────────────────────────────── modals

function renderModal() {
  if (!state.modal) return null;
  const close = () => { state.modal = null; state.editId = null; render(); };
  const backdrop = (inner) =>
    el("div", {
      class: "dialog-backdrop",
      onclick: (event) => { if (event.target === event.currentTarget) close(); },
    }, [inner]);

  if (state.modal === "counter") {
    const form = state.form;
    return backdrop(
      el("form", {
        class: "dialog",
        onsubmit: (event) => {
          event.preventDefault();
          if (!form.name.trim()) { toast("Give the counter a name", true); return; }
          guard(async () => {
            const path = state.editId ? `/api/counters/${state.editId}` : "/api/counters";
            await api(path, { method: state.editId ? "PUT" : "POST", body: form });
            close();
            await refresh();
          }, "Counter saved");
        },
      }, [
        el("div", { class: "dialog-title", text: state.editId ? "Edit counter" : "New counter" }),
        el("label", { class: "field" }, [
          el("span", { text: "Name" }),
          el("input", {
            class: "input", value: form.name, maxlength: "60", placeholder: "e.g. No alcohol", required: true,
            oninput: (event) => { form.name = event.target.value; },
          }),
        ]),
        el("label", { class: "field" }, [
          el("span", { text: "Start date" }),
          el("input", {
            class: "input", type: "date", value: form.date, max: toIso(new Date()),
            oninput: (event) => { form.date = event.target.value; },
          }),
        ]),
        el("div", { class: "field" }, [
          el("span", { text: "Icon" }),
          el("div", { class: "swatch-row", role: "radiogroup", "aria-label": "Icon" },
            data().counter_icons.map((name) =>
              el("button", {
                class: `swatch${form.icon === name ? " on" : ""}`, type: "button",
                role: "radio", "aria-checked": String(form.icon === name), "aria-label": name,
                onclick: () => { form.icon = name; render(); },
              }, [icon(name)])
            )
          ),
        ]),
        el("button", {
          class: `check-row${form.on_dash ? " on" : ""}`, type: "button",
          "aria-pressed": String(form.on_dash),
          onclick: () => { form.on_dash = !form.on_dash; render(); },
        }, [
          icon(form.on_dash ? "check-square" : "square", { fill: form.on_dash }),
          el("span", { text: "Show on dashboard" }),
        ]),
        el("div", { class: "dialog-actions" }, [
          el("button", { class: "btn btn-ghost", type: "button", text: "Cancel", onclick: close }),
          el("button", { class: "btn btn-primary", type: "submit", text: "Save" }),
        ]),
      ])
    );
  }

  if (state.modal === "affirm") {
    const form = state.form;
    return backdrop(
      el("form", {
        class: "dialog",
        onsubmit: (event) => {
          event.preventDefault();
          if (!form.text.trim()) return;
          guard(async () => {
            const path = state.editId ? `/api/affirmations/${state.editId}` : "/api/affirmations";
            await api(path, { method: state.editId ? "PUT" : "POST", body: { text: form.text } });
            close();
            await refresh();
          }, "Affirmation saved");
        },
      }, [
        el("div", { class: "dialog-title", text: state.editId ? "Edit affirmation" : "New affirmation" }),
        el("textarea", {
          class: "input", rows: "4", placeholder: "I am...", maxlength: "500", required: true,
          "aria-label": "Affirmation",
          oninput: (event) => { form.text = event.target.value; },
        }),
        el("div", { class: "dialog-actions" }, [
          el("button", { class: "btn btn-ghost", type: "button", text: "Cancel", onclick: close }),
          el("button", { class: "btn btn-primary", type: "submit", text: "Save" }),
        ]),
      ])
    );
  }

  if (state.modal === "reset") {
    const counter = data().counters.find((c) => c.id === state.editId);
    if (!counter) return null;
    return backdrop(
      el("div", { class: "dialog" }, [
        el("div", { class: "dialog-title", style: "margin-bottom:0", text: `Reset “${counter.name}”?` }),
        el("div", {
          class: "dialog-body",
          text: `The counter restarts from today. Current streak of ${counter.days} days will be lost.`,
        }),
        el("div", { class: "dialog-actions" }, [
          el("button", { class: "btn btn-ghost", type: "button", text: "Cancel", onclick: close }),
          el("button", {
            class: "btn btn-primary", type: "button", text: "Reset to today",
            onclick: () => guard(async () => {
              await api(`/api/counters/${counter.id}/reset`, { method: "POST" });
              close();
              await refresh();
            }, "Counter reset to today"),
          }),
        ]),
      ])
    );
  }

  if (state.modal === "member") {
    const form = state.form;
    return backdrop(
      el("form", {
        class: "dialog",
        onsubmit: (event) => {
          event.preventDefault();
          guard(async () => {
            await api("/api/household", { method: "POST", body: form });
            close();
            await refresh();
          }, "Member added");
        },
      }, [
        el("div", { class: "dialog-title", text: "Add member" }),
        el("label", { class: "field" }, [
          el("span", { text: "Name" }),
          el("input", {
            class: "input", maxlength: "40", required: true, autocomplete: "off",
            oninput: (event) => { form.name = event.target.value; },
          }),
        ]),
        el("label", { class: "field" }, [
          el("span", { text: "4-digit PIN" }),
          el("input", {
            class: "input", inputmode: "numeric", pattern: "\\d{4}", maxlength: "4", required: true,
            autocomplete: "new-password",
            oninput: (event) => { form.pin = event.target.value; },
          }),
        ]),
        el("div", { class: "dialog-actions" }, [
          el("button", { class: "btn btn-ghost", type: "button", text: "Cancel", onclick: close }),
          el("button", { class: "btn btn-primary", type: "submit", text: "Add" }),
        ]),
      ])
    );
  }

  if (state.modal === "health") {
    const path = data().integrations.health_webhook_path;
    const full = new URL(url(path.replace(/^\//, "")), window.location.href).href;
    return backdrop(
      el("div", { class: "dialog" }, [
        el("div", { class: "dialog-title", text: "Apple Health" }),
        el("div", { class: "dialog-body" }, [
          document.createTextNode(
            "Apple Health has no server API, so the phone has to push. Install "
          ),
          el("b", { text: "Health Auto Export" }),
          document.createTextNode(
            ", add a REST API automation, set the format to JSON, and point it at this URL:"
          ),
        ]),
        el("input", {
          class: "input", readonly: true, value: full, "aria-label": "Webhook URL",
          style: "font-size:12px", onclick: (event) => event.target.select(),
        }),
        el("div", { class: "muted-sm", text: "Steps and body-mass metrics are ingested. Keep this URL secret — the key in it is the only credential." }),
        el("div", { class: "dialog-actions" }, [
          el("button", { class: "btn btn-primary", type: "button", text: "Done", onclick: close }),
        ]),
      ])
    );
  }
  return null;
}

// ────────────────────────────────────────────────────────────────── render

function tabBar() {
  return el("nav", { class: "tabbar", "aria-label": "Sections" },
    TABS.map((tab) =>
      el("button", {
        class: `tab${state.tab === tab.key && !state.sub ? " on" : ""}`,
        type: "button",
        "aria-current": state.tab === tab.key && !state.sub ? "page" : null,
        onclick: () => {
          state.tab = tab.key;
          state.sub = null;
          render();
          $(".scroll")?.scrollTo(0, 0);
        },
      }, [icon(tab.icon, { fill: state.tab === tab.key && !state.sub }), el("span", { text: tab.label })])
    )
  );
}

function currentScreen() {
  if (state.sub === "counters") return screenCounters();
  if (state.sub === "journal") return screenJournal();
  switch (state.tab) {
    case "weight": return screenWeight();
    case "gym": return screenGym();
    case "affirm": return screenAffirm();
    case "settings": return screenSettings();
    default: return screenHome();
  }
}

function render() {
  const app = $("#app");
  const scrollTop = $(".scroll")?.scrollTop ?? 0;
  const children = [];

  if (state.phase === "loading") {
    children.push(el("div", { class: "empty", style: "margin-top:40vh", text: "Loading…" }));
  } else if (state.phase === "setup") {
    children.push(screenSetup());
  } else if (state.phase === "picking" || state.phase === "pin") {
    children.push(screenLogin());
  } else {
    children.push(el("main", { class: "scroll" }, [currentScreen()]));
    children.push(tabBar());
  }

  const modal = renderModal();
  if (modal) children.push(modal);
  if (state.toast) {
    children.push(
      el("div", {
        class: `toast${state.toast.isError ? " toast--error" : ""}`,
        role: "status", "aria-live": "polite", text: state.toast.message,
      })
    );
  }

  app.replaceChildren(...children);
  app.setAttribute("aria-busy", state.phase === "loading" ? "true" : "false");
  const scroller = $(".scroll");
  if (scroller && scrollTop) scroller.scrollTop = scrollTop;
}

// ──────────────────────────────────────────────────────────────────── boot

async function refresh() {
  state.data = await api("/api/bootstrap");
}

async function loadApp() {
  await refresh();
  state.phase = "app";
  render();
}

async function loadSession() {
  try {
    const session = await api("/api/session");
    if (session.setup_required) {
      state.phase = "setup";
    } else if (session.signed_in) {
      await loadApp();
      return;
    } else {
      state.users = session.users;
      state.phase = "picking";
    }
  } catch (error) {
    state.phase = "picking";
    state.users = [];
    toast(error.message, true);
  }
  render();
}

// Coming back to a backgrounded tab should not show a stale "today".
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && state.phase === "app") {
    refresh().then(render).catch(() => {});
  }
});

render();
loadSession();
