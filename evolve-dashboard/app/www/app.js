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

// Indexed 0..6 to match Python's weekday(), which the streak buckets use.
const WEEK_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

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
  chartSpanDays: 365,
  chartOffsetDays: 0,
  modal: null,
  editId: null,
  form: {},
  toast: null,
  data: null,
};

// ─────────────────────────────────────────────────────────────── utilities

const $ = (sel, root = document) => root.querySelector(sel);

/* ── navigation layers
 *
 * Installed to the iPhone home screen there is no browser chrome, so the
 * edge-swipe gesture is the only "back" the app has. Every sub-screen and
 * dialog therefore pushes a history entry — the swipe (and Android's back
 * button) then unwinds one layer instead of closing the app outright. The
 * pushed URL is unchanged, which keeps this safe under the ingress token
 * prefix.
 */
let layerDepth = 0;
let ignoreNextPop = 0;

function pushLayer() {
  layerDepth += 1;
  history.pushState({ momentumLayer: layerDepth }, "");
}

/** Drop the entry pushed by the matching pushLayer, if we own one. */
function popLayer() {
  if (layerDepth === 0) return;
  layerDepth -= 1;
  ignoreNextPop += 1;
  history.back();
}

/** Collapse every open layer at once — the session ended and the whole UI is
 *  about to be replaced by the login screen. */
function unwindLayers() {
  if (!layerDepth) return;
  const steps = layerDepth;
  layerDepth = 0;
  ignoreNextPop += 1;
  history.go(-steps);
}

window.addEventListener("popstate", () => {
  if (ignoreNextPop > 0) {
    ignoreNextPop -= 1;
    return;
  }
  if (!layerDepth) return;
  layerDepth -= 1;
  if (state.modal) {
    state.modal = null;
    state.editId = null;
  } else if (state.sub) {
    state.sub = null;
  }
  render();
});

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
const fmtChartDate = (iso) =>
  fromIso(iso).toLocaleDateString(undefined, { month: "short", year: "numeric" });
const fmtLong = (iso) =>
  fromIso(iso).toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
const fmtSession = (iso) =>
  fromIso(iso).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });

/* 24-hour everywhere, regardless of the device's locale. `hourCycle` rather
   than `hour12: false`, which some engines read as h24 and render midnight
   as 24:00. */
const CLOCK_OPTS = { hour: "2-digit", minute: "2-digit", hourCycle: "h23" };

/** "08:22", or null when the source recorded no time. */
function fmtClock(at) {
  if (!at) return null;
  const stamp = new Date(at);
  if (Number.isNaN(stamp.getTime())) return null;
  return stamp.toLocaleTimeString(undefined, CLOCK_OPTS);
}

/** "31 Jul, 08:22" — a date the locale orders, on a 24-hour clock. */
function fmtDateTime(at) {
  const stamp = new Date(at);
  if (Number.isNaN(stamp.getTime())) return null;
  return stamp.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    ...CLOCK_OPTS,
  });
}

/** How recent the last reading is.
 *
 * A bare date says nothing useful about a reading taken this morning, so the
 * first week is phrased in relative terms — with a clock time while it is
 * still today — and only older readings fall back to the date, by which point
 * *when* it was taken matters more than *how long ago*.
 */
function fmtUpdated(sample, today) {
  // fromIso builds local midnights, so rounding absorbs any DST hour.
  const days = Math.round((fromIso(today) - fromIso(sample.date)) / DAY_MS);
  if (days <= 0) {
    const time = fmtClock(sample.at);
    return time ? `Today ${time}` : "Today";
  }
  if (days === 1) return "Yesterday";
  if (days <= 7) return `${days} days ago`;
  return `Updated ${fmtShort(sample.date)}`;
}

function delta(current, previous, unit) {
  if (current === null || previous === null || current === undefined || previous === undefined) return "—";
  const diff = current - previous;
  return `${diff > 0 ? "+" : ""}${diff.toFixed(1)}${unit ? ` ${unit}` : ""}`;
}

function backfillMessage(result, prefix = "") {
  const base = `${prefix}found ${result.found} HA day${result.found === 1 ? "" : "s"} · ${result.added} new · ${result.updated} enriched`;
  return result.errors?.length ? `${base} · ${result.errors[0]}` : base;
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

async function uploadZeppLife(file) {
  let response;
  try {
    response = await fetch(url("/api/import/zepp-life"), {
      method: "POST",
      body: file,
      credentials: "same-origin",
      headers: { "Content-Type": "application/zip" },
    });
  } catch {
    throw new Error("Cannot reach the server");
  }
  const payload = await response.json().catch(() => ({}));
  if (response.status === 401) throw new Error("Session expired — sign in again");
  if (!response.ok) throw new Error(payload.error || `Import failed (${response.status})`);
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

function counterIconCatalog() {
  return (data().counter_icons || []).map((item) =>
    typeof item === "string"
      ? { name: item, label: item.replaceAll("-", " "), keywords: [] }
      : item
  );
}

function iconSearchTerms(text) {
  const ignored = new Set(["a", "an", "and", "day", "days", "for", "from", "my", "no", "not", "of", "since", "the", "to", "without"]);
  return String(text || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim()
    .split(/\s+/)
    .filter((term) => term && !ignored.has(term));
}

function rankCounterIcons(text, limit = null) {
  const terms = iconSearchTerms(text);
  if (!terms.length) return [];
  const phrase = terms.join(" ");
  const ranked = counterIconCatalog().map((item, order) => {
    const name = item.name.replaceAll("-", " ");
    const label = String(item.label || "").toLowerCase();
    const keywords = (item.keywords || []).map((keyword) => String(keyword).toLowerCase());
    let score = label === phrase || name === phrase ? 40 : 0;
    for (const term of terms) {
      if (label === term || name === term) score += 16;
      else if (label.startsWith(term) || name.startsWith(term)) score += 10;
      else if (label.includes(term) || name.includes(term)) score += 5;
      if (keywords.includes(term)) score += 14;
      else if (keywords.some((keyword) => keyword.startsWith(term))) score += 8;
      else if (keywords.some((keyword) => keyword.includes(term))) score += 3;
    }
    return { item, score, order };
  }).filter((entry) => entry.score > 0);
  ranked.sort((a, b) => b.score - a.score || a.order - b.order);
  const items = ranked.map((entry) => entry.item);
  return limit === null ? items : items.slice(0, limit);
}

function counterIconChoice(item, form) {
  return el("button", {
    class: `icon-choice${form.icon === item.name ? " on" : ""}`,
    type: "button",
    role: "radio",
    "aria-checked": String(form.icon === item.name),
    "aria-label": item.label,
    title: item.label,
    onclick: () => {
      form.icon = item.name;
      form.icon_touched = true;
      updateCounterIconPicker(form);
    },
  }, [
    icon(item.name),
    el("span", { text: item.label }),
  ]);
}

function counterIconResults(form) {
  const catalog = counterIconCatalog();
  const query = String(form.icon_query || "").trim();
  const suggestions = rankCounterIcons(form.name, 8);
  const matches = query ? rankCounterIcons(query) : catalog;
  const selected = catalog.find((item) => item.name === form.icon) || catalog[0];
  const children = [
    el("div", { class: "icon-picker-selection" }, [
      selected ? icon(selected.name) : null,
      el("div", {}, [
        el("div", { class: "icon-picker-selection-label", text: selected?.label || "Choose an icon" }),
        el("div", { class: "muted-sm", text: `${catalog.length} consistent local icons` }),
      ]),
    ]),
  ];

  if (!query && suggestions.length) {
    children.push(
      el("div", { class: "icon-picker-heading", text: "Suggested from the name" }),
      el("div", { class: "icon-suggestion-row", role: "radiogroup", "aria-label": "Suggested icons" },
        suggestions.map((item) => counterIconChoice(item, form))
      )
    );
  }
  children.push(
    el("div", {
      class: "icon-picker-heading",
      text: query ? `${matches.length} search result${matches.length === 1 ? "" : "s"}` : `All icons · ${catalog.length}`,
    }),
    matches.length
      ? el("div", { class: "icon-library-grid", role: "radiogroup", "aria-label": "Icon library" },
          matches.map((item) => counterIconChoice(item, form))
        )
      : el("div", { class: "empty icon-picker-empty", text: "No matching icons. Try another word." })
  );
  return children;
}

function updateCounterIconPicker(form) {
  const container = $(".icon-picker-results");
  if (container) container.replaceChildren(...counterIconResults(form));
}

function gymStats() {
  const d = data();
  return d.gym || {
    counts: new Array(WEEK_COUNT).fill(0),
    streak: 0,
    streak_since: null,
    streak_broken_week: null,
    goal_streak: 0,
    goal: d.settings.weekly_gym_goal,
    current_count: 0,
  };
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

function chartWindow(series) {
  if (!series.length) return { visible: [], maxOffset: 0 };
  const ordered = series.slice().sort((a, b) => fromIso(a.date) - fromIso(b.date));
  const firstTime = fromIso(ordered[0].date).getTime();
  const lastTime = fromIso(ordered[ordered.length - 1].date).getTime();
  const totalDays = Math.max(0, Math.round((lastTime - firstTime) / DAY_MS));
  const span = state.chartSpanDays;
  const maxOffset = span ? Math.max(0, totalDays - span) : 0;
  state.chartOffsetDays = Math.max(0, Math.min(maxOffset, state.chartOffsetDays));
  if (!span) return { visible: ordered, maxOffset, totalDays };

  const endTime = lastTime - state.chartOffsetDays * DAY_MS;
  const startTime = endTime - span * DAY_MS;
  const visible = ordered.filter((sample) => {
    const time = fromIso(sample.date).getTime();
    return time >= startTime && time <= endTime;
  });
  return { visible, maxOffset, totalDays };
}

function metricChart(series, metric) {
  if (series.length < 2) {
    return el("div", { class: "empty", text: "Not enough readings yet for a chart." });
  }
  const values = series.map((sample) => Number(sample[metric.key]));
  const min = Math.min(...values);
  const max = Math.max(...values);
  const rawRange = max - min;
  const padding = rawRange ? rawRange * 0.08 : Math.max(Math.abs(max) * 0.02, 1);
  const low = min - padding;
  const high = max + padding;
  const range = high - low;
  const left = 42;
  const right = 372;
  const top = 18;
  const bottom = 228;
  const firstTime = fromIso(series[0].date).getTime();
  const lastTime = fromIso(series[series.length - 1].date).getTime();
  const timeRange = lastTime - firstTime || DAY_MS;
  const px = (sample) => left + ((fromIso(sample.date).getTime() - firstTime) / timeRange) * (right - left);
  const py = (value) => bottom - ((value - low) / range) * (bottom - top);
  const points = series
    .map((sample) => `${px(sample).toFixed(1)},${py(Number(sample[metric.key])).toFixed(1)}`)
    .join(" ");
  const area = `M${series
    .map((sample) => `${px(sample).toFixed(1)} ${py(Number(sample[metric.key])).toFixed(1)}`)
    .join(" L")} L${right} ${bottom} L${left} ${bottom} Z`;
  const children = [];

  for (let i = 0; i <= 4; i += 1) {
    const value = high - (i / 4) * range;
    const y = top + (i / 4) * (bottom - top);
    children.push(
      svgEl("line", { x1: left, y1: y, x2: right, y2: y, class: "chart-grid" }),
      svgEl("text", { x: left - 9, y: y + 4, class: "chart-label", "text-anchor": "end" }, [
        document.createTextNode(value.toFixed(metric.key === "visceral" ? 0 : 1)),
      ])
    );
  }
  for (let i = 0; i <= 4; i += 1) {
    const time = firstTime + (i / 4) * timeRange;
    const x = left + (i / 4) * (right - left);
    const iso = toIso(new Date(time));
    children.push(
      svgEl("line", { x1: x, y1: top, x2: x, y2: bottom, class: "chart-grid chart-grid--vertical" }),
      svgEl("text", {
        x, y: 255, class: "chart-label", "text-anchor": i === 0 ? "start" : i === 4 ? "end" : "middle",
      }, [document.createTextNode(fmtChartDate(iso))])
    );
  }
  children.push(
    svgEl("path", { d: area, class: "chart-area" }),
    svgEl("polyline", { points, class: "chart-line" })
  );
  const pointStep = Math.max(1, Math.ceil(series.length / 60));
  series.forEach((sample, index) => {
    if (index % pointStep !== 0 && index !== series.length - 1) return;
    const circle = svgEl("circle", {
      cx: px(sample).toFixed(1),
      cy: py(Number(sample[metric.key])).toFixed(1),
      r: index === series.length - 1 ? 4 : 2.2,
      class: index === series.length - 1 ? "chart-point chart-point--last" : "chart-point",
    }, [
      svgEl("title", {}, [
        document.createTextNode(`${fmtChartDate(sample.date)} · ${Number(sample[metric.key]).toFixed(1)} ${metric.unit}`),
      ]),
    ]);
    children.push(circle);
  });

  return svgEl("svg", {
    class: "detail-chart",
    viewBox: "0 0 380 270",
    role: "img",
    "aria-label": `${metric.label} from ${fmtChartDate(series[0].date)} to ${fmtChartDate(series[series.length - 1].date)}`,
  }, children);
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
  const gym = gymStats();
  const counts = gym.counts;
  const goal = gym.goal;
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
        onclick: () => { pushLayer(); state.sub = "counters"; render(); },
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
          text: last ? fmtUpdated(last, d.today) : "Connect a scale entity in Settings",
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
          el("span", { class: "n", text: String(gym.streak) }),
          el("span", { class: "u", text: "weeks" }),
        ]),
        el("div", { class: "week-bars" },
          counts.map((count) =>
            el("i", {
              class: count >= goal ? "goal" : count > 0 ? "active" : "",
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
      onclick: () => { pushLayer(); state.sub = "journal"; render(); },
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
        onclick: () => { popLayer(); state.sub = null; render(); },
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
                onclick: () => { pushLayer(); state.modal = "reset"; state.editId = counter.id; render(); },
              }, [icon("arrow-counter-clockwise", { size: "18px" })]),
            ])
          )
        )
      : el("div", { class: "card", style: "padding:16px" }, [el("div", { class: "empty", text: "No counters yet." })]),
  ]);
}

function openCounterModal(counter) {
  pushLayer();
  state.modal = "counter";
  state.editId = counter ? counter.id : null;
  state.form = counter
    ? {
        name: counter.name, date: counter.date, icon: counter.icon, on_dash: !!counter.on_dash,
        icon_query: "", icon_touched: true,
      }
    : {
        name: "", date: toIso(new Date()), icon: "prohibit", on_dash: false,
        icon_query: "", icon_touched: false,
      };
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
        onclick: () => { popLayer(); state.sub = null; render(); },
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
  const last = series[series.length - 1];
  const prev = series[series.length - 2];
  const weights = metricSeries("weight");
  const lastWeight = weights[weights.length - 1];
  const window = chartWindow(series);
  const shown = window.visible;
  const shownFirst = shown[0];
  const shownLast = shown[shown.length - 1];

  const children = [
    el("div", { class: "screen-head" }, [
      el("div", { class: "h-title", text: "Body composition" }),
      el("div", {
        class: "sub",
        text: lastWeight
          ? fmtUpdated(lastWeight, d.today)
          : "Not connected — map a scale entity in Settings",
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
      el("div", { class: "chart-toolbar" }, [
        el("div", { class: "chart-ranges", role: "group", "aria-label": "Chart time range" },
          [
            [180, "6M"], [365, "1Y"], [1095, "3Y"], [0, "All"],
          ].map(([days, label]) =>
            el("button", {
              class: `chart-range${state.chartSpanDays === days ? " on" : ""}`,
              type: "button",
              text: label,
              onclick: () => {
                state.chartSpanDays = days;
                state.chartOffsetDays = 0;
                render();
              },
            })
          )
        ),
        shownFirst && shownLast
          ? el("span", {
              class: "chart-summary",
              text: `${shown.length} readings · ${fmtChartDate(shownFirst.date)} – ${fmtChartDate(shownLast.date)}`,
            })
          : null,
      ]),
      metricChart(shown, metric),
      state.chartSpanDays && window.maxOffset > 0
        ? el("div", { class: "chart-slider-wrap" }, [
            el("span", { text: "Older" }),
            el("input", {
              class: "chart-slider",
              type: "range",
              min: "0",
              max: String(window.maxOffset),
              step: "1",
              value: String(window.maxOffset - state.chartOffsetDays),
              "aria-label": "Slide chart through history",
              oninput: (event) => {
                state.chartOffsetDays = window.maxOffset - Number(event.target.value);
                render();
              },
            }),
            el("span", { text: "Latest" }),
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
                toast(backfillMessage(result));
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
  const gym = gymStats();
  const counts = gym.counts;
  const goal = gym.goal;
  const streak = gym.streak;
  const last = d.workouts[0];

  const children = [
    el("div", { class: "screen-head" }, [
      el("div", { class: "h-title", text: "Gym" }),
      el("div", { class: "sub", text: `Synced from Hevy · goal ${goal} sessions / week` }),
    ]),
    el("div", { class: "card streak-card" }, [
      el("div", { class: "row-between" }, [
        el("div", {}, [
          el("div", { class: "section-label", text: "Training streak" }),
          el("div", { style: "display:flex;align-items:baseline;gap:6px;margin-top:4px" }, [
            el("span", { class: "big", text: String(streak) }),
            el("span", { style: "font-size:13px;color:var(--color-neutral-400)", text: "weeks active" }),
          ]),
        ]),
        icon("flame", { fill: true, cls: "icon flame" }),
      ]),
      el("div", { class: "week-bars-big" },
        counts.map((count, index) =>
          el("div", {}, [
            el("div", {
              class: `bar${count >= goal ? " goal" : count > 0 ? " active" : ""}`,
              style: `height:${Math.max(6, Math.min(48, count * 11))}px`,
              title: `${count} session${count === 1 ? "" : "s"}`,
            }),
            el("span", { class: "lbl", text: index === counts.length - 1 ? "now" : `${counts.length - 1 - index}w` }),
          ])
        )
      ),
      el("div", {
        class: "muted-sm", style: "margin-top:10px",
        text: `${gym.current_count} of ${goal} sessions this week · ${
          gym.current_count >= goal ? "weekly goal reached" : `${goal - gym.current_count} to goal`
        } · goal streak ${gym.goal_streak ?? 0} week${gym.goal_streak === 1 ? "" : "s"}`,
      }),
      // A streak shorter than expected is almost always one empty week rather
      // than a miscount, so name the weeks involved and it can be checked.
      gym.streak_since
        ? el("div", {
            class: "muted-sm", style: "margin-top:4px",
            text: gym.streak_broken_week
              ? `Running since the week of ${fmtShort(gym.streak_since)} · no session logged the week of ${fmtShort(gym.streak_broken_week)}`
              : `Running since the week of ${fmtShort(gym.streak_since)} — the start of your history`,
          })
        : null,
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
          pushLayer();
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
                    pushLayer();
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
      el("label", { class: "field" }, [
        el("span", { text: "Gym week starts on" }),
        el("select", {
          class: "input",
          onchange: (event) => saveSettings({ gym_week_start: Number(event.target.value) }),
        }, WEEK_DAYS.map((day, index) =>
          el("option", {
            value: String(index),
            selected: index === (s.gym_week_start ?? 0),
            text: day,
          })
        )),
        el("small", { class: "muted-sm", text: "Match this to the week start in Hevy, or the two streaks will bucket sessions into different weeks." }),
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
        el("div", { class: "telegram-actions" }, [
          el("button", { class: "btn btn-primary", type: "submit", style: "min-height:40px", text: "Save" }),
          el("button", {
            class: "btn btn-secondary", type: "button", style: "min-height:40px", text: "Send test",
            onclick: () => guard(async () => {
              await api("/api/telegram/test", { method: "POST" });
            }, "Test message sent"),
          }),
          el("button", {
            class: "btn btn-secondary", type: "button", style: "min-height:40px", text: "Back up data",
            onclick: () => guard(async () => {
              const result = await api("/api/telegram/backup", { method: "POST" });
              const size = (result.bytes / (1024 * 1024)).toFixed(2);
              toast(`Backup sent · ${result.files} files · ${size} MB`);
            }),
          }),
        ]),
      ]),
      el("div", {
        class: "muted-sm",
        text: "Back up data sends a ZIP of the complete /data folder to your chat ID, or to the default chat.",
      }),
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
          el("div", { class: "s", text: d.gym_synced_at ? `API key · last sync ${fmtDateTime(d.gym_synced_at)}` : "API key · syncs hourly" }),
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
          onclick: () => { pushLayer(); state.modal = "health"; render(); },
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
          toast(backfillMessage(result, "Saved · "));
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

    section("Zepp Life history"),
    el("form", {
      class: "card stack-card",
      onsubmit: (event) => {
        event.preventDefault();
        const file = event.target.elements.archive.files[0];
        if (!file) {
          toast("Choose a Zepp Life export ZIP", true);
          return;
        }
        guard(async () => {
          const result = await uploadZeppLife(file);
          await refresh();
          toast(`Imported ${result.days} days · ${result.added} new · ${result.updated} enriched`);
          event.target.reset();
        });
      },
    }, [
      el("div", { class: "muted-sm", text: "Import BODY history from a Zepp Life export. Existing HA and Momentum values are preserved; missing fields are filled from Zepp." }),
      el("label", { class: "field" }, [
        el("span", { text: "Zepp Life export" }),
        el("input", {
          class: "input", name: "archive", type: "file",
          accept: ".zip,application/zip,application/x-zip-compressed",
        }),
      ]),
      el("button", { class: "btn btn-primary", type: "submit", style: "min-height:44px", text: "Import Zepp history" }),
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
          if (result.synced) toast(`Synced all ${result.synced} Hevy workouts`);
          else if (result.preserved) toast(`Hevy returned 0 · kept ${result.preserved} stored workouts`, true);
          else toast("This Hevy account has no workouts");
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
      el("div", { class: "muted-sm", text: s.hevy_configured
        ? "The saved key is retained when this field is left blank."
        : "Hevy's API requires an active Hevy Pro subscription." }),
      el("button", {
        class: "btn btn-primary", type: "submit", style: "min-height:44px",
        text: s.hevy_configured ? "Sync now" : "Save & sync",
      }),
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
          onclick: () => { pushLayer(); state.modal = "member"; state.form = { name: "", pin: "" }; render(); },
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
  const close = () => { popLayer(); state.modal = null; state.editId = null; render(); };
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
            await api(path, {
              method: state.editId ? "PUT" : "POST",
              body: { name: form.name, date: form.date, icon: form.icon, on_dash: form.on_dash },
            });
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
            oninput: (event) => {
              form.name = event.target.value;
              if (!form.icon_touched) {
                const suggested = rankCounterIcons(form.name, 1)[0];
                form.icon = suggested?.name || "prohibit";
              }
              updateCounterIconPicker(form);
            },
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
          el("input", {
            class: "input icon-search", type: "search", value: form.icon_query,
            placeholder: "Search icons — sleep, gym, money…",
            "aria-label": "Search icons",
            oninput: (event) => {
              form.icon_query = event.target.value;
              updateCounterIconPicker(form);
            },
          }),
          el("div", { class: "icon-picker-results" }, counterIconResults(form)),
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
          class: "input webhook-url", readonly: true, value: full, "aria-label": "Webhook URL",
          onclick: (event) => event.target.select(),
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
          // Switching tabs abandons an open sub-screen, so its history entry
          // goes with it — otherwise a later back would unwind a dead layer.
          if (state.sub) popLayer();
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
  unwindLayers();
  state.modal = null;
  state.editId = null;
  state.sub = null;
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

/* The shell is a fixed, full-height box, so the iOS keyboard slides over it
 * rather than resizing it and can bury whatever field has focus. visualViewport
 * is the only thing that reports the covered height; publishing it as a custom
 * property lets the scroller and the dialogs pad themselves clear of it. */
const viewport = window.visualViewport;
if (viewport) {
  const syncKeyboardInset = () => {
    const covered = window.innerHeight - viewport.height - viewport.offsetTop;
    const inset = Math.max(0, Math.round(covered));
    document.documentElement.style.setProperty("--keyboard-inset", `${inset}px`);
  };
  viewport.addEventListener("resize", syncKeyboardInset);
  viewport.addEventListener("scroll", syncKeyboardInset);
  syncKeyboardInset();
}

// Dismissing the keyboard can leave iOS holding the fixed shell scrolled up.
window.addEventListener("focusout", () => {
  if (window.scrollY || window.scrollX) window.scrollTo(0, 0);
});

// Coming back to a backgrounded tab should not show a stale "today".
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && state.phase === "app") {
    refresh().then(render).catch(() => {});
  }
});

render();
loadSession();
