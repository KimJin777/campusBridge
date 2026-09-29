// 학생 채팅 화면(상세설계 05 §1). 모든 외부 텍스트는 textContent로만 넣는다(innerHTML 금지).
import { createSSEParser } from "./sse.js";
import { hydrateIcons, icon, chipIcon } from "./icons.js";

const SCHEMA_VERSION = 1;
const THREAD_KEY = "campusbridge.thread_id";
const ALLOWED_LINK = /^https:\/\/([a-z0-9-]+\.)*kyungnam\.ac\.kr(\/|$)/i;
const STEP_LABEL = {
  classify: "질문 의도 분석",
  plan: "필요한 근거 정리",
  compose: "답변 작성",
  verify: "근거 검증",
};
const TOOL_LABEL = {
  search_academic_knowledge: "학칙·학사안내 검색",
  get_notices: "공지 확인",
  get_academic_calendar: "학사일정 확인",
  get_menu: "식단 확인",
  find_campus_location: "위치 확인",
};
const KIND_LABEL = {
  article: "규정",
  guide: "학사안내",
  notice: "공지",
  calendar: "학사일정",
  menu: "식단",
  department: "부서",
  place: "위치",
};
const SLOT_LABEL = { grade: "학년", scholarship: "장학금", dept: "학과" };
const FALLBACK_TEXT = {
  out_of_scope: "학사·장학·학생생활 안내 범위 밖의 질문이에요.",
  deadline: "답변 시간이 너무 오래 걸려 중단했어요. 잠시 후 다시 시도해 주세요.",
  tool_failure: "학교 정보 조회에 실패했어요. 잠시 후 다시 시도해 주세요.",
};

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
const uuid = () =>
  crypto.randomUUID
    ? crypto.randomUUID()
    : "10000000-1000-4000-8000-100000000000".replace(/[018]/g, (c) =>
        (c ^ (crypto.getRandomValues(new Uint8Array(1))[0] & (15 >> (c / 4)))).toString(16),
      );

// ── 상태 ────────────────────────────────────────────────────────────────
// '새 대화 시작' 버튼을 없앴으므로(교수님 2026-09-29) 페이지를 열 때마다 새 대화로 시작한다.
// 화면은 비어 있는데 이전 대화의 학생 조건이 조용히 이어지는 일을 막는다.
try {
  localStorage.removeItem(THREAD_KEY);
} catch {}
const state = { threadId: uuid(), busy: false };

function setBusy(b) {
  state.busy = b;
  $("#send").disabled = b;
  $("#input").disabled = b;
  document.querySelectorAll(".chip, .choice, .primary").forEach((x) => (x.disabled = b));
}

function track(event, extra = {}) {
  fetch("/api/track", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ thread_id: state.threadId, event, ...extra }),
    keepalive: true,
  }).catch(() => {});
}

// ── 턴 렌더링 ───────────────────────────────────────────────────────────
class Turn {
  constructor(message) {
    const node = $("#tpl-turn").content.firstElementChild.cloneNode(true);
    hydrateIcons(node);
    $(".bubble.user", node).textContent = message;
    $("#turns").append(node);
    $("#welcome").hidden = true;
    $("#today").hidden = true; // 학사 일정 버튼은 첫 화면에서만
    this.node = node;
    this.steps = $(".steps", node);
    this.cards = $(".cards", node);
    this.answer = $(".answer", node);
    this.cardMap = new Map();
    this.message = message;
    this.calendarEvents = new Map(); // 근거 id → {title, start, end}
    this.turnId = null;
    node.scrollIntoView({ block: "end" });
  }

  step(text, cls = "") {
    this.steps.querySelectorAll("li:not(.ok):not(.fail):not(.note)").forEach((li) => li.classList.add("ok"));
    const li = el("li", cls, text);
    this.steps.append(li);
    return li;
  }

  finishSteps() {
    this.steps.querySelectorAll("li:not(.fail):not(.note)").forEach((li) => li.classList.add("ok"));
  }

  status(d) {
    if (d.step === "act" && d.tool) {
      const label = TOOL_LABEL[d.tool] || d.tool;
      if (d.ok === false) this.step(`${label} — 조회 실패`, "fail");
      else this.step(`${label} (${d.count ?? 0}건)${d.stale ? " — 마지막 확인 정보" : ""}`, "ok");
      return;
    }
    if (d.step === "classify" && Array.isArray(d.corrections)) {
      for (const c of d.corrections) this.step(`'${c.from}'을(를) '${c.to}'(으)로 이해했습니다`, "note");
      return;
    }
    const label = STEP_LABEL[d.step];
    if (label && ![...this.steps.children].some((li) => li.dataset.step === d.step)) {
      const li = this.step(label);
      li.dataset.step = d.step;
    }
  }

  addCards(items) {
    for (const c of items || []) {
      if (this.cardMap.has(c.id)) continue;
      if (c.kind === "calendar" && /^\d{4}-\d{2}-\d{2}$/.test(c.start_date || "")) {
        this.calendarEvents.set(c.id, { title: c.title, start: c.start_date, end: c.end_date || c.start_date });
      }
      const kind = Object.hasOwn(KIND_LABEL, c.kind) ? c.kind : "guide";
      const card = el("article", `card kind-${kind}`);
      card.setAttribute("role", "listitem");
      card.tabIndex = -1;
      const head = el("button", "card-head");
      head.type = "button";
      head.setAttribute("aria-expanded", "false");
      head.append(el("span", "kind", KIND_LABEL[c.kind] || c.kind), el("span", "title", c.title), el("span", "num"), el("span", "chev", "▾"));
      const detail = el("div", "card-detail");
      detail.hidden = true;
      detail.append(el("div", "snippet", c.snippet));
      const meta = [c.department, c.revision_date && `${c.revision_date} 개정`].filter(Boolean).join(" · ");
      if (meta) detail.append(el("div", "meta", meta));
      if (c.has_table) detail.append(el("div", "meta", "표 포함 — 원문 확인"));
      if (c.stale) detail.append(el("div", "meta stale", "마지막 확인 정보"));
      head.addEventListener("click", () => {
        const open = detail.hidden;
        detail.hidden = !open;
        card.classList.toggle("open", open);
        head.setAttribute("aria-expanded", String(open));
        if (open) track("card_click", { turn_id: this.turnId, target: c.id, card_state: card.classList.contains("cited") ? "cited" : "candidate" });
      });
      card.append(head, detail);
      const safe = typeof c.url === "string" && ALLOWED_LINK.test(c.url);
      const source = el("button", "source-link", "원문");
      source.type = "button";
      source.disabled = !safe;
      source.prepend(icon("external", 14));
      source.addEventListener("click", () => {
        if (!safe) return;
        window.open(c.url, "_blank", "noopener,noreferrer");
        track("source_click", { turn_id: this.turnId, target: c.id, card_state: card.classList.contains("cited") ? "cited" : "candidate" });
      });
      detail.append(source);
      this.cardMap.set(c.id, card);
      this.cards.append(card);
    }
    if (this.cardMap.size) $(".evidence-wrap", this.node).hidden = false;
  }

  markCited(cited) {
    const order = new Map(cited.map((id, i) => [id, i + 1]));
    let folded = 0;
    for (const [id, card] of this.cardMap) {
      if (order.has(id)) {
        card.classList.add("cited");
        $(".num", card).textContent = `[${order.get(id)}]`;
      } else {
        card.classList.add("folded");
        folded += 1;
      }
    }
    // 인용 순서대로 앞에 배치
    [...this.cardMap.entries()]
      .filter(([id]) => order.has(id))
      .sort((a, b) => order.get(a[0]) - order.get(b[0]))
      .reverse()
      .forEach(([, card]) => this.cards.prepend(card));
    const more = $(".more-cards", this.node);
    if (folded) {
      more.hidden = false;
      more.textContent = `검색 후보 ${folded}건 더 보기`;
      more.onclick = () => {
        this.cards.querySelectorAll(".folded").forEach((c) => c.classList.remove("folded"));
        more.hidden = true;
      };
    }
    return order;
  }

  sentence(s, order) {
    const p = el("p", null, s.text);
    for (const id of s.cite_ids || []) {
      const n = order.get(id);
      if (!n) continue;
      const b = el("button", "cite", String(n));
      b.type = "button";
      b.setAttribute("aria-label", `근거 ${n}`);
      b.addEventListener("click", () => {
        const card = this.cardMap.get(id);
        if (card && !card.classList.contains("open")) $(".card-head", card).click();
        card?.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
        card?.focus();
        track("card_click", { turn_id: this.turnId, target: id, card_state: "cited" });
      });
      p.append(b);
    }
    return p;
  }

  dept(d) {
    if (!d) return null;
    const box = el("div", "dept");
    const head = el("div", "dept-head");
    head.append(icon("building", 20), el("strong", null, d.name));
    box.append(head);
    if (d.location_text) box.append(el("div", "dept-location", d.location_text));
    if (d.duties) box.append(el("div", "dept-duties", d.duties));
    const phone = typeof d.phone === "string" ? d.phone.trim() : "";
    const dial = phone.replace(/[^0-9+]/g, "");
    if (phone && dial) {
      const call = el("a", "phone-link");
      call.href = `tel:${dial}`;
      call.append(icon("phone", 15), document.createTextNode(phone));
      box.append(call);
    }
    if (d.snapshot_at) box.append(el("div", "asof", `부서 정보 확인일 ${d.snapshot_at}`));
    return box;
  }

  renderDept(d) {
    const slot = $(".dept-slot", this.node);
    slot.replaceChildren();
    const dept = this.dept(d);
    if (dept) slot.append(dept);
  }

  // 검증 전 초안(스트리밍) — 확정 답변이 아니므로 "검증 중"으로만 보이고 answer/fallback이 오면 교체된다
  renderDraft(texts) {
    const box = this.answer;
    if (!texts?.length) {
      box.classList.remove("drafting");
      box.replaceChildren();
      return;
    }
    box.classList.add("drafting");
    box.replaceChildren(el("p", "draft-badge", "근거 확인 중 · 아직 확정 답변이 아닙니다"));
    for (const t of texts) box.append(el("p", null, t));
  }

  renderAnswer(a) {
    this.finishSteps();
    const order = this.markCited(a.cited || []);
    const box = this.answer;
    box.replaceChildren();
    for (const n of a.notices || []) box.append(el("div", "notice-box", n.text));
    for (const s of a.sentences || []) box.append(this.sentence(s, order));
    if (a.checklist?.length) {
      box.append(el("h3", null, "체크리스트"));
      const ol = el("ol", "checklist");
      for (const s of a.checklist) {
        const li = el("li");
        li.append(...this.sentence(s, order).childNodes);
        ol.append(li);
      }
      box.append(ol);
    }
    if (a.next_actions?.length) {
      box.append(el("h3", null, "다음 할 일"));
      const ul = el("ul", "actions");
      for (const s of a.next_actions) {
        const li = el("li");
        li.append(...this.sentence(s, order).childNodes);
        ul.append(li);
      }
      box.append(ul);
    }
    this.renderDept(a.dept);
    this.renderCalendar(a.cited || []);
    if (a.as_of) box.append(el("p", "asof", `${formatKst(a.as_of)} 기준 정보`));
    if (a.notice) box.append(el("p", "safety", a.notice));
  }

  // 학사일정 근거를 월 달력으로 — "달력/캘린더"를 물으면 펼친 채로, 아니면 버튼으로 연다
  renderCalendar(cited) {
    const citedSet = new Set(cited);
    let events = [...this.calendarEvents].filter(([id]) => citedSet.has(id)).map(([, e]) => e);
    if (!events.length) events = [...this.calendarEvents.values()];
    if (!events.length) return;
    const wrap = el("section", "cal-wrap");
    const toggle = el("button", "cal-toggle", "");
    toggle.type = "button";
    const body = el("div", "cal-body");
    const setOpen = (open) => {
      body.hidden = !open;
      toggle.replaceChildren(icon("calendar", 15), document.createTextNode(open ? "달력 접기" : "달력으로 보기"));
      toggle.setAttribute("aria-expanded", String(open));
    };
    toggle.addEventListener("click", () => setOpen(body.hidden));
    for (const month of calendarMonths(events).slice(0, 3)) body.append(monthGrid(month, events));
    wrap.append(toggle, body);
    setOpen(/달력|캘린더|calendar/i.test(this.message));
    this.answer.append(wrap);
  }

  renderFallback(f) {
    this.finishSteps();
    this.markCited([]);
    const box = this.answer;
    box.replaceChildren(el("p", null, FALLBACK_TEXT[f.reason] || f.message));
    if (f.dept) box.append(el("p", "hint", "담당 부서에 문의해 주세요."));
    this.renderDept(f.dept);
  }

  renderAsk(q) {
    this.finishSteps();
    const box = this.answer;
    box.classList.add("ask");
    box.replaceChildren(el("p", null, q.question));
    const picked = {};
    const choices = q.choices || {};
    for (const [slot, opts] of Object.entries(choices)) {
      const g = el("div", "choice-group");
      g.append(el("span", "label", SLOT_LABEL[slot] || slot));
      for (const o of opts) {
        const b = el("button", "choice", o);
        b.type = "button";
        b.setAttribute("aria-pressed", "false");
        b.addEventListener("click", () => {
          g.querySelectorAll(".choice").forEach((x) => x.setAttribute("aria-pressed", "false"));
          b.setAttribute("aria-pressed", "true");
          picked[slot] = o;
          submit.disabled = state.busy || Object.keys(picked).length < Object.keys(choices).length;
        });
        g.append(b);
      }
      box.append(g);
    }
    const submit = el("button", "primary", "이 조건으로 답변 받기");
    submit.type = "button";
    submit.disabled = Object.keys(choices).length > 0;
    submit.addEventListener("click", () => send(answerSentence(picked)));
    if (Object.keys(choices).length) box.append(submit);
    else box.append(el("p", "hint", "아래 입력창에 답해 주세요."));
  }

  renderError(text, retry) {
    const box = this.answer;
    box.classList.add("error");
    box.replaceChildren(el("p", null, text));
    if (retry) {
      const b = el("button", "primary", "다시 시도");
      b.type = "button";
      b.addEventListener("click", retry);
      box.append(b);
    }
  }

  enableFeedback() {
    const fb = $(".feedback", this.node);
    fb.hidden = false;
    const stateEl = $(".fb-state", fb);
    let sent = 0;
    for (const [cls, rating] of [["up", 1], ["down", -1]]) {
      const b = $(`.fb.${cls}`, fb);
      b.addEventListener("click", async () => {
        if (sent === rating || !this.turnId) return;
        const r = await fetch("/api/feedback", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ thread_id: state.threadId, turn_id: this.turnId, rating }),
        }).catch(() => null);
        if (r && r.ok) {
          sent = rating;
          fb.querySelectorAll(".fb").forEach((x) => x.setAttribute("aria-pressed", "false"));
          b.setAttribute("aria-pressed", "true");
          stateEl.textContent = "저장됨";
        } else {
          stateEl.textContent = "저장 실패";
        }
      });
    }
  }
}

function answerSentence(picked) {
  const parts = [];
  if (picked.grade) parts.push(picked.grade === "5 이상" ? "5학년 이상" : `${picked.grade}학년`);
  if (picked.scholarship) {
    parts.push({ 예: "이번 학기 장학금 받아요", 아니오: "장학금 안 받아요", 모름: "장학금 여부는 몰라요" }[picked.scholarship]);
  }
  if (picked.dept) parts.push(picked.dept);
  return parts.join(", ");
}

function formatKst(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("ko-KR", { timeZone: "Asia/Seoul", month: "long", day: "numeric", hour: "numeric" });
}

// ── 전송·스트림 ─────────────────────────────────────────────────────────
async function send(message, requestId = uuid(), turn = null) {
  message = (message || "").trim();
  if (!message || state.busy) return;
  setBusy(true);
  turn = turn || new Turn(message);
  const retry = () => send(message, requestId, turn);
  let gotDone = false;
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({ schema_version: SCHEMA_VERSION, thread_id: state.threadId, request_id: requestId, message }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      if (res.status === 409) turn.renderError("이전 질문을 처리하고 있습니다. 잠시 후 다시 시도해 주세요.", retry);
      else if (err.code === "SCHEMA_MISMATCH") turn.renderError("화면이 오래되었습니다. 새로고침해 주세요.");
      else if (res.status === 429) turn.renderError("요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.", retry);
      else turn.renderError(err.message || "요청을 처리할 수 없습니다.", res.status >= 500 ? retry : null);
      return;
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    const parser = createSSEParser();
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      for (const { event, data } of parser.push(decoder.decode(value, { stream: true }))) {
        if (handle(turn, event, data, retry)) gotDone = true;
      }
    }
    if (!gotDone) turn.renderError("연결이 끊겼습니다.", retry);
  } catch {
    turn.renderError("연결이 끊겼습니다.", retry);
  } finally {
    setBusy(false);
  }
}

function handle(turn, event, d, retry) {
  if (event !== "draft" && event !== "status" && event !== "evidence") {
    turn.answer.classList.remove("drafting");
  }
  switch (event) {
    case "draft":
      turn.renderDraft(d.texts);
      break;
    case "meta":
      if (d.schema_version !== SCHEMA_VERSION) turn.renderError("화면이 오래되었습니다. 새로고침해 주세요.");
      turn.turnId = d.turn_id;
      if (d.replay) $(".timeline", turn.node).hidden = true;
      break;
    case "status":
      turn.status(d);
      break;
    case "evidence":
      turn.addCards(d.items);
      break;
    case "ask":
      turn.renderAsk(d);
      break;
    case "answer":
      turn.renderAnswer(d);
      break;
    case "fallback":
      turn.renderFallback(d);
      break;
    case "error":
      turn.renderError(d.message || "서버 오류가 발생했습니다.", retry);
      break;
    case "done":
      if (d.outcome !== "error") {
        turn.enableFeedback();
        turn.answer.focus({ preventScroll: true });
      }
      turn.node.scrollIntoView({ block: "end", behavior: "smooth" });
      return true;
  }
  return false;
}

// ── 초기화 ──────────────────────────────────────────────────────────────
async function init() {
  hydrateIcons();
  const input = $("#input");
  $("#form").addEventListener("submit", (e) => {
    e.preventDefault();
    const v = input.value;
    input.value = "";
    input.style.height = "";
    send(v);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      $("#form").requestSubmit();
    }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 140)}px`;
  });

  try {
    const [status, sug] = await Promise.all([
      fetch("/api/status").then((r) => r.json()),
      fetch("/api/suggestions").then((r) => r.json()),
    ]);
    $("#version-foot").textContent = ` · v${status.version}`;
    for (const text of sug.items || []) {
      const b = el("button", "chip", text);
      b.type = "button";
      b.setAttribute("role", "listitem");
      b.prepend(icon(chipIcon(text), 17));
      b.addEventListener("click", () => {
        track("chip_click", { target: text.slice(0, 200) });
        send(text); // 원클릭 시연 — 입력창을 거치지 않음
      });
      $("#chips").append(b);
    }
  } catch {}
  loadToday();
}

// ── 달력 보기 ──────────────────────────────────────────────────────────
function isoLocal(y, m, d) {
  return `${y}-${String(m + 1).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
}

function calendarMonths(events) {
  const keys = new Set();
  for (const e of events) keys.add(e.start.slice(0, 7));
  return [...keys].sort();
}

function monthGrid(ym, events) {
  const [y, m] = ym.split("-").map(Number);
  const byDay = new Map();
  for (const e of events) {
    for (const day of dayListLocal(e.start, e.end)) {
      if (!day.startsWith(ym)) continue;
      if (!byDay.has(day)) byDay.set(day, []);
      byDay.get(day).push(e.title);
    }
  }
  const box = el("div", "cal-month");
  box.append(el("div", "cal-title", `${y}년 ${m}월`));
  const grid = el("div", "cal-grid");
  grid.setAttribute("role", "grid");
  for (const [i, w] of ["일", "월", "화", "수", "목", "금", "토"].entries()) grid.append(el("div", `cal-dow${i === 0 ? " sun" : i === 6 ? " sat" : ""}`, w));
  const first = new Date(y, m - 1, 1).getDay();
  const days = new Date(y, m, 0).getDate();
  for (let i = 0; i < first; i += 1) grid.append(el("div", "cal-cell empty"));
  const today = isoLocal(new Date().getFullYear(), new Date().getMonth(), new Date().getDate());
  for (let d = 1; d <= days; d += 1) {
    const iso = isoLocal(y, m - 1, d);
    const dow = (first + d - 1) % 7;
    const titles = byDay.get(iso) || [];
    const cell = el("div", `cal-cell${titles.length ? " has" : ""}${dow === 0 ? " sun" : dow === 6 ? " sat" : ""}${iso === today ? " today" : ""}`);
    cell.append(el("span", "cal-num", String(d)));
    for (const t of titles.slice(0, 2)) cell.append(el("span", "cal-ev", t));
    if (titles.length) cell.title = titles.join("\n");
    grid.append(cell);
  }
  box.append(grid);
  const list = el("ul", "cal-list");
  for (const e of events.filter((x) => x.start.startsWith(ym) || x.end.startsWith(ym)).sort((a, b) => a.start.localeCompare(b.start))) {
    const when = e.start === e.end ? e.start.slice(5).replace("-", ".") : `${e.start.slice(5).replace("-", ".")} ~ ${e.end.slice(5).replace("-", ".")}`;
    const li = el("li");
    li.append(el("span", "cal-when", when), document.createTextNode(e.title));
    list.append(li);
  }
  box.append(list);
  return box;
}

function dayListLocal(start, end) {
  const [ys, ms, ds] = start.split("-").map(Number);
  const [ye, me, de] = end.split("-").map(Number);
  const out = [];
  const d = new Date(ys, ms - 1, ds);
  const last = new Date(ye, me - 1, de);
  for (let i = 0; d <= last && i < 62; i += 1) {
    out.push(isoLocal(d.getFullYear(), d.getMonth(), d.getDate()));
    d.setDate(d.getDate() + 1);
  }
  return out;
}

// 오늘·이번 주 학사 일정(진행 중 + 7일 안 마감). 없거나 실패하면 카드를 숨긴다
// 헤더 오른쪽 작은 '학사 일정' 버튼(첫 화면에서만). 올리거나 누르면 목록, 항목을 누르면 원문
async function loadToday() {
  try {
    const { items } = await fetch("/api/events/today").then((r) => r.json());
    if (!items?.length || !$("#welcome") || $("#welcome").hidden) return;
    const list = $("#today-list");
    for (const e of items) {
      const li = el("li");
      const row = el(safeSchoolUrl(e.url) ? "a" : "span", "today-row");
      if (row.tagName === "A") {
        row.href = e.url;
        row.target = "_blank";
        row.rel = "noopener noreferrer";
      }
      row.append(el("span", "t", e.title), el("span", "d", periodText(e)));
      li.append(row);
      list.append(li);
    }
    $("#today-count").textContent = String(items.length);
    const box = $("#today");
    const btn = $(".today-btn", box);
    btn.addEventListener("click", () => {
      const open = !box.classList.contains("open");
      box.classList.toggle("open", open);
      btn.setAttribute("aria-expanded", String(open));
    });
    document.addEventListener("click", (ev) => {
      if (!box.contains(ev.target)) {
        box.classList.remove("open");
        btn.setAttribute("aria-expanded", "false");
      }
    });
    box.hidden = false;
  } catch {}
}

function periodText(e) {
  const md = (iso) => `${iso.slice(5, 7)}.${iso.slice(8, 10)}`;
  return e.start_date && e.start_date !== e.end_date ? `${md(e.start_date)} ~ ${md(e.end_date)}` : `~ ${md(e.end_date)}`;
}

function safeSchoolUrl(url) {
  return /^https:\/\/([a-z0-9-]+\.)*kyungnam\.ac\.kr(\/|$)/i.test(String(url || ""));
}

init();
