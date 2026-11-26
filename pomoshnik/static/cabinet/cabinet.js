"use strict";

function el(tag, classes = "", ...children) {
  const node = document.createElement(tag);
  node.className = classes;
  for (const child of children.flat()) {
    if (child == null) continue;
    if (child instanceof Node) node.append(child);
    else if (!node.hasChildNodes()) node.textContent = String(child);
    else node.append(document.createTextNode(String(child)));
  }
  return node;
}

function icon(name) {
  const svg = document.querySelector(".brand-mark svg").cloneNode(true);
  svg.querySelector("use").setAttribute("href", "#i-" + name);
  return svg;
}

function button(label, onClick, classes = "") {
  const b = el("button", classes, label);
  b.type = "button";
  b.addEventListener("click", onClick);
  return b;
}

function toggle(label, active, onClick) {
  const b = button(label, onClick);
  b.setAttribute("aria-pressed", String(active));
  return b;
}

function card(title, symbol, ...body) {
  return el("article", "card", el("h2", "card-heading", icon(symbol), title), ...body);
}

const hint = text => el("p", "muted", text);
const stack = (...items) => el("div", "stack", ...items);
const badge = text => el("span", "badge", text);
const field = (label, input) => el("label", "field", el("span", "", label), input);
const readable = value => (value || "—").replaceAll("_", " ");
const count = items => el("span", "count", items.length);
const meta = (...parts) => el("div", "meta", parts.map(part => part instanceof Node ? part : el("span", "", part)));

function fold(title, ...body) {
  return el("details", "fold", el("summary", "", title), ...body);
}

function rows(items, render, empty) {
  return items.length ? el("div", "rows", items.map(render)) : hint(empty);
}

function bullets(items, empty) {
  return items.length ? el("ul", "bullets", items.map(s => el("li", "", s))) : hint(empty);
}

function dateOf(iso) {
  return new Date(iso.slice(0, 10) + "T00:00:00Z");
}

function dateLabel(iso, options = {day: "numeric", month: "short"}) {
  if (!iso) return "—";
  return dateOf(iso).toLocaleDateString("ru-RU", {...options, timeZone: "UTC"});
}

const time = iso => iso ? iso.slice(11, 16) : "";
const interval = item => time(item.start) + "–" + time(item.end);
const stamp = iso => iso ? [dateLabel(iso), time(iso)].filter(Boolean).join(", ") : "—";

async function api(url, method = "GET", body) {
  const options = {method, headers: {Accept: "application/json"}, cache: "no-store"};
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(url, options);
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(typeof error.detail === "string" ? error.detail : "Ошибка запроса (" + response.status + ")");
  }
  return response.json();
}

// Each read owns its mount. A late response cannot replace a newer selection.
async function load(host, render) {
  const mount = el("div", "content", hint("Загрузка…"));
  host.replaceChildren(mount);
  try {
    await render(mount);
  } catch (error) {
    mount.replaceChildren(el("p", "error", error.message), button("Повторить", () => load(host, render)));
  }
}

let noticeTimer;
function announce(text) {
  const notice = document.querySelector("#notice");
  notice.textContent = text;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { notice.textContent = ""; }, 4500);
}

function feedback() {
  const node = el("p", "error");
  node.setAttribute("role", "alert");
  return node;
}

async function submitAction(scope, errorBox, action) {
  if (scope.hasAttribute("aria-busy")) return;
  scope.setAttribute("aria-busy", "true");
  const controls = scope.querySelectorAll("button:enabled, input:enabled, textarea:enabled, select:enabled");
  controls.forEach(control => { control.disabled = true; });
  errorBox.textContent = "";
  try {
    await action();
  } catch (error) {
    errorBox.textContent = error.message;
  } finally {
    controls.forEach(control => { control.disabled = false; });
    scope.removeAttribute("aria-busy");
  }
}

function formSubmit(form, label, onSubmit) {
  const b = el("button", "primary", label);
  b.type = "submit";
  form.addEventListener("submit", event => {
    event.preventDefault();
    onSubmit();
  });
  return b;
}

function metricCards(cards) {
  const symbols = ["mail", "check", "clock", "file", "edit", "chart"];
  return el("div", "metrics", Object.entries(cards).map(([label, value], i) =>
    el("div", "metric", icon(symbols[i] || "chart"),
      el("strong", "", typeof value === "number" ? value.toLocaleString("ru-RU", {maximumFractionDigits: 5}) : value),
      el("span", "", label))));
}

function taskRow(t) {
  return el("div", "row", el("strong", "", t.task),
    meta(t.owner, el("span", t.overdue ? "late" : "", "до " + dateLabel(t.due)), t.status));
}

let requestedMeeting = null;
function openMeeting(m) {
  requestedMeeting = {day: m.start.slice(0, 10), uid: m.uid};
  location.hash = "meetings";
}

function decisionItem(d, refresh) {
  const actions = el("div", "actions");
  const error = feedback();
  for (const [verdict, label, style] of [
    ["yes", "Согласовать", "primary"], ["no", "Отказать", ""], ["later", "Позже", "quiet"]
  ]) {
    actions.append(button(label, () => submitAction(actions, error, async () => {
      await api("/api/decisions/" + encodeURIComponent(d.event_id), "POST", {verdict});
      announce(verdict === "later" ? "Письмо отложено" : "Решение принято");
      refresh();
    }), style));
  }
  return el("div", "decision", meta(d.from, stamp(d.at)), el("h3", "", d.subject),
    d.summary === d.subject ? null : el("p", "", d.summary), el("p", "reason", d.reason), actions, error);
}

function todayPage(host) {
  const refresh = () => load(host, async mount => {
    const d = await api("/api/today");
    const decisions = d.decisions.map(item => decisionItem(item, refresh));
    const queue = card("Ждут только вас", "check", decisions[0] || hint("Все решения приняты."));
    queue.querySelector("h2").append(count(decisions));
    if (decisions.length > 1) queue.append(fold("Ещё в очереди · " + (decisions.length - 1), ...decisions.slice(1)));
    const schedule = rows(d.meetings, m => button(
      el("span", "time-row", el("strong", "", interval(m)), el("span", "", m.title), icon("arrow")),
      () => openMeeting(m), "row-link"), "Встреч на сегодня нет.");
    const date = el("div", "date-chip", icon("meetings"), dateLabel(d.day, {weekday: "long", day: "numeric", month: "long"}));
    mount.replaceChildren(el("div", "page-title", el("h1", "", "Сегодня"), date), metricCards(d.metrics),
      el("div", "dashboard", stack(queue, card("Риски", "alert", bullets(d.risks, "Рисков не зафиксировано.")),
        card("Задачи", "list", el("h3", "", "Просроченные"), rows(d.overdue, taskRow, "Просроченных задач нет."),
          el("h3", "", "Ближайшие"), rows(d.due_soon, taskRow, "Ближайших сроков нет."))),
      stack(card("Встречи дня", "meetings", schedule),
        card("Окна для фокуса", "focus", rows(d.focus, f => el("p", "focus-row", interval(f)), "Свободных окон нет.")),
        card("Сводка помощника", "mail", bullets(d.digest, "Пока нет записей.")))));
  });
  refresh();
}

function letterView(letter) {
  const addr = (name, email) => [name, email].filter(Boolean).join(" · ");
  return fold(el("span", "summary-body", el("strong", "", letter.subject), el("small", "", stamp(letter.at))),
    el("div", "letter", hint("От кого · " + addr(letter.from_name, letter.from_email)),
      hint("Кому · " + addr(letter.to_name, letter.to_email)), el("p", "verbatim", letter.body),
      badge(readable(letter.category)), el("h4", "", "Действие помощника"),
      el("p", "", readable(letter.action)), hint(letter.reason)));
}

function mailPage(host) {
  load(host, async mount => {
    const {groups, letters} = await api("/api/mail");
    const titles = Object.fromEntries(groups.map(g => [g.key, g.title]));
    const tabs = el("div", "tabs");
    const body = el("div");
    mount.replaceChildren(tabs, body);
    let group = "counterparty", direction = "in", party = null;
    function select(key) {
      group = key;
      party = null;
      render();
    }
    function render() {
      const dept = ["team", "internal"].includes(group);
      const topTabs = [["counterparty", titles.counterparty || "Контрагенты"], ["departments", "Отделы"],
        ["newsletter", titles.newsletter || "Рассылки"], ["spam", titles.spam || "Спам"], ["templates", "Шаблоны"]];
      tabs.replaceChildren(...topTabs.map(([key, name]) =>
        toggle(name, key === group || (key === "departments" && dept), () => select(key === "departments" ? "team" : key))));
      if (group === "templates") {
        templatePage(body);
        return;
      }
      const filters = el("div", "filters");
      if (dept) filters.append(el("div", "segmented", ["team", "internal"].map(key =>
        toggle(titles[key]?.split(":").pop().trim() || key, group === key, () => select(key)))));
      const selected = letters.filter(m => m.group === group);
      filters.append(el("div", "segmented", [["in", "Входящие"], ["out", "Исходящие"]].map(([key, label]) =>
        toggle(label + " · " + selected.filter(m => m.direction === key).length, direction === key, () => {
          direction = key;
          select(group);
        }))));
      const parties = new Map();
      for (const letter of selected.filter(m => m.direction === direction)) {
        if (!parties.has(letter.party)) parties.set(letter.party, []);
        parties.get(letter.party).push(letter);
      }
      const contacts = card("Собеседники", "profile");
      const thread = card("Письма", "mail");
      function showThread(name, scroll) {
        party = name;
        const entries = [...(parties.get(name) || [])].sort((a, b) => (Date.parse(b.at) || 0) - (Date.parse(a.at) || 0));
        contacts.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.party === name)));
        thread.replaceChildren(el("h2", "card-heading", icon("mail"), name || "Письма"),
          rows(entries, letterView, "В этой группе пока нет писем."));
        if (scroll && phone.matches) thread.scrollIntoView({block: "start"});
      }
      for (const [name, entries] of parties) {
        const pick = button(el("span", "contact", el("span", "", el("strong", "", name), hint(entries[0].party_note)),
          count(entries)), () => showThread(name, true), "row-link");
        pick.dataset.party = name;
        contacts.append(pick);
      }
      if (!parties.size) contacts.append(hint("Собеседников пока нет."));
      body.replaceChildren(filters, el("div", "split", contacts, thread));
      showThread(party ?? parties.keys().next().value, false);
    }
    render();
  });
}

function templatePage(host) {
  load(host, async mount => {
    const {templates} = await api("/api/templates");
    const menu = card("Шаблоны писем", "file");
    const editor = el("div");
    mount.replaceChildren(el("div", "split", menu, editor));
    // Keep each editor and its unsaved discussion when switching between templates.
    const editors = templates.map(templateEditor);
    templates.forEach((t, index) => {
      const pick = toggle(t.title, index === 0, () => {
        menu.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b === pick)));
        editor.replaceChildren(editors[index]);
        if (phone.matches) editor.scrollIntoView();
      });
      pick.className = "row-link";
      menu.append(pick);
    });
    if (editors.length) editor.append(editors[0]);
    else menu.append(hint("Шаблонов пока нет."));
  });
}

function templateEditor(t) {
  const url = "/api/templates/" + encodeURIComponent(t.key);
  const subject = el("input"), body = el("textarea"), message = el("textarea");
  subject.value = t.subject;
  body.value = t.body;
  body.rows = 5;
  message.rows = 2;
  subject.required = body.required = message.required = true;
  const error = feedback(), chatError = feedback(), chat = el("div", "chat");
  error.id = "template-error-" + t.key;
  const subjectField = field("Тема письма", subject), bodyField = field("Текст письма", body);
  function showValidation(detail) {
    const unknown = Array.from(detail.split(". Можно")[0].matchAll(/\{([^{}]+)\}/g), match => match[1])
      .filter(name => !t.variables.includes(name));
    const inSubject = unknown.some(name => subject.value.includes("{" + name + "}"));
    (inSubject ? subjectField : bodyField).after(error);
    for (const input of [subject, body]) {
      const invalid = unknown.some(name => input.value.includes("{" + name + "}"));
      input.setAttribute("aria-invalid", String(invalid));
      input.setAttribute("aria-describedby", error.id);
    }
    error.textContent = detail;
  }
  const edit = el("form", "stack"), discuss = el("form", "stack");
  const panel = stack(card(t.title, "edit", hint(t.used_for), edit), card("Обсудить с помощником", "mail", chat, discuss));
  const values = () => ({subject: subject.value, body: body.value});
  const save = formSubmit(edit, "Сохранить", () => submitAction(panel, error, async () => {
    let saved;
    try {
      saved = await api(url, "PUT", values());
    } catch (cause) {
      showValidation(cause.message);
      return;
    }
    subject.value = saved.subject;
    body.value = saved.body;
    showValidation("");
    announce("Шаблон сохранён");
  }));
  edit.append(subjectField, bodyField, error,
    hint("Переменные · " + t.variables.map(v => "{" + v + "}").join("  ")), save);
  const send = formSubmit(discuss, "Отправить", () => submitAction(panel, chatError, async () => {
    const prompt = message.value.trim();
    if (!prompt) throw new Error("Введите сообщение.");
    const proposal = await api(url + "/discuss", "POST", {...values(), message: prompt});
    chat.append(el("p", "bubble", el("strong", "", "Вы"), prompt),
      el("p", "bubble reply", el("strong", "", "Помощник"), proposal.reply));
    subject.value = proposal.subject;
    body.value = proposal.body;
    showValidation((proposal.problems || []).join(" · "));
    message.value = "";
    announce("Предложение в редакторе. Проверьте и сохраните.");
  }));
  discuss.append(field("Сообщение помощнику", message), chatError, send);
  return panel;
}

const phone = window.matchMedia("(max-width: 600px)");
const colours = new Map([["операционная", "teal"], ["один_на_один", "violet"], ["клиент", "blue"],
  ["руководство", "rose"], ["деньги", "amber"], ["партнёр", "green"]]);
const eventColour = m => colours.get(m.category) || "slate";

function shiftDay(day, delta) {
  const date = dateOf(day);
  date.setUTCDate(date.getUTCDate() + delta);
  return date.toISOString().slice(0, 10);
}

function monday(day) {
  return shiftDay(day, -((dateOf(day).getUTCDay() + 6) % 7));
}

function minute(iso) {
  return Number(iso.slice(11, 13)) * 60 + Number(iso.slice(14, 16));
}

// An interval ending at the next start frees its lane. Independent clusters
// reclaim the entire day column even if an earlier cluster needed more lanes.
function meetingLanes(items) {
  const ordered = [...items].sort((a, b) => minute(a.start) - minute(b.start) || minute(b.end) - minute(a.end));
  const placed = [];
  let group = [], ends = [];
  function finishGroup() {
    for (const entry of group) placed.push({...entry, lanes: ends.length});
    group = [];
    ends = [];
  }
  for (const item of ordered) {
    const start = minute(item.start);
    if (ends.length && ends.every(end => end <= start)) finishGroup();
    let lane = ends.findIndex(end => end <= start);
    if (lane < 0) lane = ends.length;
    ends[lane] = minute(item.end);
    group.push({item, lane});
  }
  finishGroup();
  return placed;
}

function meetingButton(m, select, classes, ...children) {
  const b = button(el("span", "event-label", ...children), () => select(m.uid, true, m.start.slice(0, 10)),
    classes + " " + eventColour(m));
  b.dataset.uid = m.uid;
  b.title = m.title + " · " + interval(m) + " · " + readable(m.category);
  b.setAttribute("aria-label", b.title);
  return b;
}

function weekCalendar(data, today, select) {
  const items = [...data.meetings, ...data.focus];
  const days = Array.from({length: 7}, (_, i) => shiftDay(data.start, i))
    .filter((day, i) => i < 5 || items.some(m => m.start.startsWith(day)));
  const first = Math.min(8, ...items.map(m => Math.floor(minute(m.start) / 60)));
  const last = Math.max(20, ...items.map(m => Math.ceil(minute(m.end) / 60)));
  const board = el("div", "week-grid");
  board.style.setProperty("--days", days.length);
  board.style.setProperty("--hours", last - first);
  board.append(el("div", "axis-head"));
  for (const day of days) {
    board.append(el("div", "day-head" + (day === today ? " current" : ""),
      dateLabel(day, {weekday: "short"}), el("strong", "", Number(day.slice(8)))));
  }
  const axis = el("div", "time-axis");
  for (let hour = first; hour <= last; hour++) {
    const tick = el("span", "", String(hour).padStart(2, "0") + ":00");
    tick.style.top = (hour - first) / (last - first) * 100 + "%";
    axis.append(tick);
  }
  board.append(axis);
  function position(block, item) {
    block.style.top = (minute(item.start) - first * 60) / ((last - first) * 60) * 100 + "%";
    block.style.height = (minute(item.end) - minute(item.start)) / ((last - first) * 60) * 100 + "%";
  }
  for (const day of days) {
    const col = el("div", "day-column");
    for (const f of data.focus.filter(f => f.start.startsWith(day))) {
      const block = el("div", "focus-block", el("span", "", f.title));
      block.title = f.title + " · " + interval(f);
      position(block, f);
      col.append(block);
    }
    for (const {item, lane, lanes} of meetingLanes(data.meetings.filter(m => m.start.startsWith(day)))) {
      const block = meetingButton(item, select, "event", el("small", "", interval(item)), el("strong", "", item.title));
      position(block, item);
      block.style.left = lane / lanes * 100 + "%";
      block.style.width = 100 / lanes + "%";
      col.append(block);
    }
    board.append(col);
  }
  const legend = el("div", "legend");
  for (const category of new Set(data.meetings.map(m => m.category))) {
    legend.append(el("span", eventColour({category}), el("i", "swatch"), readable(category)));
  }
  if (data.focus.length) legend.append(el("span", "", el("i", "swatch hatch"), "Окно для фокуса"));
  return el("div", "", legend, board);
}

function dayCalendar(data, day, select) {
  const items = [...data.meetings, ...data.focus].filter(m => m.start.startsWith(day))
    .sort((a, b) => minute(a.start) - minute(b.start));
  return rows(items, m => m.uid ? meetingButton(m, select, "day-event",
    el("small", "", interval(m)), el("strong", "", m.title), el("span", "", readable(m.category))) :
    el("div", "day-focus", el("strong", "", interval(m)), m.title), "На этот день встреч и защищённых окон нет.");
}

function protocolView(p, mutate) {
  const content = el("div", "protocol");
  function evidence(item) {
    return el("div", "", item.quote ? el("blockquote", "", item.quote) : null,
      item.quote_ok === false ? el("small", "late", "Цитата требует проверки") : null);
  }
  content.append(badge(p.status), el("h4", "", "Решения"),
    rows(p.decisions, d => el("div", "row", d.text, evidence(d)), "Решений не зафиксировано."),
    el("h4", "", "Поручения"), rows(p.tasks, t => el("div", "row", el("strong", "", t.what),
      hint(t.owner + " · до " + stamp(t.due)), evidence(t), t.flags?.length ? hint(t.flags.join(" · ")) : null),
    "Поручений нет."), el("h4", "", "Открытые вопросы"), bullets(p.open_questions, "Открытых вопросов нет."));
  if (p.status !== "черновик") return content;
  const error = feedback(), controls = el("div", "actions"), form = el("form", "stack edit-form");
  const instruction = el("textarea");
  instruction.required = true;
  form.hidden = true;
  const run = (suffix, payload) => submitAction(content, error, () => mutate(p.id, suffix, payload));
  controls.append(button("Утвердить", () => run("/approve"), "primary"),
    button("Править", () => { form.hidden = !form.hidden; if (!form.hidden) instruction.focus(); }),
    button("Отклонить", () => run("/reject"), "quiet"));
  form.append(field("Что исправить в протоколе", instruction), formSubmit(form, "Применить правку",
    () => run("/edit", {instruction: instruction.value.trim()})));
  content.append(controls, form, error);
  return content;
}

function transcriptForm(onCreated) {
  const form = el("form", "stack");
  const title = el("input"), transcript = el("textarea"), minutes = el("input"), error = feedback();
  title.required = transcript.required = minutes.required = true;
  transcript.rows = 5;
  minutes.type = "number";
  minutes.min = "0";
  minutes.value = "0";
  let meetingUid = null;
  title.addEventListener("input", () => { meetingUid = null; });
  const submit = formSubmit(form, "Создать черновик", () => submitAction(form, error, async () => {
    const payload = {title: title.value.trim(), transcript: transcript.value.trim(), minutes_ago: Number(minutes.value)};
    if (!payload.title || !payload.transcript) throw new Error("Введите название встречи и расшифровку.");
    if (meetingUid) payload.meeting_uid = meetingUid;
    const {protocol_id} = await api("/api/meetings/transcript", "POST", payload);
    form.reset();
    minutes.value = "0";
    meetingUid = null;
    announce("Создан черновик протокола №" + protocol_id);
    onCreated(protocol_id);
  }));
  form.append(field("Название встречи", title), field("Расшифровка", transcript),
    field("Сколько минут назад закончилась встреча", minutes), error, submit);
  return {form, link(m) {
    meetingUid = m.uid;
    title.value = m.title;
    transcript.focus();
  }};
}

function meetingsPage(host) {
  const requested = requestedMeeting;
  requestedMeeting = null;
  load(host, async mount => {
    const {day: today} = await api("/api/today");
    if (!mount.isConnected) return;
    let day = requested?.day || today, selected = requested?.uid, tab = "protocols", openId = null;
    const board = el("div"), detail = el("div"), records = el("div"), tabs = el("div", "tabs");
    const toolbar = el("div", "calendar-toolbar");
    const calendar = el("article", "card calendar", toolbar, board);
    const transcript = transcriptForm(id => {
      openId = id;
      tab = "protocols";
      refreshRecords();
      refreshCalendar();
      tabs.scrollIntoView();
    });
    mount.replaceChildren(calendar, detail, el("div", "card", tabs, records),
      card("Расшифровка → протокол", "file", transcript.form));
    const pending = new Set();
    async function mutate(id, suffix, payload) {
      if (pending.has(id)) return;
      pending.add(id);
      try {
        await api("/api/protocols/" + encodeURIComponent(id) + suffix, "POST", payload);
        announce({"/approve": "Протокол утверждён", "/reject": "Протокол отклонён", "/edit": "Правка внесена"}[suffix]);
        openId = id;
        refreshRecords();
        refreshCalendar();
      } finally {
        pending.delete(id);
      }
    }
    function refreshRecords() {
      tabs.replaceChildren(...[["protocols", "Протоколы"], ["tasks", "Задачи"]].map(([key, title]) =>
        toggle(title, key === tab, () => { tab = key; refreshRecords(); })));
      load(records, async area => {
        if (tab === "tasks") {
          const {tasks} = await api("/api/tasks");
          area.replaceChildren(rows(tasks, taskRow, "Задач пока нет."));
        } else {
          const {protocols} = await api("/api/protocols");
          area.replaceChildren(rows(protocols, p => {
            const item = fold(el("span", "summary-body", el("strong", "", p.title),
              el("small", "", stamp(p.meeting_end) + " · " + p.status)), protocolView(p, mutate));
            item.open = p.id === openId;
            return item;
          }, "Протоколов пока нет."));
        }
      });
    }
    function select(uid, scroll = false, selectedDay = day) {
      selected = uid;
      day = selectedDay;
      board.querySelectorAll("[data-uid]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.uid === uid)));
      if (!uid) { detail.replaceChildren(); return; }
      load(detail, async area => {
        const d = await api("/api/meetings/" + encodeURIComponent(uid));
        const m = d.meeting;
        const avatar = el("a", "button", "Аватар на встречу", icon("arrow"));
        avatar.href = "/avatar?" + new URLSearchParams({title: m.title, uid: m.uid});
        const info = card(m.title, "meetings",
          meta(stamp(m.start) + "–" + time(m.end), badge(readable(m.category))),
          el("h3", "", "Участники"), bullets(m.attendees.map(a => a.name || a.email), "Участники не указаны."),
          el("div", "actions", avatar, button("Добавить расшифровку", () => transcript.link(m))),
          el("h3", "", "Протокол"), d.protocol ? protocolView(d.protocol, mutate) : hint("Протокола пока нет."),
          el("h3", "", "Вопросы по прошлому протоколу"), bullets(d.previous_questions, "Вопросов нет."),
          el("h3", "", "Вопросы из истории"), bullets(d.history_questions, "Вопросов нет."),
          el("h3", "", "Задачи участников"), rows(d.tasks, taskRow, "Открытых задач нет."));
        area.replaceChildren(info);
        if (scroll && area.isConnected) {
          info.tabIndex = -1;
          info.focus({preventScroll: true});
          info.scrollIntoView();
        }
      });
    }
    function move(next) {
      day = next;
      selected = null;
      refreshCalendar();
    }
    function refreshCalendar() {
      const daily = phone.matches, start = daily ? day : monday(day), step = daily ? 1 : 7;
      const fullDate = {day: "numeric", month: "long", year: "numeric"};
      const label = daily ? dateLabel(day, {...fullDate, weekday: "short"}) :
        dateLabel(start) + " — " + dateLabel(shiftDay(start, 6), fullDate);
      const prev = button("‹", () => move(shiftDay(day, -step)), "arrow-button");
      const next = button("›", () => move(shiftDay(day, step)), "arrow-button");
      prev.setAttribute("aria-label", daily ? "Предыдущий день" : "Предыдущая неделя");
      next.setAttribute("aria-label", daily ? "Следующий день" : "Следующая неделя");
      toolbar.replaceChildren(el("div", "", el("span", "view-label", daily ? "День" : "Неделя"), el("h2", "", label)),
        el("div", "actions", prev, button("Сегодня", () => move(today)), next));
      detail.replaceChildren();
      load(board, async area => {
        const data = await api("/api/meetings?" + new URLSearchParams({start, days: step}));
        if (!area.isConnected) return;
        area.replaceChildren(daily ? dayCalendar(data, day, select) : weekCalendar(data, today, select));
        const visible = data.meetings.filter(m => !daily || m.start.startsWith(day));
        const choice = visible.find(m => m.uid === selected) || visible.find(m => m.start.startsWith(day)) || visible[0];
        select(choice?.uid);
      });
    }
    phone.onchange = refreshCalendar;
    refreshCalendar();
    refreshRecords();
  });
}

function memoryPage(host) {
  const form = el("form", "search");
  const query = el("input");
  query.type = "search";
  query.placeholder = "Что известно по…";
  query.setAttribute("aria-label", "Поиск по памяти");
  const results = el("div"), changes = el("div");
  function search() {
    const q = query.value.trim();
    load(results, async mount => {
      const {topics, facts} = await api("/api/memory?" + new URLSearchParams({q}));
      const history = item => el("div", "row", el("strong", "", item.period), hint(item.who), el("p", "", item.what),
        el("blockquote", "", item.quote), hint("Источники · " + item.sources.join(", ")));
      mount.replaceChildren(meta("Найдено тем · " + topics.length, "Фактов в памяти · " + facts),
        rows(topics, t => fold(el("span", "summary-body", el("strong", "", t.topic),
          el("small", "", readable(t.kind) + " · " + stamp(t.last_at)), el("span", "", t.last_what)),
          rows(t.history, history, "Истории пока нет.")), q ? "По запросу ничего не найдено." : "Память пока пуста."));
    });
  }
  form.append(query, formSubmit(form, "Найти", search));
  host.append(el("div", "dashboard", card("Поиск по памяти", "memory", form, results), changes));
  search();
  load(changes, async mount => {
    const d = await api("/api/edits");
    const changesList = items => rows([...items].reverse(), e => el("div", "row",
      el("strong", "", e.title || "Протокол №" + e.protocol_id), hint(stamp(e.at)),
      el("p", "", e.instruction || e.summary)), "Правок пока нет.");
    mount.replaceChildren(card("Правки руководителя", "edit", el("h3", "", "Протоколы"), changesList(d.protocol_edits),
      el("h3", "", "Шаблоны"), changesList(d.template_edits)));
  });
}

function profilePage(host) {
  load(host, async mount => {
    const [p, metrics, journal] = await Promise.all([api("/api/profile"), api("/api/metrics"), api("/api/journal?limit=50")]);
    const profile = card(p.name, "profile", hint(p.title), el("h3", "", "Цели"), bullets(p.goals, "Цели не указаны."),
      el("h3", "", "Только с подтверждением"), el("div", "tags", p.confirm.map(x => badge(readable(x)))),
      el("h3", "", "Стиль общения"), el("p", "", p.style), el("h3", "", "Время плана"),
      el("p", "", "На день · " + p.plan_time), el("p", "", "На неделю · " + p.week_time));
    const rules = card("Правила по категориям", "list", rows(Object.entries(p.rules), ([category, rule]) =>
      el("div", "row", el("strong", "", readable(category)), el("p", "", readable(rule.action)),
        rule.delegate_to ? hint("Исполнитель · " + rule.delegate_to) : null, hint(rule.source)), "Правил пока нет."));
    const log = card("Журнал действий помощника", "clock", rows(journal.actions, a => el("div", "row",
      meta(stamp(a.at), {assistant: "Помощник", human: "Руководитель"}[a.actor] || a.actor),
      el("p", "", a.summary || readable(a.kind))), "Действий пока нет."));
    mount.replaceChildren(metricCards(metrics.cards), el("div", "dashboard", stack(profile,
      card("Дополнительные метрики", "chart",
        el("p", "", "Писем без руководителя · " + metrics.letters_without_leader),
        el("p", "", "Доля писем без руководителя · " + (metrics.share_without_leader == null ? "—" :
          (metrics.share_without_leader * 100).toLocaleString("ru-RU", {maximumFractionDigits: 2}) + " %")),
        el("p", "", "До утверждения протокола, мин · " + (metrics.protocol_minutes_to_approved ?? "—")))), rules), log);
  });
}

const pages = {today: todayPage, mail: mailPage, meetings: meetingsPage, memory: memoryPage, profile: profilePage};
function route() {
  const hash = location.hash.slice(1);
  const key = Object.hasOwn(pages, hash) ? hash : "today";
  if (hash !== key) history.replaceState(null, "", "#" + key);
  phone.onchange = null;
  document.querySelectorAll("nav a").forEach(a => {
    if (a.hash === "#" + key) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  for (const section of document.querySelectorAll("[data-section]")) {
    section.replaceChildren();
    section.hidden = section.dataset.section !== key;
    if (section.hidden) continue;
    const title = section.getAttribute("aria-label");
    document.title = title + " · Кабинет руководителя";
    const host = el("div");
    if (key !== "today") section.append(el("div", "page-title", el("h1", "", title)));
    section.append(host);
    pages[key](host);
  }
  window.scrollTo(0, 0);
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelector(".skip").addEventListener("click", event => {
    event.preventDefault();
    document.querySelector("main").focus();
  });
  window.addEventListener("hashchange", route);
  route();
});
