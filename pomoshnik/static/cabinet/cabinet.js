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
  return el("article", "card t-card", el("h2", "card-heading", icon(symbol), title), ...body);
}

// Коралловая иллюстрация в углу карточки — облик «Голубое стекло» (/static/theme/).
function illus(node, name) {
  const img = el("img", "t-illus");
  img.src = "/static/theme/img/" + name + ".svg";
  img.alt = "";
  node.prepend(img);
  node.classList.add("has-illus");
  return node;
}

function plural(n, forms) {
  const a = Math.abs(n) % 100, b = a % 10;
  return forms[a > 10 && a < 20 ? 2 : b === 1 ? 0 : b >= 2 && b <= 4 ? 1 : 2];
}

function greeting() {
  const h = new Date().getHours();
  return h < 5 ? "Доброй ночи" : h < 12 ? "Доброе утро" : h < 18 ? "Добрый день" : "Добрый вечер";
}

function hero(d, queue) {
  const art = el("img", "t-hero-art");
  art.src = "/static/theme/img/hero.svg";
  art.alt = "";
  const n = d.decisions.length, m = d.meetings.length;
  const letters = d.metrics["писем разобрано"], share = d.metrics["без руководителя"];
  const toQueue = button("Решения · " + n, () => queue.scrollIntoView({behavior: "smooth", block: "start"}), "t-btn t-btn--coral");
  const week = el("a", "t-btn t-btn--ghost", "Неделя встреч");
  week.href = "#meetings";
  return el("section", "t-hero",
    el("div", "t-hero-text",
      el("div", "t-kicker", "ИИ-помощник руководителя · " + dateLabel(d.day, {weekday: "long", day: "numeric", month: "long"})),
      el("h1", "hero-title", greeting() + ", " + d.leader),
      el("div", "t-rule"),
      el("div", "t-kicker", m + " " + plural(m, ["встреча", "встречи", "встреч"]) + " · " +
        (n ? n + " " + plural(n, ["решение ждёт", "решения ждут", "решений ждут"]) + " только вас" : "решений, которые ждут вас, нет")),
      letters == null ? null : el("div", "t-kicker", "Разобрано писем: " + letters + " · " + share + " — без вашего участия"),
      el("div", "hero-actions", toQueue, week)),
    art);
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
  const b = button(el("span", "task-line", el("strong", "", t.task),
    meta(t.owner, el("span", t.overdue ? "late" : "", "до " + dateLabel(t.due)), t.status),
    t.why ? el("small", "late task-why", t.why) : null, icon("arrow")),
    () => { location.hash = (location.hash.slice(1).split("/")[0] || "today") + "/t/" + encodeURIComponent(t.id); },
    "row-link");
  b.setAttribute("aria-label", "Задача: " + t.task);
  return b;
}

// Страница задачи: суть, что известно, что дальше и кнопки решения.
function taskView(host, id) {
  const again = () => taskView(host, id);
  load(host, async mount => {
    const p = await api("/api/tasks/" + encodeURIComponent(id));
    const t = p.task, n = p.next;
    const actions = el("div", "actions"), error = feedback();
    const due = el("input");
    due.type = "date";
    due.value = t.due;
    due.setAttribute("aria-label", "Новый срок");
    const act = (action, body = {}) => submitAction(actions, error, async () => {
      await api("/api/tasks/" + encodeURIComponent(id) + "/action", "POST", {action, ...body});
      announce({remind: "Напоминание отправлено", move: "Срок перенесён", done: "Задача закрыта"}[action]);
      again();
    });
    if (t.status !== "выполнена") {
      actions.append(button("Напомнить: " + t.owner.split(" ")[0], () => act("remind"), "primary"),
        el("label", "task-move", due, button("Перенести срок", () => act("move", {due: due.value}))),
        button("Выполнена", () => act("done"), "quiet"));
    }
    const letter = x => el("div", "row", meta(x.from, x.at ? stamp(x.at) : "", x.route), el("strong", "", x.subject));
    const fact = f => el("div", "row", meta(f.who, f.when, f.source), el("p", "", f.what), el("blockquote", "", f.quote));
    const back = button("← Назад", () => {
      const head = location.hash.slice(1).split("/")[0] || "today";
      if (history.length > 1) history.back(); else location.hash = head;
    }, "quiet back-link");
    mount.replaceChildren(back,
      card(t.task, "list",
        meta(t.owner, el("span", t.overdue ? "late" : "", "до " + dateLabel(t.due)), t.status, "откуда: " + t.source),
        t.why ? el("p", "task-why-main", el("strong", "", "Почему просрочена: "), t.why) : null,
        p.flags.length ? bullets(p.flags, "") : null,
        el("h3", "", "Что дальше"), el("p", "", n.text),
        n.meeting ? button("Открыть встречу «" + n.meeting.title + "»", () => openMeeting(n.meeting), "quiet") : null,
        actions, error),
      card("Что известно", "memory",
        el("h3", "", "Письма по теме — клиент, исполнитель"), rows(p.letters, letter, "Писем по теме нет."),
        el("h3", "", "Из памяти и протоколов"), rows(p.facts, fact, "В памяти по теме ничего нет."),
        el("h3", "", "Что уже делали"), rows(p.history, h => el("div", "row", meta(stamp(h.at)), h.summary), "Пока ничего.")));
  });
}

let calendarDay = null;            // неделя календаря, к которой возвращаемся со страницы встречи
function openMeeting(m) {
  calendarDay = m.start.slice(0, 10);
  location.hash = "meetings/m/" + encodeURIComponent(m.uid);
}

// Готовое решение помощника (TASK_12): совет, факты дословно из источников, черновик ответа, который можно поправить.
function draftField(label, area, text) {
  area.value = text || "";
  area.rows = 3;
  area.setAttribute("aria-label", label);
  return text ? el("label", "decision-draft", el("span", "", label), area) : null;
}

// Два черновика: «Согласовать» отправляет первый, «Отказать» — второй. Оба можно поправить.
function preparedBlock(p, drafts) {
  if (!p) return el("p", "reason decision-wait", "Готовлю черновики ответа и факты…");
  // Пункт: что это значит для решения, мелко — откуда (задача, встреча, память) дословно.
  const list = items => el("ul", "decision-facts", items.map(f => {
    const li = el("li", "", f.note || f.text, el("span", "decision-note", " · " + f.kind + ": " + f.text));
    li.title = f.source_text;
    return li;
  }));
  const blockers = p.blockers || [];
  const advice = p.advice === "нет совета"
    ? el("p", "decision-advice decision-advice--none", "Совета нет — решаете вы.")
    : el("p", "decision-advice", el("strong", "", "Совет: " + p.advice), p.why ? " — " + p.why : "");
  return el("div", "decision-prep",
    p.ask ? el("p", "decision-ask", el("strong", "", "Запрос: "), p.ask) : null,
    el("p", "decision-label", "Что мешает"),
    blockers.length ? list(blockers) : el("p", "decision-advice--none", "В задачах, календаре и памяти препятствий не нашёл."),
    p.facts.length ? el("p", "decision-label", "За") : null, p.facts.length ? list(p.facts) : null,
    advice,
    draftField("«Согласовать» отправит", drafts.yes, p.draft_yes || p.draft),
    draftField("«Отказать» отправит", drafts.no, p.draft_no),
    p.unverified && p.unverified.length ? el("p", "decision-warn", "Числа нет ни в письме, ни в базе — проверьте: " + p.unverified.join(", ")) : null);
}

// Риск: строка и кнопка «Подробнее» — отчёт, из которого риск, и что помощник уже сделал.
function riskItem(r) {
  const src = r.sources.map(s => el("div", "risk-src", meta(s.from, stamp(s.at)), el("strong", "", s.subject),
    el("p", "", s.body)));
  const done = r.actions.length
    ? el("ul", "bullets", r.actions.map(x => el("li", "", el("strong", "", x.to_name + ": "), x.question)))
    : hint("Исполнителям помощник пока не писал.");
  const n = r.next;
  const todo = n ? el("div", "risk-todo", el("strong", "", "Что от вас: "), n.text, el("p", "reason", n.then),
    n.meeting ? button("Открыть встречу «" + n.meeting.title + "»", () => openMeeting(n.meeting), "quiet") : null) : null;
  return el("div", "risk", el("p", "", r.text), todo,
    fold("Подробнее", el("h3", "", "Откуда"), src.length ? src : hint("Источник не сохранён."),
      el("h3", "", "Что помощник уже сделал"), done));
}

function decisionItem(d, refresh) {
  const actions = el("div", "actions");
  const error = feedback();
  const drafts = {yes: el("textarea"), no: el("textarea")};
  for (const [verdict, label, style] of [
    ["yes", "Согласовать", "primary"], ["no", "Отказать", ""], ["later", "Позже", "quiet"]
  ]) {
    actions.append(button(label, () => submitAction(actions, error, async () => {
      const body = {verdict};
      const area = drafts[verdict];
      if (area && d.prepared && area.value.trim()) body.text = area.value;
      await api("/api/decisions/" + encodeURIComponent(d.event_id), "POST", body);
      announce(verdict === "later" ? "Отложено" : "Решение принято");
      refresh();
    }), style));
  }
  const times = d.count > 1 ? el("span", "decision-times", "пишут " + d.count + "-й раз") : null;
  return el("div", "decision", meta(d.from, stamp(d.at), ...(times ? [times] : [])), el("h3", "", d.subject),
    d.summary === d.subject ? null : el("p", "", d.summary), el("p", "reason", d.reason),
    preparedBlock(d.prepared, drafts), actions, error);
}

function todayPage(host, sub = "") {
  if (sub.startsWith("t/")) return taskView(host, decodeURIComponent(sub.slice(2)));
  const refresh = () => load(host, async mount => {
    const d = await api("/api/today");
    const decisions = d.decisions.map(item => decisionItem(item, refresh));
    const queue = card("Ждут только вас", "check", decisions[0] || hint("Все решения приняты."));
    queue.querySelector("h2").append(count(decisions));
    if (decisions.length > 1) queue.append(fold("Ещё в очереди · " + (decisions.length - 1), ...decisions.slice(1)));
    const schedule = rows(d.meetings, m => button(
      el("span", "time-row", el("strong", "", interval(m)), el("span", "", m.title), icon("arrow")),
      () => openMeeting(m), "row-link"), "Встреч на сегодня нет.");
    illus(queue, "mail");
    const risks = card("Риски", "alert", (d.risk_items || []).length
      ? el("div", "rows", d.risk_items.map(riskItem)) : bullets(d.risks, "Рисков не зафиксировано."));
    risks.classList.add("t-card--coral");
    mount.replaceChildren(hero(d, queue), metricCards(d.metrics),
      el("div", "dashboard", stack(queue, risks,
        card("Задачи", "list", el("h3", "", "Просроченные"), rows(d.overdue, taskRow, "Просроченных задач нет."),
          el("h3", "", "Ближайшие"), rows(d.due_soon, taskRow, "Ближайших сроков нет."))),
      stack(illus(card("Встречи дня", "meetings", schedule), "calendar"),
        card("Сводка помощника", "mail",
          el("p", "reason", "Что помощник сделал сам, без вас: передал письма, ответил, прочитал отчёты."),
          bullets(d.digest, "Пока нет записей.")))));
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
        ["newsletter", titles.newsletter || "Рассылки"], ["spam", titles.spam || "Спам"], ["style", "Как я пишу"]];
      tabs.replaceChildren(...topTabs.map(([key, name]) =>
        toggle(name, key === group || (key === "departments" && dept), () => select(key === "departments" ? "team" : key))));
      if (group === "style") {
        stylePage(body);
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

// Как пишет руководитель (TASK_14). Шаблонов он не составляет: помощник пишет письма в его стиле и учится
// на его правках и словах. Правила — по видам писем и адресатам (Пит 27.11: «на все письма одного шаблона быть не может»).
function stylePage(host, selected = "auto_reply") {
  load(host, async mount => {
    const d = await api("/api/style");
    const audience = key => d.audiences[key] || key;
    const menu = card("Виды писем", "mail",
      hint("Шаблоны составлять не нужно. Помощник пишет в вашем стиле и учится, когда вы правите его черновики " +
        "или говорите, как писать. Цифры, сроки, имена и ссылки подставляет код."));
    const panel = el("div");
    mount.replaceChildren(el("div", "split", menu, panel));
    function show(kind) {
      menu.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.kind === kind.key)));
      panel.replaceChildren(kindPanel(kind));
      if (phone.matches) panel.scrollIntoView({block: "start"});
    }
    function kindPanel(kind) {
      const rule = r => el("div", "row",
        el("strong", "", r.text),
        meta(audience(r.audience), r.source === "ваша правка" ? "из вашей правки" : "вы сказали", stamp(r.at)),
        r.example ? el("p", "", "«" + r.example.draft + "» → «" + r.example.text + "»") : null,
        button("Забыть", async () => {
          await api("/api/style/rules/" + r.id, "DELETE");
          announce("Помощник забыл правило");
          stylePage(host, kind.key);
        }, "button-ghost"));
      const who = el("select");
      for (const [key, label] of Object.entries(d.audiences))
        who.append(Object.assign(el("option", "", key === "*" ? "Любому адресату" : label[0].toUpperCase() + label.slice(1)), {value: key}));
      const message = el("textarea");
      message.rows = 2;
      message.required = true;
      message.placeholder = kind.key === "decision" ? "Например: руководству — без «ты», по имени-отчеству"
        : "Например: не пиши «подтверждаю», пиши «спасибо, получил»";
      const error = feedback(), teach = el("form", "stack");
      const box = card(kind.title, "edit",
        kind.key === "*" ? el("p", "", d.style || "Стиль в профиле не описан.") : null,
        el("h3", "", "Чему помощник научился"),
        rows([...kind.rules].reverse(), rule, kind.key === "decision"
          ? "Пока ничему: поправьте черновик ответа в карточке решения или скажите ниже."
          : "Пока ничему: скажите ниже, как писать такие письма."),
        el("h3", "", "Научить помощника"), teach);
      teach.append(field("Кому", who), field("Как писать " + (kind.key === "*" ? "все письма" : "такие письма"), message),
        error, formSubmit(teach, "Запомнить", () => submitAction(box, error, async () => {
          const text = message.value.trim();
          if (!text) throw new Error("Напишите, как писать письма.");
          const r = await api("/api/style/teach", "POST", {message: text, kind: kind.key, audience: who.value});
          announce(r.reply);
          stylePage(host, kind.key);
        })));
      const sounds = kind.letters.length ? card("Как сейчас звучит", "mail", rows(kind.letters, x => el("div", "row",
        el("strong", "", x.title + (x.audience === "*" ? "" : " · " + audience(x.audience))),
        meta(x.source, x.used_for),
        el("p", "letter-example", x.example),
        x.problem ? hint("Новый текст не прошёл проверку кода: " + x.problem + ". Пока уходит прежний.") : null), "")) : null;
      return stack(box, sounds);
    }
    for (const kind of d.kinds) {
      const pick = toggle(kind.title + (kind.rules.length ? " · " + kind.rules.length : ""), kind.key === selected, () => show(kind));
      pick.className = "row-link";
      pick.dataset.kind = kind.key;
      menu.append(pick);
    }
    show(d.kinds.find(k => k.key === selected) || d.kinds[0]);
  });
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
  const b = button(el("span", "event-label", ...children), () => openMeeting(m), classes + " " + eventColour(m));
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
    col.title = "Нажмите на свободное время, чтобы назначить встречу";
    col.addEventListener("click", ev => {          // клик по свободному месту — новая встреча на это время
      if (ev.target.closest(".event")) return;
      const box = col.getBoundingClientRect();
      const mins = first * 60 + (ev.clientY - box.top) / box.height * (last - first) * 60;
      const slot = Math.max(first * 60, Math.min(last * 60 - 30, Math.floor(mins / 30) * 30));
      newMeetingDialog(day, String(Math.floor(slot / 60)).padStart(2, "0") + ":" + String(slot % 60).padStart(2, "0"));
    });
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

// Ссылка Телемоста: без токена — открыть Телемост и вставить ссылку; с TELEMOST_OAUTH_TOKEN — создать автоматически.
let telemostConfig = null;
function telemostLinkTool(input, error) {
  const btn = button("Создать комнату в Телемосте", () => {}, "");
  const tip = hint("");
  (telemostConfig ? Promise.resolve(telemostConfig) : api("/api/telemost").then(c => (telemostConfig = c))).then(cfg => {
    tip.textContent = cfg.auto ? "Комната создастся автоматически." :
      "Встреча в календаре уже есть — это только комната для созвона. В Телемосте нажмите «Создать видеовстречу», " +
      "скопируйте ссылку и вставьте её в поле выше: она сохранится сама и уйдёт участникам.";
    btn.onclick = async () => {
      if (!cfg.auto) {
        window.open(cfg.create_url, "_blank", "noopener");
        input.focus();
        return;
      }
      btn.disabled = true;
      try {
        input.value = (await api("/api/telemost/conference", "POST")).link;
        input.dispatchEvent(new Event("change"));
      } catch (e) {
        error.textContent = e.message;
      } finally {
        btn.disabled = false;
      }
    };
  }).catch(() => {});
  return el("div", "telemost-tool", btn, tip);
}

// Как проходит встреча: веду сам или мой ИИ-аватар; комната Телемоста; подключиться самому (TASK_11).
function telemostBlock(m, refresh) {
  const error = feedback(), status = hint(""), actions = el("div", "actions");
  const link = el("input");
  link.type = "url";
  link.placeholder = "Вставьте ссылку на комнату Телемоста";
  link.value = m.link || "";
  link.addEventListener("change", async () => {
    const v = link.value.trim();
    if (!v || v === m.link) return;
    error.textContent = "";
    try {
      await api("/api/meetings/" + encodeURIComponent(m.uid) + "/link", "PUT", {link: v});
      announce("Ссылка сохранена");
      refresh();
    } catch (e) { error.textContent = e.message; }
  });
  async function setLead(avatar) {
    try {
      await api("/api/meetings/" + encodeURIComponent(m.uid) + "/lead", "PUT", {avatar});
      announce(avatar ? "Встречу ведёт ваш ИИ-аватар" : "Встречу ведёте вы");
      refresh();
    } catch (e) { error.textContent = e.message; }
  }
  const lead = el("div", "segmented lead", toggle("Веду сам", !m.avatar, () => setLead(false)),
    toggle("Ведёт мой ИИ-аватар", m.avatar, () => setLead(true)));
  if (m.link) {
    const join = el("a", "button-ghost join", "Подключиться к встрече", icon("arrow"));
    join.href = m.link; join.target = "_blank"; join.rel = "noopener";
    actions.append(join);
  }
  const box = el("div", "stack telemost", el("h3", "", "Кто ведёт встречу"), lead,
    field("Комната в Телемосте", link), m.link ? null : telemostLinkTool(link, error), actions, status, error);
  if (!m.avatar) {
    status.textContent = m.link ? "Встречу ведёте вы. После неё добавьте расшифровку — протокол соберётся сам." :
      "Встречу ведёте вы.";
    return box;
  }
  if (!m.link) {
    status.textContent = "Аватару нужна комната: создайте её в Телемосте и вставьте ссылку — он зайдёт сам.";
    return box;
  }
  api("/api/robot?" + new URLSearchParams({uid: m.uid})).then(r => {
    const here = r.state === "running";
    if (here) {
      status.textContent = "Аватар сейчас на встрече: " + r.status + ". Подключитесь, чтобы послушать или перехватить разговор.";
      actions.append(button("Остановить аватара", async () => { await api("/api/robot/stop?" + new URLSearchParams({uid: m.uid}), "POST"); announce("Аватар выходит из встречи"); refresh(); }, "quiet"));
    } else {
      status.textContent = "Аватар зайдёт в Телемост сам в " + time(m.avatar_at) + ". Вы можете подключиться: просто слушать или вступить в разговор — аватар слышит всех.";
      actions.append(button("Запустить аватара сейчас", () => submitAction(box, error, async () => {
        const minutes = Math.max(15, Math.round((Date.parse(m.end) - Date.parse(m.start)) / 60000) + 10);
        await api("/api/robot/start", "POST", {link: m.link, title: m.title, uid: m.uid, minutes});
        announce("Аватар заходит в Телемост, через полминуты он будет на встрече");
        refresh();
      }), "quiet"));
    }
  }).catch(() => {});
  return box;
}

// ---- Совещания: календарь отдельно, встреча — своя страница, новая встреча кликом по слоту ----
const CATEGORIES = [["операционная", "Операционная"], ["один_на_один", "Один на один"], ["клиент", "Клиент"],
  ["партнёр", "Партнёр"], ["руководство", "Руководство"], ["деньги", "Деньги"]];
let peopleCache = null;

function newMeetingDialog(day, at) {
  const dlg = el("dialog", "t-card meeting-dialog");
  const title = el("input"), date = el("input"), start = el("input"), minutes = el("select"), category = el("select");
  const emails = el("input"), link = el("input"), avatar = el("input"), error = feedback();
  title.required = true;
  title.placeholder = "Например: Разбор КП «ТехноСтрой»";
  date.type = "date"; date.value = day;
  start.type = "time"; start.value = at; start.step = 300;
  for (const m of [15, 30, 45, 60, 90, 120]) {
    const o = el("option", "", m + " мин"); o.value = m; if (m === 30) o.selected = true; minutes.append(o);
  }
  for (const [v, t] of CATEGORIES) { const o = el("option", "", t); o.value = v; category.append(o); }
  emails.placeholder = "Адреса через запятую — для тех, кого нет в оргструктуре";
  link.type = "url"; link.placeholder = "Ссылка на комнату Телемоста (можно позже)";
  avatar.type = "checkbox";
  const people = el("div", "people-pick", hint("Загрузка…"));
  (peopleCache ? Promise.resolve(peopleCache) : api("/api/people").then(r => (peopleCache = r.people)))
    .then(list => people.replaceChildren(...list.map(p => {
      const box = el("input"); box.type = "checkbox"; box.value = p.name;
      return el("label", "pick", box, el("span", "", p.name), el("small", "", p.position));
    }))).catch(e => people.replaceChildren(el("p", "error", e.message)));
  const form = el("form", "stack");
  const save = formSubmit(form, "Назначить и разослать приглашения", () => submitAction(form, error, async () => {
    const body = {title: title.value.trim(), start: date.value + "T" + start.value, minutes: Number(minutes.value),
      category: category.value, link: link.value.trim(), avatar: avatar.checked,
      attendees: [...people.querySelectorAll("input:checked")].map(x => x.value),
      emails: emails.value.split(/[,;\s]+/).map(x => x.trim()).filter(Boolean)};
    if (!body.title) throw new Error("Назовите встречу.");
    const m = await api("/api/meetings", "POST", body);
    dlg.close();
    announce("Встреча назначена, приглашения разосланы");
    openMeeting(m);
  }));
  save.classList.add("primary");
  const cancel = button("Отмена", () => dlg.close(), "quiet");
  form.append(el("h2", "", "Новая встреча"), field("Название", title),
    el("div", "row-3", field("День", date), field("Начало", start), field("Длительность", minutes)),
    field("Категория", category), el("div", "field", el("span", "", "Участники"), people), field("Ещё участники", emails),
    field("Телемост", link), telemostLinkTool(link, error), el("label", "check", avatar, el("span", "", "Ведёт мой ИИ-аватар — зайдёт в Телемост сам; я могу подключиться и перехватить разговор")),
    error, el("div", "actions", save, cancel));
  dlg.append(form);
  dlg.addEventListener("close", () => dlg.remove());
  document.body.append(dlg);
  dlg.showModal();
  title.focus();
}

async function mutateProtocol(id, suffix, payload) {
  await api("/api/protocols/" + encodeURIComponent(id) + suffix, "POST", payload);
  announce({"/approve": "Протокол утверждён и разослан", "/reject": "Протокол отклонён", "/edit": "Правка внесена"}[suffix]);
}

function backLink(text = "← Календарь") {
  const a = el("a", "back-link", text);
  a.href = "#meetings";
  return a;
}

function calendarView(host) {
  load(host, async mount => {
    const {day: today} = await api("/api/today");
    if (!mount.isConnected) return;
    let day = calendarDay || today;
    const board = el("div"), toolbar = el("div", "calendar-toolbar");
    mount.replaceChildren(el("article", "card t-card calendar", toolbar, board));
    function move(next) { day = calendarDay = next; refresh(); }
    function refresh() {
      const daily = phone.matches, start = daily ? day : monday(day), step = daily ? 1 : 7;
      const fullDate = {day: "numeric", month: "long", year: "numeric"};
      const label = daily ? dateLabel(day, {...fullDate, weekday: "short"}) :
        dateLabel(start) + " — " + dateLabel(shiftDay(start, 6), fullDate);
      const prev = button("‹", () => move(shiftDay(day, -step)), "arrow-button");
      const next = button("›", () => move(shiftDay(day, step)), "arrow-button");
      prev.setAttribute("aria-label", daily ? "Предыдущий день" : "Предыдущая неделя");
      next.setAttribute("aria-label", daily ? "Следующий день" : "Следующая неделя");
      const add = button("+ Новая встреча", () => newMeetingDialog(day, "10:00"), "primary");
      const protocols = el("a", "button-ghost", "Протоколы"); protocols.href = "#meetings/protocols";
      const tasks = el("a", "button-ghost", "Задачи"); tasks.href = "#meetings/tasks";
      toolbar.replaceChildren(el("div", "", el("span", "view-label", daily ? "День" : "Неделя"), el("h2", "", label),
        hint(daily ? "Нажмите на встречу, чтобы открыть её" : "Нажмите на встречу, чтобы открыть её, или на свободное время — чтобы назначить новую")),
        el("div", "actions", prev, button("Сегодня", () => move(today)), next, add, protocols, tasks));
      load(board, async area => {
        const data = await api("/api/meetings?" + new URLSearchParams({start, days: step}));
        data.focus = [];                  // окна для фокуса не показываем (решение Пита 27.11)
        if (!area.isConnected) return;
        area.replaceChildren(daily ? dayCalendar(data, day, () => {}) : weekCalendar(data, today, () => {}));
      });
    }
    phone.onchange = refresh;
    refresh();
  });
}

function meetingView(host, uid) {
  const transcript = transcriptForm(id => { announce("Черновик протокола №" + id + " готов"); meetingView(host, uid); });
  load(host, async mount => {
    const d = await api("/api/meetings/" + encodeURIComponent(uid));
    const m = d.meeting;
    calendarDay = m.start.slice(0, 10);
    document.title = m.title + " · Кабинет руководителя";
    const refresh = () => meetingView(host, uid);
    const mutate = async (id, suffix, payload) => { await mutateProtocol(id, suffix, payload); refresh(); };
    const head = card(m.title, "meetings",
      meta(dateLabel(m.start, {weekday: "long", day: "numeric", month: "long"}) + ", " + interval(m),
        badge(readable(m.category)), m.avatar ? badge("ведёт ИИ-аватар") : null),
      el("h3", "", "Участники"), bullets(m.attendees.map(a => a.name || a.email), "Участники не указаны."));
    const telemost = card("Как проходит встреча", "focus", telemostBlock(m, refresh));
    const protocol = card("Протокол", "file", d.protocol ? protocolView(d.protocol, mutate) : hint("Протокола пока нет."),
      fold("Добавить расшифровку вручную", transcript.form));
    transcript.link(m);
    const prep = card("Подготовка к встрече", "list",
      el("h3", "", "Вопросы по прошлому протоколу"), bullets(d.previous_questions, "Вопросов нет."),
      el("h3", "", "Вопросы из истории"), bullets(d.history_questions, "Вопросов нет."),
      el("h3", "", "Задачи участников"), rows(d.tasks, taskRow, "Открытых задач нет."));
    const live = el("div");
    mount.replaceChildren(backLink(), live, el("div", "dashboard", stack(head, protocol), stack(telemost, prep)));
    liveProtocol(live, m.uid);
  });
}

// Протокол по ходу встречи: пока аватар на встрече — живая расшифровка с именами и черновик договорённостей.
function liveProtocol(host, uid) {
  let timer = null;
  async function tick() {
    if (!host.isConnected) { clearInterval(timer); return; }
    let r;
    try { r = await api("/api/robot?" + new URLSearchParams({uid})); } catch (e) { return; }
    if (r.state !== "running") { host.replaceChildren(); return; }
    const lv = r.live || {}, pr = lv.protocol;
    const lines = (lv.transcript || []).filter(x => !/^\d\d:\d\d:\d\d$/.test(x));
    const talk = el("div", "live-talk", ...(lines.length ? lines.slice(-12).map(x => el("p", "", x)) : [hint("Пока тихо.")]));
    const draft = pr ? stack(
      el("h3", "", "Договорённости"), bullets(pr.decisions, "Пока нет."),
      el("h3", "", "Задачи"), rows(pr.tasks, t => el("div", "row", el("strong", "", t.what),
        hint(t.owner + " · до " + dateLabel(t.due) + (t.default_due ? " (срок не назван)" : ""))), "Пока нет."),
      el("h3", "", "Открытые вопросы"), bullets(pr.open_questions, "Пока нет."),
      hint("Черновик обновлён " + time(lv.updated_at) + ". Итоговый протокол придёт в Telegram после встречи.")) :
      hint("Черновик договорённостей появится через пару минут разговора.");
    host.replaceChildren(card("Встреча идёт · протокол по ходу", "file",
      meta(r.status, r.participants.length ? "Участники: " + r.participants.join(", ") : null),
      el("div", "dashboard live", stack(el("h3", "", "Кто что говорит"), talk), draft)));
  }
  tick();
  timer = setInterval(tick, 15000);
}

function protocolsView(host) {
  load(host, async mount => {
    const {protocols} = await api("/api/protocols");
    const again = () => protocolsView(host);
    mount.replaceChildren(backLink(), card("Протоколы встреч", "file", rows(protocols, p => fold(
      el("span", "summary-body", el("strong", "", p.title), el("small", "", stamp(p.meeting_end) + " · " + p.status)),
      protocolView(p, async (id, s, b) => { await mutateProtocol(id, s, b); again(); })), "Протоколов пока нет.")));
  });
}

function tasksView(host) {
  load(host, async mount => {
    const {tasks} = await api("/api/tasks");
    mount.replaceChildren(backLink(), card("Задачи и сроки", "list", rows(tasks, taskRow, "Задач пока нет.")));
  });
}

function meetingsPage(host, sub = "") {
  if (sub.startsWith("m/")) return meetingView(host, decodeURIComponent(sub.slice(2)));
  if (sub.startsWith("t/")) return taskView(host, decodeURIComponent(sub.slice(2)));
  if (sub === "protocols") return protocolsView(host);
  if (sub === "tasks") return tasksView(host);
  return calendarView(host);
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
  host.append(el("div", "dashboard", illus(card("Поиск по памяти", "memory", form, results), "memory"), changes));
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
      illus(card("Дополнительные метрики", "chart",
        el("p", "", "Писем без руководителя · " + metrics.letters_without_leader),
        el("p", "", "Доля писем без руководителя · " + (metrics.share_without_leader == null ? "—" :
          (metrics.share_without_leader * 100).toLocaleString("ru-RU", {maximumFractionDigits: 2}) + " %")),
        el("p", "", "До утверждения протокола, мин · " + (metrics.protocol_minutes_to_approved ?? "—"))), "chart")), rules), log);
  });
}

const pages = {today: todayPage, mail: mailPage, meetings: meetingsPage, memory: memoryPage, profile: profilePage};
function route() {
  const hash = location.hash.slice(1);
  const head = hash.split("/")[0], sub = hash.slice(head.length + 1);
  const key = Object.hasOwn(pages, head) ? head : "today";
  if (head !== key) history.replaceState(null, "", "#" + key);
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
    pages[key](host, key === head ? sub : "");
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
