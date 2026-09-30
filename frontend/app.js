// 학생 채팅 화면(상세설계 05 §1). 모든 외부 텍스트는 textContent로만 넣는다(innerHTML 금지).
import { calendarButtons } from "./ics.js";
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
    $("#new-thread").hidden = false; // 새 대화 버튼은 대화 화면에서만(교수님 2026-09-30 #628)
    document.querySelectorAll(".follow-ups .fu").forEach((b) => (b.disabled = true)); // 지난 턴 칩 비활성
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
        this.calendarEvents.set(c.id, { title: c.title, start: c.start_date, end: c.end_date || c.start_date, url: c.url });
      }
      if (c.kind === "place") this.placeText = `${this.placeText || ""} ${c.title} ${c.snippet}`;
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
    if (d.location_text) {
      // 부서 위치(예: 본관 1층)로 길찾기 — 교수님 2026-09-30
      this.routeWidget(`${d.name} ${d.location_text}`).then((w) => w && box.append(w));
    }
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
    this.renderRoute();
    this.renderFollowUps(a.follow_ups || []);
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

  // 정문 출발 도보 길안내: 장소 근거가 있거나 '어디·가는 길'을 물으면 지도에 경로를 그린다
  // 정문 출발 도보 길안내: 장소 근거가 있거나 '어디·가는 길'을 물으면 지도에 경로를 그린다
  async renderRoute() {
    const asksWay = /어디|위치|가는\s*길|가려면|찾아가|길\s*안내|몇\s*분/.test(this.message);
    const target = this.placeText || (asksWay ? this.message : "");
    const wrap = await this.routeWidget(target);
    if (!wrap) return;
    const fu = $(".follow-ups", this.answer);
    this.answer.insertBefore(wrap, fu || null);
  }

  // 목적지 문구(장소 이름·"본관 1층" 등) → [정문에서 ○○까지 걸어서 약 N분] 버튼 + 펼치면 약도·사진.
  // 같은 턴에서 같은 건물은 한 번만(답변과 부서 카드가 같은 건물을 가리키는 경우).
  async routeWidget(target) {
    if (!target) return null;
    try {
      const get = (start) => fetch(`/api/campus/route?to=${encodeURIComponent(target.slice(0, 200))}${start ? `&start=${encodeURIComponent(start)}` : ""}`).then((x) => x.json());
      let r = await get("");
      this.routesShown = this.routesShown || new Set();
      if (!r.found || this.routesShown.has(r.to)) return null;
      this.routesShown.add(r.to);
      const wrap = el("section", "route-wrap");
      const toggle = el("button", "route-toggle");
      toggle.type = "button";
      const label = () => toggle.replaceChildren(icon("pin", 15), document.createTextNode(`${r.from}에서 ${r.to}까지 걸어서 약 ${r.minutes}분 (${r.distance_m}m)`));
      label();
      const body = el("div", "route-body");
      body.hidden = true;
      // 출발지 고르기(교수님 2026-09-30): 바꾸면 경로·거리·시간을 다시 그린다
      const pick = el("label", "route-start", "출발 ");
      const select = el("select");
      const fig = el("div");
      const tips = el("div", "route-tips");
      const drawTips = () => tips.replaceChildren(...(r.tips || []).map((t) => el("p", "hint", t)));
      const draw = async () => fig.replaceChildren(await routeFigure(r));
      select.addEventListener("change", async () => {
        const next = await get(select.value).catch(() => null);
        if (!next?.found) return;
        r = next;
        label();
        drawTips();
        await draw();
        track("route_start", { turn_id: this.turnId, target: `${r.from}>${r.to}` });
      });
      pick.append(select);
      body.append(pick, tips, fig);
      drawTips();
      let drawn = false;
      toggle.addEventListener("click", async () => {
        body.hidden = !body.hidden;
        toggle.setAttribute("aria-expanded", String(!body.hidden));
        if (!drawn && !body.hidden) {
          drawn = true;
          campusMapCache = campusMapCache || (await fetch("/api/campus/map").then((x) => x.json()));
          for (const name of campusMapCache.starts || [r.from]) {
            if (name === r.to) continue;
            const o = el("option", null, name);
            o.value = name;
            o.selected = name === r.from;
            select.append(o);
          }
          await draw();
          track("route_open", { turn_id: this.turnId, target: r.to });
        }
      });
      wrap.append(toggle, body);
      return wrap;
    } catch {
      return null;
    }
  }

  renderFollowUps(list) {
    if (!list.length) return;
    const wrap = el("div", "follow-ups");
    wrap.append(el("span", "fu-label", "이어서 물어보기"));
    for (const q of list) {
      const b = el("button", "chip fu", q);
      b.type = "button";
      b.addEventListener("click", () => {
        if (state.busy) return;
        track("follow_up_click", { turn_id: this.turnId, target: q.slice(0, 200) });
        send(q);
      });
      wrap.append(b);
    }
    this.answer.append(wrap);
  }

  renderFallback(f) {
    this.finishSteps();
    this.markCited([]);
    const box = this.answer;
    box.replaceChildren(el("p", null, FALLBACK_TEXT[f.reason] || f.message));
    if (f.dept) box.append(el("p", "hint", "담당 부서에 문의해 주세요."));
    this.renderDept(f.dept);
    this.renderRoute();
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
// 첫 화면으로: 새 대화(새 thread)로 시작하고 이전 대화 화면을 지운다
function goHome() {
  if (state.busy) return;
  state.threadId = uuid();
  $("#turns").replaceChildren();
  $("#welcome").hidden = false;
  $("#new-thread").hidden = true;
  $("#today").hidden = !$("#today-list").children.length;
  $("#input").focus();
  window.scrollTo({ top: 0, behavior: "smooth" });
  track("new_thread");
}

async function init() {
  hydrateIcons();
  $("#new-thread").addEventListener("click", goHome);
  $("#home-link").addEventListener("click", (e) => {
    e.preventDefault();
    goHome();
  });
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

// ── 캠퍼스 도보 길안내 약도(SVG, 외부 지도 없음) ─────────────────────────
let campusMapCache = null;
const SVG_NS = "http://www.w3.org/2000/svg";
function svgEl(tag, attrs) {
  const n = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
  return n;
}

async function routeFigure(r) {
  campusMapCache = campusMapCache || (await fetch("/api/campus/map").then((x) => x.json()));
  const m = campusMapCache;
  const all = m.paths.flat();
  const xs = all.map((p) => p[0]);
  const ys = all.map((p) => p[1]);
  const pad = 40;
  const [x0, y0] = [Math.min(...xs) - pad, Math.min(...ys) - pad];
  const [w, h] = [Math.max(...xs) - x0 + pad, Math.max(...ys) - y0 + pad];
  const svg = svgEl("svg", { viewBox: `${x0} ${y0} ${w} ${h}`, class: "route-svg", role: "img", "aria-label": `${r.from}에서 ${r.to}까지 도보 경로` });
  const pts = (line) => line.map((p) => p.join(",")).join(" ");
  for (const line of m.paths) svg.append(svgEl("polyline", { points: pts(line), class: "rt-path" }));
  svg.append(svgEl("polyline", { points: pts(r.line), class: "rt-route" }));
  for (const [name, info] of Object.entries(m.places)) {
    const [x, y] = info.xy;
    const main = name === r.to || name === r.from;
    svg.append(svgEl("circle", { cx: x, cy: y, r: main ? 9 : 4, class: name === r.from ? "rt-start" : name === r.to ? "rt-end" : "rt-dot" }));
    const t = svgEl("text", { x: x + 8, y: y - 6, class: main ? "rt-label main" : "rt-label" });
    t.textContent = name;
    svg.append(t);
  }
  const fig = el("figure", "route-fig");
  fig.append(svg);
  const cap = el("figcaption", "hint", "교수님이 표시한 교내 도보길 기준 · 경사·계단 구간이 있어 실제 시간은 더 걸릴 수 있습니다.");
  fig.append(cap);
  const photo = m.places[r.to]?.photo;
  if (photo) {
    const img = el("img", "route-photo");
    img.src = photo;
    img.alt = `${r.to} 사진`;
    img.loading = "lazy";
    fig.append(img, el("figcaption", "hint", m.photo_credit || "사진 출처: 경남대학교 홈페이지"));
  }
  return fig;
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
    li.append(el("span", "cal-when", when), document.createTextNode(e.title), calendarButtons({ title: e.title, start: e.start, end: e.end, url: e.url }, el));
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

// 이번 1주일 학교 일정 + [전체보기](월 달력 페이지). 조회 실패면 버튼을 숨긴다
// 헤더 오른쪽 작은 '학사 일정' 버튼(첫 화면에서만). 올리거나 누르면 목록, 항목을 누르면 원문
async function loadToday() {
  try {
    const { items } = await fetch("/api/events/today").then((r) => r.json());
    if (!items || !$("#welcome") || $("#welcome").hidden) return;
    const list = $("#today-list");
    if (!items.length) list.append(el("li", "today-row", "이번 주 일정이 없습니다."));
    for (const e of items) {
      const li = el("li");
      const row = el(safeSchoolUrl(e.url) ? "a" : "span", "today-row");
      if (row.tagName === "A") {
        row.href = e.url;
        row.target = "_blank";
        row.rel = "noopener noreferrer";
      }
      const tag = { event: "행사", scholarship: "장학" }[e.category] || "학사";
      const head = el("span", "t");
      head.append(el("i", `tag${e.category === "event" ? " ev" : ""}`, tag), document.createTextNode(e.title));
      row.append(head, el("span", "d", `${e.label ? `${e.label} ` : ""}${periodText(e)}`));
      li.className = "today-li";
      li.append(row, calendarButtons({ title: e.title, start: e.start_date || e.end_date, end: e.end_date, url: e.url }, el));
      list.append(li);
    }
    const all = el("a", "today-all", "전체보기 →");
    all.href = "/calendar.html";
    const allLi = el("li");
    allLi.append(all);
    list.append(allLi);
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
