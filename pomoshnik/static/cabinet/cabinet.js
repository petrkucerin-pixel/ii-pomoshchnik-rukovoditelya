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
    due.value = p.proposal ? p.proposal.due : t.due;        // предложенная исполнителем дата — уже в поле
    due.setAttribute("aria-label", "Новый срок");
    const act = (action, body = {}) => submitAction(actions, error, async () => {
      await api("/api/tasks/" + encodeURIComponent(id) + "/action", "POST", {action, ...body});
      announce({remind: "Напоминание отправлено", move: "Срок перенесён", done: "Задача закрыта"}[action]);
      again();
    });
    if (t.status !== "выполнена") {
      actions.append(button("Спросить ещё раз: " + t.owner.split(" ")[0], () => act("remind")),
        el("label", "task-move", due, button("Перенести срок", () => act("move", {due: due.value}))),
        button("Выполнена", () => act("done"), "quiet"));
    }
    const letter = x => el("div", "row", meta(x.from, x.at ? stamp(x.at) : "", x.route), el("strong", "", x.subject));
    const fact = f => el("div", "row", meta(f.who, f.when, f.source), el("p", "", f.what), el("blockquote", "", f.quote));
    const back = backTo(location.hash.slice(1).split("/")[0] || "today", "Назад");
    mount.replaceChildren(back,
      card(t.task, "list",
        meta(t.owner, el("span", t.overdue ? "late" : "", "до " + dateLabel(t.due)), t.status, "откуда: " + t.source),
        t.why ? el("p", "task-why-main", el("strong", "", "Почему просрочена: "), t.why) : null,
        p.flags.length ? bullets(p.flags, "") : null,
        el("h3", "", "Что дальше"), el("p", "", n.text),
        p.answer ? el("div", "risk-src", meta(t.owner, stamp(p.answer.at)), el("strong", "", p.answer.subject),
          el("p", "", p.answer.body)) : null,
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
    // Пит 28.11: «Встречи дня не обновились» — прошедшие приглушены, текущая и следующая отмечены, раз в минуту
    const dayGroups = {};
    for (const m of d.upcoming || []) (dayGroups[m.start.slice(0, 10)] ||= []).push(m);
    const dayTitle = day => day === d.day ? "Сегодня" : dateLabel(day, {weekday: "long", day: "numeric", month: "long"});
    const schedule = el("div", "", ...Object.entries(dayGroups).map(([day, ms]) => el("div", "upcoming-day",
      el("h3", "", dayTitle(day)), rows(ms, m => {
      const row = button(el("span", "time-row", el("strong", "", interval(m)), el("span", "", m.title),
        el("span", "when-chip"), icon("arrow")), () => openMeeting(m), "row-link");
      if (day === d.day) { row.dataset.start = minute(m.start); row.dataset.end = minute(m.end); }
      return row;
    }, ""))));
    if (!Object.keys(dayGroups).length) schedule.append(hint("Ближайших встреч нет."));
    const mark = () => {
      const now = new Date(), cur = now.getHours() * 60 + now.getMinutes();
      let nextSet = false;
      schedule.querySelectorAll("[data-start]").forEach(r => {
        const s = +r.dataset.start, e = +r.dataset.end, chip = r.querySelector(".when-chip");
        const state = e <= cur ? "past" : s <= cur ? "now" : !nextSet ? "next" : "";
        if (state === "next") nextSet = true;
        r.classList.toggle("meeting-past", state === "past");
        r.classList.toggle("meeting-now", state === "now");
        chip.textContent = {past: "прошла", now: "идёт сейчас", next: "следующая"}[state] || "";
      });
    };
    mark();
    const ticker = setInterval(() => { if (schedule.isConnected) mark(); else clearInterval(ticker); }, 60 * 1000);
    illus(queue, "mail");
    const risks = card("Риски", "alert", (d.risk_items || []).length
      ? el("div", "rows", d.risk_items.map(riskItem)) : bullets(d.risks, "Рисков не зафиксировано."));
    risks.classList.add("t-card--coral");
    mount.replaceChildren(hero(d, queue), metricCards(d.metrics),
      el("div", "dashboard", stack(queue, risks,
        card("Задачи", "list", el("h3", "", "Просроченные"), rows(d.overdue, taskRow, "Просроченных задач нет."),
          el("h3", "", "Ближайшие"), rows(d.due_soon, taskRow, "Ближайших сроков нет."))),
      stack(illus(card("Ближайшие встречи", "meetings", schedule), "calendar"),
        card("Сводка помощника за сегодня", "mail",
          el("p", "reason", "Что помощник сделал сам, без вас: передал письма, ответил, прочитал отчёты."),
          bullets(d.digest, "Сегодня помощник ещё ничего не делал сам."),
          d.digest_earlier ? el("p", "muted", "Ранее — " + d.digest_earlier + " " +
            plural(d.digest_earlier, ["действие", "действия", "действий"]) + ". ",
            Object.assign(el("a", "", "Журнал действий"), {href: "#profile/journal"})) : null))));
    mount.prepend(el("div", "updated-line", hint("Обновлено в " + time(d.updated_at) + " · обновляется само раз в час"),
      button("Обновить сейчас", () => refresh(), "button-ghost")));
  });
  refresh();
  // Пит 28.11: «Тут нужна актуальность. Обновление раз в час»
  const hourly = setInterval(() => { if (host.isConnected) refresh(); else clearInterval(hourly); }, 3600 * 1000);
}

// Почта как в Outlook (Пит 27–28.11): папки слева, список «От / Тема / Получено», область чтения справа.
// «Отсортированные / Другие» — как сработал фильтр помощника; метка у письма — что с ним сделано.
const statusChip = st => st ? el("span", "status-chip status-" + st.kind, st.label) : null;

const MAIL_FOLDERS = [["inbox", "Входящие"], ["drafts", "Черновики"], ["sent", "Отправленные"],
  ["newsletter", "Рассылки"], ["spam", "Нежелательная почта"], ["trash", "Удалённые"], ["style", "Как я пишу"]];

function folderOf(m) {
  if (m.deleted) return "trash";
  if (m.direction === "out") return "sent";
  if (m.group === "spam") return "spam";
  if (m.group === "newsletter") return "newsletter";
  return "inbox";
}

function initials(name) {
  const words = (name || "?").replace(/[«»"]/g, "").split(/\s+/).filter(Boolean);
  return ((words[0] || "?")[0] + (words.length > 1 ? words[1][0] : "")).toUpperCase();
}

function avatar(name) {
  let h = 0;
  for (const ch of name || "") h = (h * 31 + ch.charCodeAt(0)) % 360;
  const a = el("span", "mail-avatar", initials(name));
  a.style.background = `hsl(${h} 55% 85%)`;
  return a;
}

function received(iso, today) {
  if (!iso) return "";
  const d = iso.slice(0, 10);
  return d === today ? iso.slice(11, 16) : d.slice(8, 10) + "." + d.slice(5, 7) + "." + d.slice(0, 4);
}

// Вложения: файл → base64 для отправки; ссылки на вложения письма.
const readFile = file => new Promise((ok, fail) => {
  const r = new FileReader();
  r.onload = () => ok({name: file.name, type: file.type || "application/octet-stream", data: String(r.result)});
  r.onerror = () => fail(new Error("не удалось прочитать файл " + file.name));
  r.readAsDataURL(file);
});
const fileSize = n => n < 1024 ? n + " Б" : n < 1048576 ? Math.round(n / 1024) + " КБ" : (n / 1048576).toFixed(1) + " МБ";
function attachmentLinks(files) {
  if (!files || !files.length) return null;
  return el("div", "mail-files", files.map(f => {
    const a = el("a", "mail-file", "📎 " + f.name + " · " + fileSize(f.size));
    a.href = "/api/mail/file/" + f.key + "/" + encodeURIComponent(f.name);
    a.setAttribute("download", f.name);
    return a;
  }));
}

// Письмо: новое, ответ или черновик. Ответ уже написан помощником — руководитель правит и отправляет.
// Если письмо ждёт его решения, вариантов два: «Согласие» и «Отказ», отправка закрывает вопрос.
function composeForm(contacts, init, onDone, onCancel) {
  const form = el("form", "mail-compose");
  const to = el("input"), subject = el("input"), body = el("textarea"), error = feedback();
  const files = el("input"), chosen = el("div", "mail-files");
  const list = el("datalist");
  list.id = "mail-contacts";
  for (const c of contacts) list.append(new Option(c.name + " · " + c.note, c.email));
  to.setAttribute("list", "mail-contacts");
  to.type = "email";
  to.required = true;
  to.value = init.to || "";
  subject.value = init.subject || "";
  body.rows = 10;
  body.value = init.body || "";
  files.type = "file";
  files.multiple = true;
  files.setAttribute("aria-label", "Вложения");
  files.addEventListener("change", () => chosen.replaceChildren(...[...files.files].map(f =>
    el("span", "mail-file", "📎 " + f.name + " · " + fileSize(f.size)))));
  const field = (label, input) => el("label", "field", el("span", "", label), input);
  const note = el("p", "reason");
  let suggested = "", decision = null, verdict = "yes";
  const variants = el("div", "segmented");
  function pick(v) {
    verdict = v;
    body.value = v === "yes" ? decision.yes : decision.no;
    suggested = body.value;
    variants.replaceChildren(toggle("Согласие", v === "yes", () => pick("yes")), toggle("Отказ", v === "no", () => pick("no")));
  }
  if (init.reply_to && !init.draft_id) {
    body.value = "Готовлю ответ…";
    body.disabled = true;
    api("/api/mail/" + encodeURIComponent(init.reply_to) + "/reply-draft").then(d => {
      body.disabled = false;
      if (d.mode === "decision") {
        decision = d;
        note.textContent = "Письмо ждёт вашего решения. Выберите вариант, поправьте текст — отправка закроет вопрос." +
          (d.advice && d.advice !== "нет совета" ? " Совет помощника: " + d.advice + "." : "");
        pick(d.advice === "отказать" ? "no" : "yes");
      } else {
        body.value = d.text;
        suggested = d.text;
        note.textContent = "Ответ подготовил помощник — поправьте и отправьте.";
      }
      if (d.unverified && d.unverified.length) note.textContent += " Проверьте числа: " + d.unverified.join(", ") + ".";
    }).catch(() => { body.disabled = false; body.value = ""; note.textContent = "Не удалось подготовить ответ — напишите сами."; });
  }
  const send = button("Отправить", () => submitAction(form, error, async () => {
    if (decision) {
      await api("/api/decisions/" + encodeURIComponent(decision.event_id), "POST", {verdict, text: body.value});
    } else {
      const attached = await Promise.all([...files.files].map(readFile));
      await api("/api/mail/send", "POST", {to: to.value, subject: subject.value, body: body.value,
        reply_to: init.reply_to || null, files: attached, draft_id: init.draft_id || null, suggested});
    }
    announce("Письмо отправлено");
    onDone("sent");
  }), "primary");
  const save = button("Сохранить черновик", () => submitAction(form, error, async () => {
    await api("/api/mail/drafts", "POST", {draft_id: init.draft_id || null, to: to.value, subject: subject.value,
      body: body.value, reply_to: init.reply_to || null});
    announce("Черновик сохранён");
    onDone("drafts");
  }));
  const drop = init.draft_id ? button("Удалить черновик", () => submitAction(form, error, async () => {
    await api("/api/mail/drafts/" + encodeURIComponent(init.draft_id), "DELETE");
    onDone("drafts");
  }), "quiet") : null;
  form.addEventListener("submit", e => { e.preventDefault(); send.click(); });
  form.append(el("h2", "mail-read-subject", init.draft_id ? "Черновик" : init.reply_to ? "Ответ" : "Новое сообщение"), list,
    variants, note, field("Кому", to), field("Тема", subject), field("Текст", body),
    field("Вложения", files), chosen,
    hint("Подпись «— " + (init.leader || "руководитель") + "» добавится сама."),
    el("div", "actions", send, save, drop, button("Отмена", onCancel, "quiet")), error);
  setTimeout(() => (init.to ? body : to).focus(), 0);
  return form;
}

function readingPane(m, letters, onBack, act) {
  const addr = (name, email) => [name, email ? "<" + email + ">" : ""].filter(Boolean).join(" ");
  const replies = m.direction === "in" ? letters.filter(x => x.direction === "out" && x.reply_to === m.id) : [];
  const source = m.direction === "out" && m.reply_to ? letters.find(x => x.direction === "in" && x.id === m.reply_to) : null;
  const who = m.direction === "out" ? (m.to_name || m.to_email) : m.from_name;
  const mine = m.direction === "in" && m.status && m.status.kind === "you";   // рамка — только у входящих
  const tools = act ? el("div", "mail-read-tools",
    m.direction === "in" && !m.deleted ? button("Ответить", () => act.reply(m), "primary") : null,
    m.deleted ? button("Восстановить", () => act.restore(m)) : button("Удалить", () => act.remove(m))) : null;
  return el("article", "mail-read" + (mine ? " mail-read--you" : ""),
    onBack ? button("← К списку", onBack, "quiet back-link") : null, tools,
    mine ? el("p", "mail-you-rule", el("strong", "", "Нужно ваше решение. "),
      "Правило: " + (m.category ? readable(m.category) + " — " : "") + (m.reason || "решает руководитель")) : null,
    el("h2", "mail-read-subject", m.subject || "(без темы)"),
    el("div", "mail-read-head", avatar(who),
      el("div", "mail-read-from", el("strong", "", addr(m.from_name, m.from_email)),
        hint("Кому: " + addr(m.to_name, m.to_email))),
      el("small", "mail-read-date", stamp(m.at))),
    m.status ? el("p", "mail-read-status", el("strong", "", "Что с письмом: "), statusChip(m.status)) : null,
    el("div", "mail-read-body verbatim", m.body),
    attachmentLinks(m.attachments),
    m.reason && !mine ? hint("Почему так: " + m.reason) : null,
    source ? el("div", "mail-thread", el("h3", "", "В ответ на"),
      el("p", "", el("strong", "", source.from_name + ": "), source.subject)) : null,
    replies.length ? el("div", "mail-thread", el("h3", "", "Что ушло по этому письму"),
      ...replies.map(r => el("div", "risk-src", meta("→ " + (r.to_name || r.to_email), stamp(r.at)), statusChip(r.status),
        el("strong", "", r.subject), el("p", "", r.body), attachmentLinks(r.attachments)))) : null);
}

function mailPage(host, folder = "") {
  if (!MAIL_FOLDERS.some(([k]) => k === folder)) folder = "inbox";
  load(host, async mount => {
    const [{letters, groups}, td, book, {drafts}] = await Promise.all([api("/api/mail"), api("/api/today"),
      api("/api/contacts").catch(() => ({contacts: []})), api("/api/mail/drafts").catch(() => ({drafts: []}))]);
    const today = td.day;
    const titles = Object.fromEntries((groups || []).map(g => [g.key, g.title]));
    const draftRows = drafts.map(d => ({id: d.draft_id, direction: "draft", at: d.at, to_name: "", to_email: d.to,
      from_name: "", subject: d.subject, body: d.body, draft: d}));
    // Новые сверху — по времени, когда письмо пришло в кабинет (учебные письма заказчика датированы «будущим» декабрём).
    const fresh = m => Date.parse(m.arrived || m.at) || 0;
    const inFolder = f => (f === "drafts" ? draftRows : letters.filter(m => folderOf(m) === f))
      .sort((a, b) => fresh(b) - fresh(a) || (b.seq || 0) - (a.seq || 0) || (Date.parse(b.at) || 0) - (Date.parse(a.at) || 0));
    const unread = () => inFolder("inbox").filter(m => m.direction === "in" && !m.read).length;
    const youCount = unread();
    const side = el("div", "mail-side");
    side.setAttribute("role", "navigation");
    side.setAttribute("aria-label", "Папки почты");
    side.append(...MAIL_FOLDERS.map(([key, name]) => {
      const n = key === "style" ? "" : key === "inbox" ? (youCount || "") : inFolder(key).length;
      const b = button(el("span", "mail-folder", el("span", "", name), n !== "" ? el("small", "", String(n)) : null),
        () => mailPage(host, key), "mail-folder-btn");
      b.dataset.folder = key;
      b.setAttribute("aria-pressed", String(key === folder));
      return b;
    }));
    const listCol = el("section", "mail-col"), pane = el("section", "mail-pane");
    mount.replaceChildren(el("div", "mail-outlook" + (folder === "style" ? " mail-outlook--wide" : ""), side, listCol, pane));
    if (folder === "style") {
      listCol.remove();
      stylePage(pane);
      return;
    }
    let tab = "focused", who = "all", opened = null;
    const search = el("input");
    search.type = "search";
    search.placeholder = "Поиск";
    search.setAttribute("aria-label", "Поиск по почте");
    const filter = el("select");
    filter.setAttribute("aria-label", "Фильтр");
    for (const [k, name] of [["all", "Фильтр: все"], ["you", "Ждут вашего решения"],
      ["counterparty", titles.counterparty || "Контрагенты"], ["team", titles.team || "Отделы: участники совещаний"],
      ["internal", titles.internal || "Отделы: другие"]]) filter.append(new Option(name, k));
    filter.addEventListener("change", () => { who = filter.value; render(); });
    const tabs = el("div", "mail-tabs");
    const list = el("div", "mail-list");
    const head = el("div", "mail-head", el("span", ""), el("span", "", folder === "inbox" ? "От" : "Кому"),
      el("span", "", "Тема"), el("span", "", folder === "inbox" ? "Получено" : "Дата"));
    const focused = m => m.direction !== "in" || !m.status || m.status.kind !== "info";
    const back = () => { pane.hidden = true; listCol.hidden = false; };
    const showPane = node => {
      pane.replaceChildren(node);
      if (phone.matches) { listCol.hidden = true; pane.hidden = false; window.scrollTo(0, 0); }
    };
    const compose = init => showPane(composeForm(book.contacts, {...init, leader: td.leader},
      next => api("/api/mail/refresh", "POST").catch(() => {}).then(() => mailPage(host, next || folder)),
      () => (opened && folder !== "drafts" ? open(letters.find(x => x.id === opened)) : (phone.matches ? back() : pane.replaceChildren()))));
    const act = {
      reply: m => compose({to: m.from_email, reply_to: m.id,
        subject: /^re:/i.test(m.subject || "") ? m.subject : "Re: " + (m.subject || "")}),
      remove: async m => { await api("/api/mail/" + encodeURIComponent(m.id) + "/delete", "POST"); announce("Письмо удалено"); mailPage(host, folder); },
      restore: async m => { await api("/api/mail/" + encodeURIComponent(m.id) + "/restore", "POST"); announce("Письмо восстановлено"); mailPage(host, folder); },
    };
    function open(m) {
      if (!m) return;
      opened = m.id;
      list.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.id === m.id)));
      if (m.draft) {
        compose({draft_id: m.draft.draft_id, to: m.draft.to, subject: m.draft.subject, body: m.draft.body,
          reply_to: m.draft.reply_to});
        return;
      }
      showPane(readingPane(m, letters, phone.matches ? back : null, act));
      if (m.direction === "in" && !m.read) {        // открыл — прочитано: счётчик «Входящих» уменьшается
        m.read = true;
        api("/api/mail/" + encodeURIComponent(m.id) + "/read", "POST").catch(() => {});
        const row = list.querySelector(`[data-id="${CSS.escape(m.id)}"]`);
        if (row) row.classList.remove("mail-row-btn--unread");
        const badge = side.querySelector("[data-folder=inbox] small");
        const n = unread();
        if (badge) badge.textContent = n ? String(n) : "";
      }
    }
    function render() {
      const q = search.value.trim().toLowerCase();
      const all = inFolder(folder);
      const split = folder === "inbox";
      tabs.replaceChildren(...(split ? [["focused", "Отсортированные"], ["other", "Другие"]].map(([k, name]) => {
        const n = all.filter(m => (k === "focused") === focused(m)).length;
        return toggle(name + " · " + n, tab === k, () => { tab = k; render(); });
      }) : []));
      const items = all
        .filter(m => !split || (tab === "focused") === focused(m))
        .filter(m => who === "all" || (who === "you" ? (m.status && m.status.kind === "you") : m.group === who))
        .filter(m => !q || [m.from_name, m.from_email, m.to_name, m.to_email, m.subject, m.body].join(" ").toLowerCase().includes(q));
      list.replaceChildren(...(items.length ? items.map(m => {
        const name = m.direction === "in" ? m.from_name : "→ " + (m.to_name || m.to_email || "(без адреса)");
        const mine = m.direction === "in" && m.status && m.status.kind === "you";   // рамка — только у входящих
        const clip = m.attachments && m.attachments.length ? "📎 " : "";
        const b = button(el("span", "mail-row" + (mine ? " mail-row--you" : ""),
          avatar(m.direction === "in" ? m.from_name : (m.to_name || m.to_email || "?")),
          el("span", "mail-row-from", name),
          el("span", "mail-row-subject", clip + (m.subject || "(без темы)")),
          el("span", "mail-row-date", received(m.at, today)),
          el("span", "mail-row-preview", (m.body || "").replace(/\s+/g, " ").slice(0, 140)),
          el("span", "mail-row-tag", m.draft ? el("span", "status-chip status-info", "Черновик") : statusChip(m.status))),
          () => open(m), "mail-row-btn" + (mine ? " mail-row-btn--you" : "") + (m.direction === "in" && !m.read ? " mail-row-btn--unread" : ""));
        b.dataset.id = m.id;
        b.setAttribute("aria-pressed", String(m.id === opened));
        return b;
      }) : [hint(q ? "Ничего не найдено." : "В папке пока нет писем.")]));
      if (!phone.matches && folder !== "drafts" && items.length && !items.some(m => m.id === opened)) open(items[0]);
      if (!items.length) pane.replaceChildren();
    }
    search.addEventListener("input", render);
    pane.hidden = phone.matches;
    const refresh = button("Обновить", () => submitAction(refresh.parentNode, el("p"), async () => {
      await api("/api/mail/refresh", "POST");
      mailPage(host, folder);
    }));
    refresh.setAttribute("aria-label", "Проверить почту сейчас");
    listCol.replaceChildren(el("div", "mail-new", button("Новое сообщение", () => compose({}), "primary"), refresh),
      el("div", "mail-tools", search, filter), tabs, head, list);
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
  const days = Array.from({length: 7}, (_, i) => shiftDay(data.start, i));     // 7 дней, выходные приглушены (Пит 28.11)
  const weekend = day => [0, 6].includes(new Date(day + "T12:00").getDay());
  const first = Math.min(8, ...items.map(m => Math.floor(minute(m.start) / 60)));
  const last = Math.max(20, ...items.map(m => Math.ceil(minute(m.end) / 60)));
  const board = el("div", "week-grid");
  board.style.setProperty("--days", days.length);
  board.style.setProperty("--hours", last - first);
  board.append(el("div", "axis-head"));
  for (const day of days) {
    board.append(el("div", "day-head" + (day === today ? " current" : "") + (weekend(day) ? " weekend" : ""),
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
    const col = el("div", "day-column" + (weekend(day) ? " weekend" : ""));
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

// «Назад» ведёт туда, откуда пришли (Пит 28.11: «если зашёл из профиля, то и вернуться должен в профиль»).
// Кабинет помнит путь внутри вкладки; открыли страницу по прямой ссылке — «назад» ведёт на запасное место.
const visited = [];
const PLACES = [["profile/journal", "Журнал действий"], ["profile/rules", "Правила"], ["profile/metrics", "Метрики"],
  ["profile", "Профиль"], ["meetings/protocols", "Протоколы"], ["meetings/tasks", "Задачи"], ["meetings/m/", "Встреча"],
  ["meetings/t/", "Задача"], ["meetings", "Календарь"], ["mail", "Переписка"], ["memory", "Память"], ["today/t/", "Задача"],
  ["today", "Сегодня"]];
function placeName(hash) {
  const hit = PLACES.find(([h]) => h.endsWith("/") ? hash.startsWith(h) : hash === h || hash.startsWith(h + "/"));
  return hit ? hit[1] : "Назад";
}
function remember(hash) {
  if (visited.length > 1 && visited[visited.length - 2] === hash) visited.pop();      // вернулись назад
  else if (visited[visited.length - 1] !== hash) visited.push(hash);
}
function backTo(fallbackHash, fallbackName) {
  const prev = visited.length > 1 ? visited[visited.length - 2] : null;
  const a = el("a", "back-link", "← " + (prev ? placeName(prev) : fallbackName));
  a.href = "#" + (prev || fallbackHash);
  a.addEventListener("click", e => { if (prev) { e.preventDefault(); history.back(); } });
  return a;
}

function backLink(text = "← Календарь") {
  return backTo("meetings", text.replace(/^←\s*/, ""));
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
      // Стенд живёт на учебной неделе заказчика (DEMO_DATE): говорим об этом прямо (Пит 28.11: «какое число указывать?»)
      const real = new Date().toLocaleDateString("sv");
      const demo = real !== today ? el("p", "demo-note", "Учебная неделя заказчика: письма, встречи и задачи — 7–11 декабря. " +
        "Сегодня по учебному календарю — " + dateLabel(today, {weekday: "long", day: "numeric", month: "long"}) +
        ", время настоящее. Новые встречи назначайте на эти даты: на сегодня — " + dateLabel(today) + ".") : null;
      toolbar.replaceChildren(...[demo, el("div", "", el("span", "view-label", daily ? "День" : "Неделя"), el("h2", "", label),
        hint(daily ? "Нажмите на встречу, чтобы открыть её" : "Нажмите на встречу, чтобы открыть её, или на свободное время — чтобы назначить новую")),
        el("div", "actions", prev, button("Сегодня", () => move(today)), next, add, protocols, tasks)].filter(Boolean));
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

// Большая кнопка-плитка: заголовок, короткая суть, «Открыть →». Проваливаемся внутрь раздела профиля.
function tile(href, symbol, title, lead, ...facts) {
  const a = el("a", "tile t-card", el("span", "tile-head", icon(symbol), el("strong", "", title)),
    el("p", "tile-lead", lead), facts.length ? el("div", "tile-facts", facts.map(f => el("span", "", f))) : null,
    el("span", "tile-go", "Открыть", icon("arrow")));
  a.href = href;
  return a;
}

function profileBack() {
  return backTo("profile", "Профиль");
}

// Одни подписи на плитке и внутри страницы (Пит 28.11: «Если уж заявляем, то внутри должно соответствовать»).
const ruleFacts = s => ["Решаете только вы · " + s.only_me + " " + plural(s.only_me, ["тема", "темы", "тем"]),
  "Помощник делает сам · " + s.auto + " " + plural(s.auto, ["правило", "правила", "правил"]),
  "Без вас · " + s.letters_without_you + " из " + s.letters + " " + plural(s.letters, ["письма", "писем", "писем"])];
const metricFacts = m => {
  const pct = m.letters ? Math.round(100 * m.letters_without_leader / m.letters) : 0;
  return ["Без вас · " + pct + " %", "Освобождённое время · " + nf(m.hours_saved) + " ч", "Правок руководителя · " + m.leader_edits];
};

// Профиль → Правила: что решаете только вы, что помощник делает сам и кому передаёт (Пит 28.11).
function rulesPage(host) {
  load(host, async mount => {
    const d = await api("/api/profile/rules");
    const letters = n => n ? n + " " + plural(n, ["письмо", "письма", "писем"]) : "писем пока не было";
    const s = d.summary;
    const head = el("div", "rules-head",
      el("h2", "", "Правила помощника"),
      el("p", "lead", "Помощник решает сам всё, что вы ему разрешили, и приходит к вам только с тем, что можете решить только вы."),
      el("div", "tile-facts", ...ruleFacts(s).map(t => el("span", "", t))));
    const mine = card(ruleFacts(s)[0], "check",
      el("p", "reason", "Такие письма помощник не отправляет сам: готовит карточку с советом и черновиком, вы нажимаете «Согласовать» или «Отказать»."),
      rows(d.only_me, x => el("div", "row", el("strong", "", x.title),
        x.examples ? el("p", "", x.examples) : null, hint("Пришло вам на решение: " + letters(x.letters))), "Таких правил нет."));
    const auto = d.groups.map(g => card(g.title, g.action === "делегировать" ? "arrow" : g.action === "авто" ? "mail" : "list",
      el("p", "reason", g.what),
      rows(g.rules, r => el("div", "row", el("strong", "", r.title),
        r.to ? el("p", "", "→ " + r.to.name + ", " + r.to.position) : null,
        hint("По правилу: " + letters(r.letters)),
        el("p", "muted", r.own ? "Правило самого помощника — в профиле его нет." : "В профиле: «" + r.profile_line + "»")), "")));
    const rest = card("Если правила нет", "profile", el("p", "", d.fallback.text),
      hint("Так пришло: " + letters(d.fallback.letters)),
      hint("Правила записаны в профиле руководителя (" + d.file + ") обычным текстом — менять их можно без программиста."));
    const autoHead = el("div", "rules-head", el("h3", "", ruleFacts(s)[1]),
      hint("Писем без вас по этим правилам: " + s.groups_letters + " — это цифра «Без вас» сверху."));
    mount.replaceChildren(profileBack(), head, el("div", "dashboard", stack(mine, rest), stack(autoHead, ...auto)));
  });
}

// ---------------- Профиль → Метрики: живые круговые диаграммы (Пит 28.11) ----------------
// Цвета проверены валидатором палитры (порядок фиксирован, цвет закреплён за долей); контраст с фоном ниже 3:1,
// поэтому у каждой доли есть подпись с цифрой рядом с диаграммой.
const SLICE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"];   // 6 цветов — проверены валидатором
const nf = (v, d = 1) => Number(v || 0).toLocaleString("ru-RU", {maximumFractionDigits: d});

// Кольцо из долей. update(parts, center, sub) меняет те же дуги — переход анимирует CSS.
function donut(label) {
  const NS = document.documentElement.namespaceURI.replace("1999/xhtml", "2000/svg"), R = 52, C = 2 * Math.PI * R, GAP = 2;
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 140 140");
  svg.setAttribute("role", "img");
  svg.classList.add("donut");
  const track = document.createElementNS(NS, "circle");
  Object.entries({cx: 70, cy: 70, r: R, class: "donut-track"}).forEach(([k, v]) => track.setAttribute(k, v));
  svg.append(track);
  const big = document.createElementNS(NS, "text"), small = document.createElementNS(NS, "text");
  big.setAttribute("x", 70); big.setAttribute("y", 70); big.setAttribute("class", "donut-big");
  small.setAttribute("x", 70); small.setAttribute("y", 90); small.setAttribute("class", "donut-small");
  svg.append(big, small);
  const arcs = [], legend = el("div", "donut-legend"), tip = el("div", "donut-tip");
  tip.hidden = true;
  const box = el("div", "donut-box", el("div", "donut-wrap", svg, tip), legend);
  function update(parts, center, sub) {
    const total = parts.reduce((s, p) => s + p.value, 0);
    let at = 0;
    parts.forEach((p, i) => {
      let a = arcs[i];
      if (!a) {
        a = document.createElementNS(NS, "circle");
        Object.entries({cx: 70, cy: 70, r: R, class: "donut-arc", stroke: p.color || SLICE[i]})
          .forEach(([k, v]) => a.setAttribute(k, v));
        a.addEventListener("pointerenter", () => { tip.hidden = false; tip.textContent = a.dataset.tip; });
        a.addEventListener("pointerleave", () => { tip.hidden = true; });
        svg.insertBefore(a, big);
        arcs[i] = a;
      }
      const len = total ? Math.max(0, p.value / total * C - (p.value && parts.filter(x => x.value).length > 1 ? GAP : 0)) : 0;
      a.style.strokeDasharray = len + " " + C;
      a.style.strokeDashoffset = String(-at);
      a.dataset.tip = p.label + ": " + p.text;
      at += total ? p.value / total * C : 0;
    });
    big.textContent = center;
    small.textContent = sub || "";
    svg.setAttribute("aria-label", label + ": " + parts.map(p => p.label + " " + p.text).join(", "));
    legend.replaceChildren(...parts.map((p, i) => el("div", "donut-row",
      el("span", "swatch"), el("span", "donut-name", p.label), el("strong", "", p.text))));
    legend.querySelectorAll(".swatch").forEach((s, i) => { s.style.background = parts[i].color || SLICE[i]; });
  }
  return {node: box, update};
}

function metricsPage(host) {
  const letters = donut("Куда ушли письма"), share = donut("Без вас"), hours = donut("Освобождено часов");
  const tiles = el("div", "metrics");
  const stamp_ = hint(""), facts = el("div", "tile-facts");
  host.replaceChildren(profileBack(),
    el("div", "rules-head", el("h2", "", "Метрики помощника"),
      el("p", "lead", "Сколько работы помощник забрал у вас. Цифры обновляются сами, как только он что-то делает."), facts, stamp_),
    el("div", "donuts",
      card("Без вас", "check", el("p", "reason", "Доля писем, которые помощник решил без вашего участия."), share.node),
      card("Куда ушли письма", "mail", el("p", "reason", "Что помощник сделал с каждым письмом."), letters.node),
      card("Освобождённое время", "clock", el("p", "reason", "Часы, которые вы не потратили сами. Цель по заданию — 8–12 часов в неделю."), hours.node)),
    tiles);
  async function tick() {
    if (!host.isConnected) return clearInterval(timer);
    let m;
    try { m = await api("/api/metrics"); } catch (e) { return; }
    const pct = m.letters ? Math.round(100 * m.letters_without_leader / m.letters) : 0;
    facts.replaceChildren(...metricFacts(m).map(t => el("span", "", t)));     // те же цифры, что на плитке
    share.update([{label: "Без вас", value: m.letters_without_leader, text: nf(m.letters_without_leader, 0) + " писем"},
                  {label: "Пришло к вам", value: m.letters - m.letters_without_leader, color: "#d3e0f2",
                   text: nf(m.letters - m.letters_without_leader, 0) + " писем"}], pct + " %", "из " + m.letters + " писем");
    letters.update(m.letters_by_action.map(p => ({label: p.label, value: p.value, text: nf(p.value, 0)})),
      String(m.letters), "писем");
    const left = Math.max(0, m.hours_goal - m.hours_saved);
    hours.update([...m.hours_by_activity.filter(p => p.value).map(p => ({label: p.label, value: p.value, text: nf(p.value) + " ч"})),
                  ...(left ? [{label: "До цели", value: left, color: "#d3e0f2", text: nf(left) + " ч"}] : [])],
      nf(m.hours_saved) + " ч", "цель " + m.hours_goal + " ч");
    tiles.replaceChildren(...[["file", "Встреча → протокол", m.protocol_minutes_to_draft == null ? "—" : nf(m.protocol_minutes_to_draft) + " мин"],
      ["check", "До утверждения протокола", m.protocol_minutes_to_approved == null ? "—" : nf(m.protocol_minutes_to_approved) + " мин"],
      ["edit", "Правок руководителя", nf(m.leader_edits, 0)], ["chart", "Расходы на модель", "$" + nf(m.llm_cost_usd, 2)]]
      .map(([s, label, value]) => el("div", "metric", icon(s), el("strong", "", value), el("span", "", label))));
    stamp_.textContent = "Обновлено в " + new Date().toLocaleTimeString("ru-RU", {hour: "2-digit", minute: "2-digit", second: "2-digit"});
  }
  const timer = setInterval(tick, 20000);
  tick();
}

// Профиль → Журнал действий: что сделал помощник и что решили вы, по дням (Пит 28.11).
function journalPage(host) {
  let who = "", group = "";
  const filters = el("div", "filters"), list = el("div");
  host.replaceChildren(profileBack(),
    el("div", "rules-head", el("h2", "", "Журнал действий"),
      el("p", "lead", "Всё, что помощник сделал сам, и все ваши решения. Новые сверху; одинаковые действия подряд — одной строкой.")),
    filters, list);
  async function render() {
    const d = await api("/api/profile/journal?" + new URLSearchParams({who, group}));
    filters.replaceChildren(
      el("div", "segmented", [["", "Все"], ["assistant", "Помощник · " + d.counts["Помощник"]], ["human", "Вы · " + d.counts["Вы"]]]
        .map(([k, label]) => toggle(label, who === k, () => { who = k; render(); }))),
      el("div", "segmented journal-groups", [["", "Все разделы"], ...d.groups.map(g => [g, g])]
        .map(([k, label]) => toggle(label, group === k, () => { group = k; render(); }))));
    list.replaceChildren(...(d.days.length ? d.days.map(day => card(day.label, "clock",
      rows(day.items, journalRow, ""))) : [hint("Записей нет.")]));
  }
  render();
}

// Строка журнала раскрывается: письмо, что ушло, кому и почему, протокол, встреча, текст в Telegram.
function journalRow(x) {
  const box = el("div", "row journal-row" + (x.who === "Вы" ? " journal-mine" : ""));
  const head = el("button", "journal-head", meta(time(x.at), x.who, x.group),
    el("span", "journal-text", x.text + (x.times > 1 ? "  ×" + x.times : "")), el("span", "journal-more", "Подробнее"));
  head.type = "button";
  head.setAttribute("aria-expanded", "false");
  const more = el("div", "journal-detail");
  more.hidden = true;
  head.addEventListener("click", async () => {
    const open = head.getAttribute("aria-expanded") !== "true";
    head.setAttribute("aria-expanded", String(open));
    head.querySelector(".journal-more").textContent = open ? "Свернуть" : "Подробнее";
    more.hidden = !open;
    if (!open || more.childElementCount) return;
    try {
      const d = await api("/api/profile/journal/" + x.id);
      more.replaceChildren(...[      // replaceChildren не пропускает null — убираем пустые части сами
        el("strong", "", d.title),
        x.times > 1 ? hint("Повторялось " + x.times + " раз, показана последняя запись.") : null,
        d.fields.length ? el("dl", "journal-fields", ...d.fields.flatMap(f => [el("dt", "", f.label), el("dd", "", f.value)])) : null,
        ...d.blocks.map(b => el("div", "journal-block", el("span", "muted", b.label), el("p", "letter-example", b.text))),
        d.note ? hint(d.note) : null,
        d.links.length ? el("div", "actions", ...d.links.map(l => { const a = el("a", "button-ghost", l.label); a.href = l.href; return a; })) : null].filter(Boolean));
    } catch (e) {
      more.replaceChildren(hint("Не удалось загрузить подробности."));
    }
  });
  box.append(head, more);
  return box;
}

// ---------------- Профиль: карточка руководителя, штат, мои аватары (Пит 28.11) ----------------
function leaderCard(host) {
  load(host, async mount => {
    const c = await api("/api/profile/card");
    const contact = (label, value, href) => value ? el("div", "contact", el("span", "muted", label),
      href ? Object.assign(el("a", "", value), {href}) : el("span", "", value)) : null;
    const view = card(c.name, "profile",
      el("p", "lead", c.title),
      c.about ? el("p", "", c.about) : hint("Расскажите, чем вы занимаетесь, — нажмите «Изменить»."),
      el("div", "contacts", contact("Почта", c.email, "mailto:" + c.email), contact("Телефон", c.phone, "tel:" + c.phone.replace(/[^\d+]/g, "")),
        contact("Адрес", c.address)),
      !c.phone || !c.address ? hint("Добавьте телефон и адрес — нажмите «Изменить».") : null,
      el("h3", "", "Цели"), bullets(c.goals, "Цели не указаны."),
      el("h3", "", "Как писать от моего имени"), el("p", "", c.style),
      el("h3", "", "Ритм"), el("p", "", "План дня · " + c.plan_time + " · итог недели · " + c.week_time),
      el("div", "actions", button("Изменить", () => edit(), "button-ghost")));
    function edit() {
      const form = el("form", "stack"), error = feedback();
      const inputs = {};
      const fieldOf = (key, label, type = "text", rows = 0) => {
        const input = rows ? el("textarea") : el("input");
        if (rows) input.rows = rows; else input.type = type;
        input.value = c[key] || "";
        inputs[key] = input;
        return field(label, input);
      };
      form.append(fieldOf("name", "Имя"), fieldOf("title", "Должность"), fieldOf("about", "Чем занимаюсь", "text", 3),
        fieldOf("email", "Почта", "email"), fieldOf("phone", "Телефон", "tel"), fieldOf("address", "Адрес"), error,
        el("div", "actions", formSubmit(form, "Сохранить", () => submitAction(form, error, async () => {
          await api("/api/profile/card", "PUT", Object.fromEntries(Object.entries(inputs).map(([k, i]) => [k, i.value])));
          announce("Карточка сохранена");
          leaderCard(host);
        })), button("Отмена", () => leaderCard(host), "button-ghost")));
      mount.replaceChildren(card("Изменить карточку", "edit",
        hint("Имя и должность помощник подставляет в письма и представляется ими на встречах."), form));
    }
    mount.replaceChildren(view);
  });
}

function staffPage(host) {
  load(host, async mount => {
    const d = await api("/api/profile/staff");
    const person = p => el("div", "row staff-row",
      el("strong", "", p.name), el("p", "", p.position),
      meta(p.email, p.tasks_open ? "задач в работе · " + p.tasks_open : "задач нет",
        p.tasks_late ? el("span", "late", "просрочено · " + p.tasks_late) : null,
        p.letters ? "писем · " + p.letters + (p.last_letter ? ", последнее " + dateLabel(p.last_letter) : "") : "писем не было"));
    mount.replaceChildren(profileBack(),
      el("div", "rules-head", el("h2", "", "Штат сотрудников"),
        el("p", "lead", "С кем работает помощник: кому передаёт письма, кого спрашивает о задачах и чьи письма решаете только вы.")),
      el("div", "dashboard", ...d.groups.map(g => card(g.title + " · " + g.people.length, "profile", rows(g.people, person, "Никого нет.")))),
      hint("Штат берётся из оргструктуры (" + d.total + " человек)."));
  });
}

function avatarsPage(host) {
  load(host, async mount => {
    const d = await api("/api/profile/avatars");
    const tileOf = x => {
      const img = el("div", "avatar-face");
      if (x.preview) { const i = el("img"); i.src = x.preview; i.alt = ""; i.loading = "lazy"; img.append(i); }
      else img.append(icon("profile"));
      const box = el("article", "card t-card avatar-card" + (x.current ? " avatar-current" : ""), img,
        el("strong", "", x.name), meta(x.source, x.type),
        el("p", x.ready ? "" : "late", x.current ? "Ведёт ваши встречи" : x.status_text),
        x.current ? null : button(x.ready ? "Выбрать для встреч" : "Ещё не готов", async () => {
          await api("/api/profile/avatar", "PUT", {avatar_id: x.id});
          announce("Теперь встречи ведёт " + x.name);
          avatarsPage(host);
        }, x.ready ? "" : "button-ghost"));
      if (!x.ready) box.querySelector("button").disabled = true;
      return box;
    };
    mount.replaceChildren(...[profileBack(),          // replaceChildren не пропускает null
      el("div", "rules-head", el("h2", "", "Мои аватары"),
        el("p", "lead", "Каким аватаром помощник заходит на встречи. Когда LiveAvatar обучит нового аватара на вас, он появится здесь сам.")),
      d.note ? hint(d.note) : null,
      el("div", "avatars", ...d.avatars.map(tileOf))].filter(Boolean));
  });
}

function profilePage(host, sub = "") {
  if (sub === "rules") return rulesPage(host);
  if (sub === "staff") return staffPage(host);
  if (sub === "avatars") return avatarsPage(host);
  if (sub === "journal") return journalPage(host);
  if (sub === "metrics") return metricsPage(host);
  load(host, async mount => {
    const [p, metrics, journal, r] = await Promise.all([api("/api/profile"), api("/api/metrics"),
      api("/api/journal?limit=50"), api("/api/profile/rules")]);
    const pct = metrics.letters ? Math.round(100 * metrics.letters_without_leader / metrics.letters) : 0;
    const metricsTile = tile("#profile/metrics", "chart", "Метрики",
      "Сколько работы помощник забрал у вас — круговые диаграммы, цифры обновляются сами.",
      ...metricFacts(metrics));
    const profile = el("div");
    leaderCard(profile);
    const staffTile = tile("#profile/staff", "profile", "Штат сотрудников",
      "Руководство, ваша команда и коллеги: кто на чём сейчас, у кого просрочки.", "Человек · " + (p.staff_total ?? "—"));
    const avatarsTile = tile("#profile/avatars", "edit", "Мои аватары",
      "Каким аватаром помощник заходит на встречи. Новый обученный появится сам.");
    const rules = tile("#profile/rules", "list", "Правила",
      "Что решаете только вы, что помощник делает сам и кому передаёт.",
      ...ruleFacts(r.summary));
    const last = journal.actions[0];
    const log = tile("#profile/journal", "clock", "Журнал действий",
      "Всё, что помощник сделал сам, и все ваши решения — по дням, обычными словами.",
      last ? "Последнее · " + stamp(last.at) : "Записей пока нет");
    mount.replaceChildren(profile, el("div", "tiles", rules, metricsTile, log, staffTile, avatarsTile));
  });
}

const pages = {today: todayPage, mail: mailPage, meetings: meetingsPage, memory: memoryPage, profile: profilePage};
function route() {
  const hash = location.hash.slice(1);
  remember(hash || "today");
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
