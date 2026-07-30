/* Self Improvement Dashboard — client.
 *
 * Home Assistant ingress mounts this page under an opaque path prefix
 * (/api/hassio_ingress/<token>/), so every request is resolved against the
 * document's own directory rather than the server root.
 */
(() => {
  'use strict';

  const BASE = (() => {
    const path = window.location.pathname;
    return path.endsWith('/') ? path : path.replace(/[^/]*$/, '');
  })();

  const url = (endpoint) => BASE + endpoint.replace(/^\//, '');

  const COLORS = ['iris', 'violet', 'aqua', 'amber', 'rose', 'lime'];
  const DAY_MS = 86400000;
  const HEATMAP_WEEKS = 12;
  const SENSOR_POLL_MS = 30000;

  const store = {
    habits: [],
    checkins: {},
    goals: [],
    journal: [],
    config: { today: isoToday(), week_starts_on: 'monday', sensors: [], ha_available: false },
    sensors: [],
    draft: { mood: 3, energy: 3 },
  };

  const $ = (id) => document.getElementById(id);

  // ----------------------------------------------------------------- dates

  function isoToday() {
    const now = new Date();
    return toIso(now);
  }

  function toIso(date) {
    const y = date.getFullYear();
    const m = String(date.getMonth() + 1).padStart(2, '0');
    const d = String(date.getDate()).padStart(2, '0');
    return `${y}-${m}-${d}`;
  }

  function fromIso(iso) {
    const [y, m, d] = iso.split('-').map(Number);
    return new Date(y, m - 1, d);
  }

  function shiftIso(iso, days) {
    return toIso(new Date(fromIso(iso).getTime() + days * DAY_MS));
  }

  /** Monday-or-Sunday-aligned start of the week containing `iso`. */
  function weekStart(iso) {
    const date = fromIso(iso);
    const dow = date.getDay(); // 0 = Sunday
    const offset = store.config.week_starts_on === 'sunday' ? dow : (dow + 6) % 7;
    return shiftIso(iso, -offset);
  }

  function formatLongDate(iso) {
    return fromIso(iso).toLocaleDateString(undefined, {
      weekday: 'long',
      day: 'numeric',
      month: 'long',
    });
  }

  function formatShortDate(iso) {
    return fromIso(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
  }

  function daysUntil(iso) {
    return Math.round((fromIso(iso) - fromIso(store.config.today)) / DAY_MS);
  }

  // ------------------------------------------------------------------- dom

  function el(tag, props = {}, children = []) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key === 'html') node.innerHTML = value;
      else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? '' : String(value));
    }
    for (const child of [].concat(children)) {
      if (child) node.append(child);
    }
    return node;
  }

  function icon(path) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 16 16');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('aria-hidden', 'true');
    const shape = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    shape.setAttribute('d', path);
    svg.append(shape);
    return svg;
  }

  const CHECK_PATH = 'M3.5 8.5 6.5 11.5 12.5 4.5';

  let toastTimer;
  function toast(message, isError = false) {
    const node = $('toast');
    node.textContent = message;
    node.classList.toggle('toast--error', isError);
    node.classList.add('toast--on');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => node.classList.remove('toast--on'), 2600);
  }

  // ------------------------------------------------------------------- api

  async function request(endpoint, options = {}) {
    const init = { headers: {}, ...options };
    if (init.body !== undefined && typeof init.body !== 'string') {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(init.body);
    }

    let response;
    try {
      response = await fetch(url(endpoint), init);
    } catch {
      throw new Error('Cannot reach the add-on');
    }

    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.error || `Request failed (${response.status})`);
    }
    return payload;
  }

  // ------------------------------------------------------------- selectors

  const activeHabits = () => store.habits.filter((habit) => !habit.archived);

  const isDone = (habitId, iso) => (store.checkins[habitId] || []).includes(iso);

  function completionOn(iso) {
    const habits = activeHabits();
    if (!habits.length) return { done: 0, total: 0, ratio: 0 };
    const done = habits.filter((habit) => isDone(habit.id, iso)).length;
    return { done, total: habits.length, ratio: done / habits.length };
  }

  /** Consecutive days with at least one check-in, ending today or yesterday. */
  function currentStreak() {
    const today = store.config.today;
    let cursor = completionOn(today).done > 0 ? today : shiftIso(today, -1);
    let streak = 0;
    // A year of history is plenty and keeps this bounded.
    for (let i = 0; i < 366; i += 1) {
      if (completionOn(cursor).done === 0) break;
      streak += 1;
      cursor = shiftIso(cursor, -1);
    }
    return streak;
  }

  function weekProgress() {
    const start = weekStart(store.config.today);
    const habits = activeHabits();
    const target = habits.reduce((sum, habit) => sum + (habit.target_per_week || 7), 0);
    let done = 0;
    for (let i = 0; i < 7; i += 1) {
      const iso = shiftIso(start, i);
      done += habits.filter((habit) => isDone(habit.id, iso)).length;
    }
    return { done, target, ratio: target ? Math.min(1, done / target) : 0 };
  }

  // --------------------------------------------------------------- renders

  function renderMasthead() {
    const hour = new Date().getHours();
    const greeting = hour < 5 ? 'Still up' : hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening';
    $('greeting').textContent = greeting;
    $('today-label').textContent = formatLongDate(store.config.today);
  }

  function statCard({ label, value, unit, foot, offline }) {
    return el('div', { class: `stat${offline ? ' stat--offline' : ''}` }, [
      el('span', { class: 'stat__label', text: label }),
      el('span', { class: 'stat__value numeric' }, [
        document.createTextNode(value),
        unit
          ? el('span', {
              class: /^[a-z]/i.test(unit) ? null : 'unit--symbol',
              text: unit,
            })
          : null,
      ]),
      foot ? el('span', { class: 'stat__foot', text: foot }) : null,
    ]);
  }

  function renderStats() {
    const today = completionOn(store.config.today);
    const week = weekProgress();
    const streak = currentStreak();
    const openGoals = store.goals.filter((goal) => !goal.done).length;

    const cards = [
      statCard({
        label: 'Today',
        value: `${today.done}/${today.total}`,
        foot: today.total && today.done === today.total ? 'All clear' : 'habits completed',
      }),
      statCard({
        label: 'Streak',
        value: String(streak),
        unit: streak === 1 ? 'day' : 'days',
        foot: streak ? 'keep it going' : 'start one today',
      }),
      statCard({
        label: 'This week',
        value: String(Math.round(week.ratio * 100)),
        unit: '%',
        foot: `${week.done} of ${week.target} check-ins`,
      }),
      statCard({
        label: 'Goals',
        value: String(openGoals),
        foot: `${store.goals.length - openGoals} completed`,
      }),
    ];

    for (const sensor of store.sensors) {
      cards.push(
        statCard({
          label: sensor.label || sensor.entity_id,
          value: sensor.ok ? String(sensor.state) : '—',
          unit: sensor.ok ? sensor.unit : '',
          foot: sensor.ok ? sensor.entity_id : sensor.reason || 'unavailable',
          offline: !sensor.ok,
        })
      );
    }

    $('stats').replaceChildren(...cards);
  }

  function renderHabits() {
    const list = $('habits');
    const habits = activeHabits();
    const today = store.config.today;

    if (!habits.length) {
      list.replaceChildren(el('li', { class: 'empty', text: 'No habits yet — add your first one above.' }));
      $('today-progress').textContent = '';
      return;
    }

    const start = weekStart(today);
    const nodes = habits.map((habit) => {
      const done = isDone(habit.id, today);

      const pips = [];
      for (let i = 0; i < 7; i += 1) {
        const iso = shiftIso(start, i);
        pips.push(
          el('span', {
            class: `pip${isDone(habit.id, iso) ? ' pip--done' : ''}${iso === today ? ' pip--today' : ''}`,
          })
        );
      }

      const weekCount = pips.filter((pip) => pip.classList.contains('pip--done')).length;

      return el('li', { class: `habit color-${habit.color}${done ? ' habit--done' : ''}` }, [
        el(
          'button',
          {
            class: 'check',
            type: 'button',
            'aria-pressed': String(done),
            'aria-label': `${done ? 'Undo' : 'Complete'} ${habit.name}`,
            'data-toggle': habit.id,
          },
          [icon(CHECK_PATH)]
        ),
        el('div', { class: 'habit__name' }, [
          document.createTextNode(habit.name),
          el('small', { text: `${weekCount}/${habit.target_per_week} this week` }),
        ]),
        el('div', { class: 'habit__week', 'aria-hidden': 'true' }, pips),
        el(
          'button',
          {
            class: 'btn btn--ghost btn--danger',
            type: 'button',
            'aria-label': `Delete ${habit.name}`,
            'data-delete-habit': habit.id,
            text: '×',
          }
        ),
      ]);
    });

    list.replaceChildren(...nodes);

    const { done, total } = completionOn(today);
    $('today-progress').textContent = `${done} of ${total} done`;
  }

  function renderHeatmap() {
    const container = $('heatmap');
    const today = store.config.today;
    const start = weekStart(shiftIso(today, -(HEATMAP_WEEKS - 1) * 7));
    const cells = [];

    for (let i = 0; i < HEATMAP_WEEKS * 7; i += 1) {
      const iso = shiftIso(start, i);
      const { done, total, ratio } = completionOn(iso);
      const level = ratio === 0 ? 0 : Math.min(4, Math.ceil(ratio * 4));
      cells.push(
        el('span', {
          class: 'heatmap__cell',
          'data-level': String(level),
          'data-future': iso > today ? '1' : null,
          title: `${formatShortDate(iso)} — ${done}/${total}`,
        })
      );
    }

    container.replaceChildren(...cells);
  }

  function renderGoals() {
    const list = $('goals');
    if (!store.goals.length) {
      list.replaceChildren(el('li', { class: 'empty', text: 'No goals yet. What are you working towards?' }));
      return;
    }

    const nodes = store.goals.map((goal) => {
      const ratio = goal.target > 0 ? Math.min(1, goal.current / goal.target) : 0;
      const percent = Math.round(ratio * 100);

      let dueNode = null;
      if (goal.due) {
        const left = daysUntil(goal.due);
        const tone = left < 0 ? ' goal__due--past' : left <= 7 ? ' goal__due--soon' : '';
        const label =
          left < 0 ? `${Math.abs(left)}d overdue` : left === 0 ? 'Due today' : `${left}d left`;
        dueNode = el('span', { class: `goal__due${tone}`, text: `${label} · ${formatShortDate(goal.due)}` });
      }

      return el('li', { class: `goal${goal.done ? ' goal--done' : ''}` }, [
        el('div', { class: 'goal__top' }, [
          el('span', { class: 'goal__title', text: goal.title }),
          el('span', {
            class: 'goal__count numeric',
            text: `${trim(goal.current)} / ${trim(goal.target)}${goal.unit ? ` ${goal.unit}` : ''}`,
          }),
        ]),
        goal.notes ? el('p', { class: 'goal__notes', text: goal.notes }) : null,
        el('div', { class: 'meter', role: 'progressbar', 'aria-valuenow': String(percent), 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-label': `${goal.title} progress` }, [
          el('div', { class: 'meter__fill', style: `width:${percent}%` }),
        ]),
        el('div', { class: 'goal__foot' }, [
          dueNode || el('span', { class: 'goal__due', text: `${percent}% complete` }),
          el('button', { class: 'btn btn--ghost', type: 'button', 'data-goal-step': goal.id, 'data-step': '-1', text: '−' }),
          el('button', { class: 'btn btn--ghost', type: 'button', 'data-goal-step': goal.id, 'data-step': '1', text: '+' }),
          el('button', {
            class: 'btn btn--ghost',
            type: 'button',
            'data-goal-done': goal.id,
            text: goal.done ? 'Reopen' : 'Complete',
          }),
          el('button', { class: 'btn btn--ghost btn--danger', type: 'button', 'data-delete-goal': goal.id, text: '×', 'aria-label': `Delete ${goal.title}` }),
        ]),
      ]);
    });

    list.replaceChildren(...nodes);
  }

  function trim(value) {
    return Number.isInteger(value) ? String(value) : String(Math.round(value * 100) / 100);
  }

  function renderScales() {
    for (const group of document.querySelectorAll('[data-scale]')) {
      const name = group.dataset.scale;
      const dots = [];
      for (let i = 1; i <= 5; i += 1) {
        dots.push(
          el('button', {
            class: 'scale__dot',
            type: 'button',
            role: 'radio',
            'aria-checked': String(store.draft[name] === i),
            'aria-label': `${name} ${i} of 5`,
            'data-scale-set': name,
            'data-value': String(i),
            text: String(i),
          })
        );
      }
      group.replaceChildren(...dots);
    }
  }

  function renderEntries() {
    const list = $('entries');
    if (!store.journal.length) {
      list.replaceChildren(el('li', { class: 'empty', text: 'Nothing written down yet.' }));
      return;
    }

    const nodes = store.journal.slice(0, 20).map((entry) =>
      el('li', { class: 'entry' }, [
        el('div', { class: 'entry__head' }, [
          el('span', { class: 'entry__date', text: formatShortDate(entry.date) }),
          el('span', { text: `mood ${entry.mood}/5` }),
          el('span', { text: `energy ${entry.energy}/5` }),
          el('button', {
            class: 'btn btn--ghost btn--danger',
            type: 'button',
            style: 'margin-left:auto',
            'data-delete-entry': entry.id,
            text: '×',
            'aria-label': 'Delete entry',
          }),
        ]),
        entry.text ? el('p', { class: 'entry__text', text: entry.text }) : null,
      ])
    );

    list.replaceChildren(...nodes);
  }

  function renderAll() {
    renderMasthead();
    renderStats();
    renderHabits();
    renderHeatmap();
    renderGoals();
    renderEntries();
  }

  // -------------------------------------------------------------- mutations

  async function guard(action) {
    try {
      await action();
    } catch (error) {
      toast(error.message, true);
    }
  }

  async function toggleHabit(habitId) {
    const iso = store.config.today;
    const days = new Set(store.checkins[habitId] || []);
    const wasDone = days.has(iso);

    // Optimistic: the check should feel instant, then reconcile.
    wasDone ? days.delete(iso) : days.add(iso);
    store.checkins[habitId] = [...days].sort();
    renderAll();

    try {
      const result = await request(`/api/habits/${habitId}/toggle`, {
        method: 'POST',
        body: { date: iso },
      });
      const confirmed = new Set(store.checkins[habitId] || []);
      result.done ? confirmed.add(iso) : confirmed.delete(iso);
      store.checkins[habitId] = [...confirmed].sort();
    } catch (error) {
      const reverted = new Set(store.checkins[habitId] || []);
      wasDone ? reverted.add(iso) : reverted.delete(iso);
      store.checkins[habitId] = [...reverted].sort();
      toast(error.message, true);
    }
    renderAll();
  }

  async function refreshSensors() {
    if (!store.config.sensors.length) return;
    try {
      const payload = await request('/api/sensors');
      store.sensors = payload.sensors || [];
      renderStats();
    } catch {
      // A transient core hiccup should not disturb the page.
    }
  }

  // ---------------------------------------------------------------- events

  function openDialog(id) {
    const dialog = $(id);
    dialog.querySelector('form').reset();
    if (id === 'habit-dialog') renderSwatches();
    dialog.showModal();
    dialog.querySelector('input')?.focus();
  }

  let pendingColor = COLORS[0];

  function renderSwatches() {
    const nodes = COLORS.map((name) =>
      el('button', {
        class: `swatch color-${name}`,
        type: 'button',
        role: 'radio',
        'aria-checked': String(name === pendingColor),
        'aria-label': name,
        'data-color': name,
      })
    );
    $('habit-colors').replaceChildren(...nodes);
  }

  function wireEvents() {
    $('add-habit').addEventListener('click', () => {
      pendingColor = COLORS[store.habits.length % COLORS.length];
      openDialog('habit-dialog');
    });
    $('add-goal').addEventListener('click', () => openDialog('goal-dialog'));

    for (const dialog of document.querySelectorAll('dialog')) {
      dialog.addEventListener('click', (event) => {
        if (event.target.matches('[data-close]')) dialog.close();
      });
    }

    $('habit-colors').addEventListener('click', (event) => {
      const swatch = event.target.closest('[data-color]');
      if (!swatch) return;
      pendingColor = swatch.dataset.color;
      renderSwatches();
    });

    $('habit-form').addEventListener('submit', (event) => {
      const data = new FormData(event.target);
      const name = String(data.get('name') || '').trim();
      if (!name) return;
      guard(async () => {
        const payload = await request('/api/habits', {
          method: 'POST',
          body: {
            name,
            color: pendingColor,
            target_per_week: Number(data.get('target_per_week')),
          },
        });
        store.habits.push(payload.habit);
        renderAll();
        toast('Habit added');
      });
    });

    $('goal-form').addEventListener('submit', (event) => {
      const data = new FormData(event.target);
      const title = String(data.get('title') || '').trim();
      if (!title) return;
      guard(async () => {
        const payload = await request('/api/goals', {
          method: 'POST',
          body: {
            title,
            current: Number(data.get('current')) || 0,
            target: Number(data.get('target')) || 100,
            unit: String(data.get('unit') || ''),
            due: String(data.get('due') || '') || null,
            notes: String(data.get('notes') || ''),
          },
        });
        store.goals.push(payload.goal);
        renderGoals();
        renderStats();
        toast('Goal added');
      });
    });

    $('journal-form').addEventListener('submit', (event) => {
      event.preventDefault();
      const text = $('journal-text').value.trim();
      guard(async () => {
        const payload = await request('/api/journal', {
          method: 'POST',
          body: {
            date: store.config.today,
            text,
            mood: store.draft.mood,
            energy: store.draft.energy,
          },
        });
        store.journal.unshift(payload.entry);
        $('journal-text').value = '';
        renderEntries();
        toast('Entry saved');
      });
    });

    document.addEventListener('click', (event) => {
      const target = event.target.closest('[data-toggle], [data-delete-habit], [data-goal-step], [data-goal-done], [data-delete-goal], [data-delete-entry], [data-scale-set]');
      if (!target) return;
      const data = target.dataset;

      if (data.toggle) {
        toggleHabit(data.toggle);
      } else if (data.deleteHabit) {
        const habit = store.habits.find((item) => item.id === data.deleteHabit);
        if (!habit || !window.confirm(`Delete “${habit.name}” and its history?`)) return;
        guard(async () => {
          await request(`/api/habits/${habit.id}`, { method: 'DELETE' });
          store.habits = store.habits.filter((item) => item.id !== habit.id);
          delete store.checkins[habit.id];
          renderAll();
        });
      } else if (data.goalStep) {
        const goal = store.goals.find((item) => item.id === data.goalStep);
        if (!goal) return;
        const step = Number(data.step) * Math.max(1, Math.round(goal.target / 100));
        const next = Math.max(0, goal.current + step);
        guard(async () => {
          const payload = await request(`/api/goals/${goal.id}`, {
            method: 'PUT',
            body: { current: next },
          });
          Object.assign(goal, payload.goal);
          renderGoals();
        });
      } else if (data.goalDone) {
        const goal = store.goals.find((item) => item.id === data.goalDone);
        if (!goal) return;
        guard(async () => {
          const payload = await request(`/api/goals/${goal.id}`, {
            method: 'PUT',
            body: { done: !goal.done },
          });
          Object.assign(goal, payload.goal);
          renderGoals();
          renderStats();
        });
      } else if (data.deleteGoal) {
        guard(async () => {
          await request(`/api/goals/${data.deleteGoal}`, { method: 'DELETE' });
          store.goals = store.goals.filter((item) => item.id !== data.deleteGoal);
          renderGoals();
          renderStats();
        });
      } else if (data.deleteEntry) {
        guard(async () => {
          await request(`/api/journal/${data.deleteEntry}`, { method: 'DELETE' });
          store.journal = store.journal.filter((item) => item.id !== data.deleteEntry);
          renderEntries();
        });
      } else if (data.scaleSet) {
        store.draft[data.scaleSet] = Number(data.value);
        renderScales();
      }
    });

    // Keep the displayed day honest if the tab is left open overnight.
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) boot(true);
    });
  }

  // ------------------------------------------------------------------ boot

  async function boot(silent = false) {
    try {
      const payload = await request('/api/bootstrap');
      store.habits = payload.state.habits || [];
      store.checkins = payload.state.checkins || {};
      store.goals = payload.state.goals || [];
      store.journal = payload.state.journal || [];
      store.config = { ...store.config, ...payload.config };
      renderAll();
      $('shell').setAttribute('aria-busy', 'false');
      refreshSensors();
    } catch (error) {
      if (!silent) {
        $('greeting').textContent = 'Dashboard unavailable';
        $('today-label').textContent = error.message;
      }
      toast(error.message, true);
    }
  }

  renderScales();
  wireEvents();
  boot();
  setInterval(refreshSensors, SENSOR_POLL_MS);
})();
