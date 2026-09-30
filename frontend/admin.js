// 관리자 화면(상세설계 05 §3). Google ID 토큰을 메모리에만 두고 Bearer로 보낸다.
// 모든 서버 텍스트는 textContent로만 넣는다(innerHTML 금지).

import { hydrateIcons, icon } from "./icons.js";
import { normalizePhoneInput, placeVerificationIssue, schoolSourceUrl } from "./admin-utils.js";

const $ = (s, r = document) => r.querySelector(s);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = String(text);
  return n;
};
const rid = () => (crypto.randomUUID ? crypto.randomUUID() : `r-${Date.now()}-${Math.random()}`);
const fmt = (v) => {
  if (v == null || v === "") return "—";
  if (Array.isArray(v)) return v.join(", ") || "—";
  if (typeof v === "object") return JSON.stringify(v);
  const s = String(v);
  return /^\d{4}-\d{2}-\d{2}T/.test(s) ? new Date(s).toLocaleString("ko-KR", { timeZone: "Asia/Seoul" }) : s;
};

let token = null;

async function api(path, { method = "GET", body, form } = {}) {
  const headers = { Authorization: `Bearer ${token}` };
  let payload;
  if (form) payload = form;
  else if (body) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  const r = await fetch(`/api/admin${path}`, { method, headers, body: payload });
  if (r.status === 401) {
    token = null;
    showLogin("로그인이 만료되었거나 허용되지 않은 계정입니다.");
    throw new Error("unauthorized");
  }
  const data = r.status === 204 ? {} : await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.message || `요청 실패(${r.status})`);
  return data;
}

// ── 로그인 ──────────────────────────────────────────────────────────────
let myRole = "admin";

// 관리자가 아닌 계정: 승인 요청(MatchProf 방식) 또는 대기 안내
function showAccess(me) {
  const box = $("#login-msg");
  $("#app").hidden = true;
  $("#login").hidden = false;
  box.replaceChildren();
  if (me.status === "pending") {
    box.append(el("strong", null, `${me.email} — 승인 대기 중입니다.`), el("br"), document.createTextNode("최고관리자가 승인하면 다시 로그인해 들어올 수 있습니다."));
    return;
  }
  const note = el("input");
  note.placeholder = "소속·용도(예: 학사관리팀 업무 담당)";
  note.maxLength = 200;
  const ask = el("button", "act primary", "관리자 승인 요청");
  ask.type = "button";
  ask.addEventListener("click", async () => {
    ask.disabled = true;
    try {
      await api("/access-request", { method: "POST", body: { note: note.value.trim() } });
      showAccess({ ...me, status: "pending" });
    } catch (e) {
      box.append(el("p", "msg err", e.message));
      ask.disabled = false;
    }
  });
  box.append(
    el("p", null, me.status === "rejected" ? `${me.email} — 이전 요청이 거절되었습니다. 다시 요청할 수 있습니다.` : `${me.email} — 아직 관리자가 아닙니다.`),
    note,
    ask,
  );
}

function showLogin(msg) {
  $("#app").hidden = true;
  $("#login").hidden = false;
  $("#login-msg").textContent = msg || "";
}

function decodeEmail(jwt) {
  try {
    const p = JSON.parse(atob(jwt.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
    return p.email || "";
  } catch {
    return "";
  }
}

async function initLogin() {
  const cfg = await fetch("/api/auth/config").then((r) => r.json()).catch(() => ({}));
  if (!cfg.google_client_id) return showLogin("관리자 로그인이 설정되지 않았습니다(GOOGLE_OAUTH_CLIENT_ID).");
  const wait = () =>
    window.google?.accounts?.id ? Promise.resolve() : new Promise((res) => setTimeout(() => res(wait()), 200));
  await wait();
  google.accounts.id.initialize({
    client_id: cfg.google_client_id,
    callback: async ({ credential }) => {
      token = credential;
      try {
        const me = await api("/me"); // 관리자 상태(active/pending/rejected/none)
        if (me.status !== "active") return showAccess(me);
        myRole = me.role || "admin";
        $("#who").textContent = `${decodeEmail(credential)}${myRole === "super_admin" ? " · 최고관리자" : ""}`;
        initVersion();
        $("#login").hidden = true;
        $("#app").hidden = false;
        openTab(location.hash.slice(1) || "sources");
      } catch (e) {
        if (e.message !== "unauthorized") showLogin(e.message);
      }
    },
  });
  google.accounts.id.renderButton($("#gsi"), { theme: "outline", size: "large", locale: "ko" });
}

// ── 공통 UI ─────────────────────────────────────────────────────────────
function table(rows, cols, actions) {
  if (!rows.length) return el("p", "hint", "항목이 없습니다.");
  const t = el("table", "grid");
  const head = el("tr");
  for (const [, label] of cols) head.append(el("th", null, label));
  if (actions) head.append(el("th", null, "작업"));
  t.append(head);
  for (const row of rows) {
    const tr = el("tr");
    for (const [key] of cols) {
      const td = el("td");
      const v = typeof key === "function" ? key(row) : row[key];
      if (v instanceof Node) td.append(v);
      else td.textContent = fmt(v);
      tr.append(td);
    }
    if (actions) {
      const td = el("td", "row-actions");
      for (const a of actions(row)) if (a) td.append(a);
      tr.append(td);
    }
    t.append(tr);
  }
  return t;
}

function btn(label, onClick, cls = "act") {
  const b = el("button", cls, label);
  b.type = "button";
  b.addEventListener("click", async () => {
    b.disabled = true;
    try {
      await onClick();
    } catch (e) {
      if (e.message !== "unauthorized") flash(e.message, true);
    } finally {
      b.disabled = false;
    }
  });
  return b;
}

function askReason(what) {
  const r = prompt(`${what}\n사유를 입력하세요(감사 로그에 남습니다).`);
  if (r == null) return null;
  if (!r.trim()) {
    flash("사유는 비워 둘 수 없습니다.", true);
    return null;
  }
  return r.trim();
}

function flash(text, err = false) {
  const b = $("#banner");
  b.hidden = false;
  b.textContent = text;
  b.style.color = err ? "var(--danger)" : "";
}

function badge(status) {
  const warn = ["pending", "staging", "publishing", "queued", "running", "purging"];
  const err = ["failed", "rejected", "purged"];
  return el("span", `status${warn.includes(status) ? " warn" : err.includes(status) ? " err" : ""}`, status || "—");
}

function field(label, name, type = "text", extra = {}) {
  const l = el("label", null, label);
  let input;
  if (type === "select") {
    input = el("select");
    for (const [v, t] of extra.options) {
      const o = el("option", null, t);
      o.value = v;
      input.append(o);
    }
  } else if (type === "textarea") {
    input = el("textarea");
    input.rows = 2;
  } else {
    input = el("input");
    input.type = type;
  }
  input.name = name;
  if (extra.required) input.required = true;
  if (extra.placeholder) input.placeholder = extra.placeholder;
  l.append(input);
  return l;
}

// ── 탭 ──────────────────────────────────────────────────────────────────
const TABS = {
  sources: ["데이터 출처", viewSources, "book"],
  runs: ["수집 실행", viewRuns, "retry"],
  disable: ["긴급 회수", viewDisable, "alert"],
  documents: ["교내 문서", viewDocuments, "book"],
  webpages: ["홈페이지 등록", viewWebPages, "external"],
  events: ["학사·행사 일정", viewEvents, "calendar"],
  reports: ["제보함", viewReports, "alert"],
  places: ["장소 표", viewPlaces, "pin"],
  phonebook: ["전화번호부", viewPhonebook, "phone"],
  review: ["검수 대기함", viewReview, "check"],
  unanswered: ["미응답·피드백", viewUnanswered, "help"],
  stats: ["통계", viewStats, "bolt"],
  admins: ["관리자", viewAdmins, "bot"],
  audit: ["감사 로그", viewAudit, "info"],
  faq: ["자주 묻는 질문", viewFaq, "help"],
  glossary: ["용어 사전", viewGlossary, "book"],
  shortcuts: ["바로가기", viewShortcuts, "external"],
};

function openTab(name) {
  if (!TABS[name]) name = "sources";
  if (location.hash.slice(1) !== name) {
    location.hash = name; // hashchange가 다시 불러 그린다(이중 로드 방지)
    return;
  }
  const nav = $("#tabs");
  nav.replaceChildren();
  for (const [key, [label, , iconName]] of Object.entries(TABS)) {
    const b = el("button");
    b.append(icon(iconName, 15), document.createTextNode(label));
    b.type = "button";
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", String(key === name));
    b.addEventListener("click", () => openTab(key));
    nav.append(b);
  }
  $("#banner").hidden = true;
  const view = $("#view");
  view.replaceChildren(el("p", "hint", "불러오는 중…"));
  return TABS[name][1](view).catch((e) => {
    if (e.message !== "unauthorized") view.replaceChildren(el("p", "msg err", e.message));
  });
}

// ── 바로가기: 운영에 자주 여는 화면(서비스·GCP 콘솔·저장소) ─────────────────────
const GCP = "https://console.cloud.google.com";
const PROJECT = "project=campusbridge-510101";
const SHORTCUTS = [
  ["서비스", [
    ["학생 화면", "/", "chat", "배포된 학생용 챗봇"],
    ["개인정보 처리방침", "/privacy", "info", "학생 화면에 연결된 안내문"],
    ["상태 점검(API)", "/api/status", "check", "버전·기본 상태 JSON"],
  ]],
  ["배포·운영 (GCP 콘솔)", [
    ["Cloud Run 리비전", `${GCP}/run/detail/asia-northeast3/campusbridge-web/revisions?${PROJECT}`, "bolt", "현재 리비전·트래픽·로그·측정항목"],
    ["Cloud Build 기록", `${GCP}/cloud-build/builds?${PROJECT}`, "retry", "배포 빌드 단계별 성공 여부"],
    ["수집 Job 실행", `${GCP}/run/jobs/details/asia-northeast3/campusbridge-ingest/executions?${PROJECT}`, "retry", "campusbridge-ingest 실행 이력"],
    ["로그 탐색기", `${GCP}/logs/query?${PROJECT}`, "alert", "오류·요청 로그 검색"],
  ]],
  ["데이터·인증 (GCP 콘솔)", [
    ["Firestore", `${GCP}/firestore/databases/campusbridge/data?${PROJECT}`, "book", "대화·장소·감사 로그 원본 데이터"],
    ["Vertex AI Search", `${GCP}/gen-app-builder/engines?${PROJECT}`, "spark", "학칙·학사안내 검색 엔진"],
    ["OAuth 대상·테스트 사용자", `${GCP}/auth/audience?${PROJECT}`, "bot", "관리자 추가 시 테스트 사용자도 등록"],
    ["결제·예산", `${GCP}/billing?${PROJECT}`, "coin", "비용 확인"],
  ]],
  ["기타", [
    ["GitHub 저장소", "https://github.com/KimJin777/campusBridge", "external", "소스 코드·커밋 기록"],
    ["경남대학교 홈페이지", "https://www.kyungnam.ac.kr", "building", "학교 공식 사이트"],
  ]],
];

async function viewShortcuts(view) {
  view.replaceChildren(el("div", "section-head", "바로가기"), el("p", "hint", "운영에 자주 여는 화면입니다. 새 탭으로 열립니다."));
  for (const [group, links] of SHORTCUTS) {
    view.append(el("h3", "shortcut-group", group));
    const grid = el("div", "shortcut-grid");
    for (const [title, href, iconName, desc] of links) {
      const a = el("a", "shortcut");
      a.href = href;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      const head = el("span", "shortcut-title");
      head.append(icon(iconName, 16), document.createTextNode(title));
      a.append(head, el("span", "shortcut-desc", desc));
      grid.append(a);
    }
    view.append(grid);
  }
}

// 출처 원본 링크: 긴 주소는 말줄임(마우스를 올리면 전체), [바로가기]는 새 창
function sourceLinks(row) {
  const links = row.links || [];
  if (!links.length) return el("span", "hint", row.id === "phonebook" ? "관리자 업로드 파일" : "—");
  const list = el("div", "src-links");
  for (const { label, url } of links) {
    const line = el("div", "src-link");
    const text = el("span", "src-url", url);
    text.title = `${label}: ${url}`;
    const go = el("a", "act small", "바로가기");
    go.href = url;
    go.target = "_blank";
    go.rel = "noopener noreferrer";
    line.append(text, go);
    list.append(line);
  }
  if (links.length <= 2) return list;
  const box = el("details", "src-more");
  box.append(el("summary", null, `${links.length}개 링크 보기`), list);
  return box;
}

// ── 학사 일정(#596): 공식 학사일정·공지 자동 추출분은 게시됨, AI 추출분은 검수 대기 ──────
const EVENT_STATUS = { pending: "검수 대기", active: "게시됨", disabled: "숨김", superseded: "정정되어 대체됨" };
const EVENT_SOURCE = { calendar: "공식 학사일정", regex: "공지(자동 추출)", llm: "공지(AI 추출)" };
const EVENT_CATEGORY = { calendar: "학사일정", academic: "학사공지", scholarship: "장학", event: "교내 행사" };

function todayIso() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

async function viewEvents(view) {
  const { items } = await api("/events");
  const setStatus = (row, status, label) => async () => {
    const body = { status, request_id: rid() };
    if (status === "active" && row.date_missing) {
      // 날짜 확인 필요 행사: 원문을 보고 기간을 넣어야 게시된다
      const raw = prompt(`「${row.title}」 기간을 입력하세요 (예: 2026-10-07 또는 2026-10-01~2026-10-10)`, "");
      if (!raw) return;
      const m = raw.replace(/\s/g, "").match(/^(\d{4}-\d{2}-\d{2})(?:~(\d{4}-\d{2}-\d{2}))?$/);
      if (!m) return flash("날짜 형식이 올바르지 않습니다. 예: 2026-10-01~2026-10-10", true);
      body.start_date = m[1];
      body.end_date = m[2] || m[1];
    }
    // 게시는 사유를 묻지 않는다(교수님 2026-09-30). 숨김만 사유를 받는다
    const reason = status === "active" ? "게시" : askReason(`「${row.title}」 ${label}`);
    if (!reason) return;
    body.reason = reason;
    await api(`/events/${encodeURIComponent(row.id)}`, { method: "PATCH", body });
    await openTab("events");
    flash(`「${row.title}」 ${label} 완료`);
  };
  view.replaceChildren(
    el("div", "section-head", "학사 일정"),
    el("p", "hint", "첫 화면 '학교 일정'에 나가는 데이터입니다(학사일정·학사/장학 공지·교내 행사). 기간이 명확한 일정은 자동 게시되고, AI가 추출하거나 포스터에서 읽은 일정은 원문을 확인한 뒤 게시하세요. '날짜 확인 필요' 행사는 [게시] 때 기간을 입력합니다. 종료된 일정은 기록으로 남고 학생 화면에는 나오지 않습니다."),
  );
  if (!items.length) {
    view.append(el("p", "hint", "아직 수집된 일정이 없습니다. '데이터 출처'에서 변경분 다시 수집을 실행하면 채워집니다."));
    return;
  }
  view.append(
    table(
      items,
      [[(r) => el("span", `status${r.status === "pending" ? " warn" : r.status === "disabled" ? " err" : ""}`, r.status === "pending" && r.date_missing ? "날짜 확인 필요" : EVENT_STATUS[r.status] || r.status), "상태"], ["title", "일정"],
       [(r) => el("span", r.end_date < todayIso() && !r.date_missing ? "hint" : null, r.date_missing ? "— (원문 확인)" : `${r.start_date || ""} ~ ${r.end_date}${r.end_date < todayIso() ? " (종료)" : ""}`), "기간"],
       [(r) => el("span", null, EVENT_CATEGORY[r.source_category] || r.source_category || "—"), "분류"],
       [(r) => el("span", null, EVENT_SOURCE[r.extracted_by] || r.extracted_by || "—"), "출처"],
       [(r) => sourceLink(r.source_url), "원문"], ["reviewed_by", "검수자"]],
      (row) => [
        row.status !== "active" ? btn(row.status === "pending" ? "게시" : "다시 게시", setStatus(row, "active", "게시"), "act primary") : null,
        row.status !== "disabled" ? btn(row.status === "pending" ? "반려" : "숨김", setStatus(row, "disabled", "숨김")) : null,
      ],
    ),
  );
}

async function viewSources(view) {
  const { items } = await api("/sources");
  const rules = items.find((s) => s.id === "rules");
  view.replaceChildren();
  if (rules?.rejected_rule_nos?.length) {
    flash(`검수 실패로 색인되지 않은 규정 ${rules.rejected_rule_nos.length}건: ${rules.rejected_rule_nos.join(", ")} — 이전 버전이 유지됩니다(검수 대기함 참고).`, true);
  }
  const bar = el("div");
  bar.append(
    btn("변경분 다시 수집(전체)", async () => {
      if (!confirm("규정·학사안내 변경분 수집을 시작할까요?")) return;
      const r = await api("/ingestion-runs", { method: "POST", body: { source_ids: [], idempotency_key: rid() } });
      flash(r.reused ? `이미 실행 중인 작업이 있습니다: ${r.id}` : `수집을 시작했습니다: ${r.id}`);
    }, "act primary"),
  );
  view.append(bar);
  view.append(
    table(
      items,
      [["id", "출처"], [sourceLinks, "원본 링크"], ["kind", "종류"], ["schedule", "주기"], [(r) => (r.paused ? "일시정지" : "동작"), "상태"],
       ["last_success_at", "마지막 성공"], ["doc_count", "문서 수"], ["active_index_version", "색인 버전"]],
      (row) => [
        btn(row.paused ? "재개" : "일시정지", async () => {
          const reason = askReason(`${row.id} ${row.paused ? "재개" : "일시정지"}`);
          if (!reason) return;
          await api(`/sources/${encodeURIComponent(row.id)}`, { method: "PATCH", body: { paused: !row.paused, reason, request_id: rid() } });
          openTab("sources");
        }),
        ...["manual", "daily", "weekly"].filter((p) => p !== row.schedule).map((p) =>
          btn(`주기: ${p}`, async () => {
            const reason = askReason(`${row.id} 주기를 ${p}로`);
            if (!reason) return;
            await api(`/sources/${encodeURIComponent(row.id)}`, { method: "PATCH", body: { schedule: p, reason, request_id: rid() } });
            openTab("sources");
          }),
        ),
        btn("이 출처만 다시 수집", async () => {
          if (!confirm(`${row.id} 변경분 수집을 시작할까요?`)) return;
          const r = await api("/ingestion-runs", { method: "POST", body: { source_ids: [row.id], idempotency_key: rid() } });
          flash(`수집 작업: ${r.id}${r.reused ? " (기존 작업 재사용)" : ""}`);
        }),
      ],
    ),
  );
}

// 수집 실행이 무엇이었고 무엇을 모았는지 사람이 읽게(교수님 #775·#778)
const RUN_SOURCE = { rules: "학칙·규정", guides: "학사안내", web_pages: "등록 홈페이지", events: "학사일정·공지 일정", menus: "식단", places: "장소·부서" };
const RUN_PART = { web_pages: "홈페이지", events: "일정", event_docs: "공지 본문", tips: "꿀팁", menus: "식단" };
const RUN_WORD = { pages: "쪽", indexed: "색인", sections: "절", failed: "실패", calendar: "학사일정", notice_auto: "공지(자동)", notice_llm: "공지(AI)", notice_poster: "그림 공지", event_auto: "행사", event_pending: "행사 검수", read: "읽음", poster: "그림", removed: "삭제", superseded: "정정", skipped: "건너뜀" };
function runTitle(r) {
  const what = (r.source_ids || []).length ? r.source_ids.map((s) => RUN_SOURCE[s] || s).join(", ") : "전체";
  if (r.trigger === "schedule" || String(r.id).startsWith("schedule-")) return `매일 자동 수집(${what})`;
  return `관리자 수집: ${what}${r.actor ? ` · ${r.actor}` : ""}`;
}
function runSummary(r) {
  const parts = [];
  for (const [k, label] of Object.entries(RUN_PART)) {
    if (r[`${k}_error`]) parts.push(`${label} 실패(${r[`${k}_error`]})`);
    const v = r[k];
    if (v == null) continue;
    if (typeof v === "number") parts.push(`${label} ${v}`);
    else if (typeof v === "object") {
      const nums = Object.entries(v).filter(([, n]) => typeof n === "number" && n > 0).map(([w, n]) => `${RUN_WORD[w] || w} ${n}`);
      if (nums.length) parts.push(`${label}: ${nums.join(", ")}`);
    }
  }
  return parts.join(" · ") || "—";
}

async function viewRuns(view) {
  const { items } = await api("/ingestion-runs");
  view.replaceChildren(
    btn("새로고침", () => openTab("runs")),
    el("p", "hint", "무엇을 누가 실행했는지와 단계별로 모은 건수를 보여 줍니다. 실행 번호(run-…)는 감사 로그와 대조할 때만 씁니다."),
    table(items, [[runTitle, "수집"], [runSummary, "수집 결과"], ["id", "실행 번호"], [(r) => badge(r.status), "상태"], ["phase", "단계"],
      [(r) => `${r.processed ?? 0}/${r.total ?? "?"}`, "진행"], ["added", "추가"], ["changed", "변경"],
      ["rejected", "거부"], ["error_code", "실패 원인"], ["started_at", "시작"], ["finished_at", "종료"]]),
  );
}

async function viewDisable(view) {
  const form = el("form", "inline");
  form.append(
    field("문서·조문 ID(줄바꿈으로 여러 개)", "ids", "textarea", { required: true, placeholder: "예: 29_main_38\nguide:leave:2" }),
    field("사유", "reason", "text", { required: true }),
  );
  const submit = el("button", "act danger", "검색에서 즉시 제외");
  submit.type = "submit";
  form.append(submit);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const ids = form.ids.value.split(/\s+/).map((s) => s.trim()).filter(Boolean);
    if (!ids.length || !confirm(`${ids.length}건을 학생 검색에서 즉시 제외할까요?`)) return;
    try {
      const r = await api("/disable", { method: "POST", body: { document_ids: ids, reason: form.reason.value, request_id: rid() } });
      flash(r.message || "반영했습니다.");
      form.reset();
    } catch (err) {
      if (err.message !== "unauthorized") flash(err.message, true);
    }
  });
  view.replaceChildren(el("p", "hint", "잘못 공개된 문서·조문을 학생 검색에서 즉시 뺍니다. 사유는 감사 로그에 남습니다."), form);
}

async function docAction(row, action) {
  const label = { publish: "게시", reject: "거부", archive: "게시 중단", purge: "완전 삭제" }[action];
  const reason = askReason(`문서 「${row.title}」 ${label}`);
  if (!reason) return;
  const body = { reason, request_id: rid() };
  if (action === "purge") {
    const c = prompt(`완전 삭제는 되돌릴 수 없습니다(원본·추출본·색인 삭제).\n확인을 위해 문서 ID(${row.id})를 입력하세요.`);
    if (c !== row.id) return flash("문서 ID가 일치하지 않아 취소했습니다.", true);
    body.confirm_document_id = c;
  } else if (!confirm(`「${row.title}」을(를) ${label}할까요?`)) return;
  await api(`/documents/${encodeURIComponent(row.id)}/${action}`, { method: "POST", body });
  flash(`${label} 요청을 반영했습니다.`);
  openTab("documents");
}

async function viewDocuments(view) {
  const form = el("form", "inline");
  form.append(
    field("파일(PDF·HWP·HWPX·DOCX·TXT·MD, 20MB 이하)", "file", "file", { required: true }),
    field("제목", "title", "text", { required: true }),
    field("소관부서", "department", "text", { required: true }),
    field("문서일", "doc_date", "date", { required: true }),
    field("시행일", "effective_from", "date", { required: true }),
    field("만료일", "expires_at_doc", "date", { required: true }),
    field("출처(공문 번호 등)", "source", "text", { required: true }),
    field("공개 답변 인용 승인 근거", "approval_basis", "text", { required: true }),
    field("사유", "reason", "text", { required: true }),
  );
  const up = el("button", "act primary", "업로드(검토 대기)");
  up.type = "submit";
  form.append(up);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    fd.append("request_id", rid());
    try {
      const r = await api("/documents", { method: "POST", form: fd });
      flash(`업로드했습니다(${r.id}). 미리보기 변환이 끝나면 게시할 수 있습니다.`);
      openTab("documents");
    } catch (err) {
      if (err.message !== "unauthorized") flash(err.message, true);
    }
  });
  const { items } = await api("/documents");
  view.replaceChildren(
    el("h3", null, "교내 문서 업로드"),
    form,
    el("h3", null, "문서 목록"),
    table(items, [["title", "제목"], ["department", "소관부서"], [(r) => badge(r.status), "상태"],
      [(r) => badge(r.preview_status), "미리보기"], ["approval_basis", "승인 근거"], ["effective_from", "시행일"], ["updated_at", "수정"]],
      (row) => [
        btn("미리보기", async () => {
          const p = await api(`/documents/${encodeURIComponent(row.id)}/preview`);
          const pre = el("pre", "preview", (p.preview_chunks || []).join("\n\n— — —\n\n") || "(미리보기 없음)");
          view.prepend(el("p", "msg", `「${p.title}」 분할 ${p.preview_chunk_count ?? 0}개, 본문 ${p.preview_text_length ?? 0}자`), pre);
        }),
        row.status === "staging" && row.preview_status === "ready" ? btn("게시", () => docAction(row, "publish"), "act primary") : null,
        row.status === "staging" ? btn("거부", () => docAction(row, "reject")) : null,
        row.status === "published" ? btn("게시 중단", () => docAction(row, "archive")) : null,
        row.status !== "purged" ? btn("완전 삭제", () => docAction(row, "purge"), "act danger") : null,
      ]),
  );
}

async function viewPlaces(view) {
  const form = el("form", "inline");
  form.append(
    field("place_id(영문 소문자)", "place_id", "text", { required: true, placeholder: "acad_office" }),
    field("이름", "name", "text", { required: true }),
    field("종류", "kind", "select", { options: [["unit", "부서·연구소"], ["building", "건물"], ["facility", "시설"]] }),
    field("별칭(쉼표)", "aliases"),
    field("위치 문구", "raw_location", "text", { placeholder: "본관 1층 101호" }),
    field("원문 URL(학교 도메인)", "source_url", "url"),
    field("확인일", "snapshot_at", "date"),
    field("사유", "reason", "text", { required: true }),
  );
  const add = el("button", "act primary", "등록(검수 대기)");
  add.type = "submit";
  form.append(add);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(form));
    const body = { place_id: f.place_id, name: f.name, kind: f.kind, reason: f.reason, request_id: rid() };
    if (f.aliases) body.aliases = f.aliases.split(",");
    for (const k of ["raw_location", "source_url", "snapshot_at"]) if (f[k]) body[k] = f[k];
    try {
      await api("/places", { method: "POST", body });
      flash("등록했습니다. 원문 확인 후 [검수 완료]를 눌러 주세요.");
      openTab("places");
    } catch (err) {
      if (err.message !== "unauthorized") flash(err.message, true);
    }
  });
  const { items } = await api("/places");
  const editor = el("div", "editor-host");
  const openEditor = (row) => {
    editor.replaceChildren(placeEditor(row, () => editor.replaceChildren()));
    editor.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  const addBlock = el("details", "block block-add");
  const addHead = el("summary", "block-head");
  addHead.append(icon("building", 16), document.createTextNode("새 장소 등록"), el("span", "block-sub", "목록에 없는 부서·건물을 직접 추가할 때만 펼치세요"));
  addBlock.append(addHead, form);
  const listBlock = el("section", "block block-list");
  const listHead = el("div", "block-head");
  listHead.append(icon("pin", 16), document.createTextNode(`등록된 장소 목록 (${items.length}건)`), el("span", "block-sub", "편집·검수는 여기서 합니다"));
  listBlock.append(listHead, editor, placeTable(items, { onEdit: openEditor, refreshTab: "places" }));
  view.replaceChildren(
    el("div", "section-head", "장소·부서 연락처 운영"),
    el("p", "hint", "지도·좌표는 쓰지 않습니다. 검수 완료(verified)된 행만 학생 답변에 나갑니다. 검수된 위치·전화를 고치면 다시 검수 대기로 돌아갑니다."),
    addBlock,
    listBlock,
  );
}

function sourceLink(url) {
  if (!schoolSourceUrl(url)) return el("span", null, "—");
  const a = el("a", null, "원문");
  a.href = url;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  return a;
}

function placeEditor(row, close) {
  const form = el("form", "place-editor");
  const title = el("div", "editor-title");
  title.append(icon("pin", 18), el("strong", null, `「${row.name}」 위치·대표전화 편집`));
  const location = field("위치(건물/층/호실)", "raw_location", "text", { placeholder: "본관 1층 101호" });
  const phone = field("대표전화", "phone", "tel", { placeholder: "055-249-1234" });
  const reason = field("수정 사유", "reason", "text", { required: true, placeholder: "원문 확인 후 위치·전화 수정" });
  location.querySelector("input").value = row.raw_location || "";
  phone.querySelector("input").value = row.phone || "";
  const save = el("button", "act primary", "변경 저장");
  save.type = "submit";
  const cancel = el("button", "act", "취소");
  cancel.type = "button";
  cancel.addEventListener("click", close);
  const actions = el("div", "editor-actions");
  actions.append(save, cancel);
  form.append(
    title,
    el("p", "hint", row.status === "verified" ? "저장하면 검수 상태가 pending으로 바뀝니다." : "수정 내용과 사유는 감사 로그에 남습니다."),
    location,
    phone,
    reason,
    actions,
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    save.disabled = true;
    try {
      const values = Object.fromEntries(new FormData(form));
      const body = { reason: String(values.reason || "").trim(), request_id: rid() };
      const loc = String(values.raw_location || "").trim();
      const tel = normalizePhoneInput(values.phone);
      if (loc !== (row.raw_location || "")) body.raw_location = loc;
      if (tel !== (row.phone || "")) body.phone = tel;
      if (!("raw_location" in body) && !("phone" in body)) {
        flash("바뀐 내용이 없습니다.", true);
        return;
      }
      await api(`/places/${encodeURIComponent(row.place_id || row.id)}`, { method: "PATCH", body });
      await openTab("places");
      flash(`「${row.name}」 위치·대표전화를 저장했습니다.`);
    } catch (error) {
      if (error.message !== "unauthorized") flash(error.message, true);
    } finally {
      save.disabled = false;
    }
  });
  return form;
}

function skipSummary(skipped) {
  const counts = new Map();
  for (const reason of skipped) counts.set(reason, (counts.get(reason) || 0) + 1);
  return [...counts].map(([reason, count]) => `${reason} ${count}건`).join(", ");
}

function placeTable(rows, { onEdit = null, refreshTab = "review", includeEvidence = false } = {}) {
  const wrap = el("div", "place-table");
  if (!rows.length) {
    wrap.append(el("p", "hint", "검수 대기 항목이 없습니다."));
    return wrap;
  }
  const checked = new Set();
  const all = el("button", "act", "대기 항목 전체 선택");
  all.type = "button";
  const go = btn("선택 항목 검수 완료", async () => {
    if (!checked.size) return flash("선택한 항목이 없습니다.", true);
    const reason = askReason(`${checked.size}건 검수 완료(원문 확인함)`);
    if (!reason) return;
    let ok = 0;
    const skipped = [];
    for (const id of checked) {
      const row = rows.find((item) => (item.place_id || item.id) === id);
      const issue = placeVerificationIssue(row || {});
      if (issue) {
        skipped.push(issue);
        continue;
      }
      try {
        await api(`/places/${encodeURIComponent(id)}`, { method: "PATCH", body: { status: "verified", reason, request_id: rid() } });
        ok += 1;
      } catch (e) {
        skipped.push(`서버 거부(${e.message})`);
      }
    }
    const result = `성공 ${ok} · 건너뜀 ${skipped.length}${skipped.length ? ` (${skipSummary(skipped)})` : ""}`;
    await openTab(refreshTab);
    flash(result, skipped.length > 0);
  }, "act primary");
  const boxes = [];
  const columns = [
    [(r) => {
      if (r.status !== "pending") return el("span", "hint", "—");
      const cb = el("input");
      cb.type = "checkbox";
      cb.setAttribute("aria-label", `${r.name} 선택`);
      cb.addEventListener("change", () => (cb.checked ? checked.add(r.place_id || r.id) : checked.delete(r.place_id || r.id)));
      boxes.push(cb);
      return cb;
    }, "선택"],
    ["name", "이름"], ["kind", "종류"], ["raw_location", "위치"], ["phone", "대표 전화"],
    [(r) => badge(r.status), "상태"],
  ];
  if (includeEvidence) columns.push(["evidence_text", "원문 문장"]);
  columns.push([(r) => sourceLink(r.source_url), "원문"], ["snapshot_at", "확인일"], ["verified_by", "검수자"]);
  const t = table(rows, columns, onEdit ? (row) => [btn("편집", () => onEdit(row))] : null);
  all.addEventListener("click", () => {
    const on = checked.size < boxes.length;
    boxes.forEach((cb) => {
      cb.checked = on;
      cb.dispatchEvent(new Event("change"));
    });
  });
  const tools = el("div", "bulk-tools");
  tools.append(all, go);
  wrap.append(el("p", "hint", "학교 원문 URL·확인일·위치 또는 부서 대표전화가 있고, 원문과 일치하는 항목만 검수 완료하세요."), tools, t);
  return wrap;
}

// ── 교내 홈페이지 등록(교수님 #682·#685·#688): 미리보기 → 등록하면 바로 수집 대상 ──
const WEB_STATUS = { active: "수집 중", stopped: "중지", deleted: "삭제(색인 정리 대기)" };

async function viewWebPages(view) {
  const { items } = await api("/web-pages");
  const form = el("form", "inline");
  form.append(
    field("교내·소속 기관 홈페이지 주소(kyungnam.ac.kr, kusemicamp.com)", "url", "url", { required: true, placeholder: "https://www.kyungnam.ac.kr/ko/4319/subview.do" }),
    field("메모(무엇을 위한 페이지인지)", "note", "text", { placeholder: "예: 통학버스 노선 안내" }),
  );
  const preview = el("div", "web-preview");
  let checked = null; // 미리보기를 확인한 주소 — 이 주소일 때만 [등록]
  const reg = btn("등록", async () => {
    if (checked !== form.url.value.trim()) return flash("먼저 [미리보기]로 내용을 확인하세요.", true);
    await api("/web-pages", { method: "POST", body: { url: checked, note: form.note.value, request_id: rid() } });
    flash("등록했습니다. 다음 수집(매일 05:00) 또는 [지금 수집]으로 학생 답변에 반영됩니다.");
    openTab("webpages");
  }, "act primary");
  reg.disabled = true;
  form.url.addEventListener("input", () => {
    reg.disabled = checked !== form.url.value.trim();
  });
  const pv = btn("미리보기", async () => {
    const url = form.url.value.trim();
    if (!url) return flash("주소를 입력하세요.", true);
    preview.replaceChildren(el("p", "hint", "페이지를 읽는 중…"));
    try {
      const r = await api("/web-pages/preview", { method: "POST", body: { url } });
      checked = url;
      reg.disabled = false;
      const list = el("div");
      for (const s of r.sections) list.append(el("h4", null, s.heading), el("pre", "preview", s.text));
      preview.replaceChildren(el("p", "hint", `「${r.title}」 — 본문 ${r.count}개 절을 찾았습니다. 학생 답변에 쓰일 내용이 맞으면 [등록]을 누르세요.`), list);
    } catch (e) {
      checked = null;
      reg.disabled = true;
      // 실패 이유를 미리보기 자리에 바로 보인다(교수님 #775: 눌러도 아무 반응이 없어 보임)
      const fail = el("p", "msg err", `미리보기 실패: ${e.message} `);
      // 원인은 서버 로그에 남는다(교수님 2026-10-01): 이 오류만 거른 로그 탐색기로 바로 이동
      const q = 'resource.type="cloud_run_revision"
resource.labels.service_name="campusbridge-web"
textPayload:"web page preview failed"';
      const logs = el("a", null, "오류 로그 확인 →");
      logs.href = `${GCP}/logs/query;query=${encodeURIComponent(q)};duration=P1D?${PROJECT}`;
      logs.target = "_blank";
      logs.rel = "noopener noreferrer";
      fail.append(logs);
      preview.replaceChildren(fail);
      if (e.message === "unauthorized") throw e;
    }
  });
  form.addEventListener("submit", (e) => e.preventDefault());
  form.append(pv, reg);
  const act = (row, action, label) => btn(label, async () => {
    const reason = askReason(`「${row.title || row.url}」 ${label}`);
    if (!reason) return;
    await api(`/web-pages/${encodeURIComponent(row.id)}/${action}`, { method: "POST", body: { reason, request_id: rid() } });
    openTab("webpages");
  });
  view.replaceChildren(
    el("p", "hint", "학생 답변에 쓸 교내 홈페이지(학과·부서·생활관 등)를 주소로 등록합니다. 입력한 한 쪽만 읽고 링크는 따라가지 않습니다. 등록·중지·삭제는 감사 로그에 남습니다."),
    form,
    preview,
    btn("지금 수집", async () => {
      if (!confirm("등록된 홈페이지를 지금 다시 읽어 반영할까요?")) return;
      const r = await api("/ingestion-runs", { method: "POST", body: { source_ids: ["web_pages"], idempotency_key: rid() } });
      flash(`수집 작업: ${r.id}${r.reused ? " (기존 작업 재사용)" : ""} — '수집 실행' 탭에서 진행을 볼 수 있습니다.`);
    }),
    table(items, [["title", "제목"], [(r) => sourceLink(r.url), "주소"], ["note", "메모"], [(r) => WEB_STATUS[r.status] || r.status, "상태"],
      ["sections", "절 수"], [(r) => badge(r.last_status), "마지막 수집"], ["last_error", "실패 원인"], ["last_run_at", "수집 시각"],
      ["created_by", "등록자"], ["created_at", "등록"]],
    (row) => [
      row.status === "active" ? act(row, "stop", "중지") : null,
      row.status === "stopped" ? act(row, "resume", "재개") : null,
      row.status !== "deleted" ? act(row, "delete", "삭제") : null,
    ]),
  );
}

// ── 제보함(교수님 #697~#715): 꿀팁 검수 · 잘못된 정보 제보 처리 ─────────────
const TIP_STATUS = { pending: "관리자 확인 필요", verifying: "검증 중(공개 투표)", student_approved: "학생 확인 승인", approved: "관리자 승인", hidden: "임시 가림", rejected: "반려", withdrawn: "회수" };
const WRONG_STATUS = { pending: "확인 필요", confirmed: "확인됨(수정 필요)", no_issue: "이상 없음", needs_source_review: "원문 확인 필요", resolved: "수정 완료", rejected: "반려" };
const WRONG_KIND = { answer_evidence_mismatch: "답변-근거 불일치", stale_source: "원문이 낡음", missing_evidence: "근거 없음", display_error: "화면 오류", other: "기타" };
let reportType = "tip";
// 잘못된 정보 제보 처리 버튼의 뜻(교수님 #776) — 버튼 툴팁과 안내 표에 같이 쓴다
const WRONG_ACTION = {
  confirm: ["확인됨", "제보가 맞다(답변이 틀렸거나 질문과 어긋남). AI 개선용 평가 목록에 자동으로 올라갑니다. 자료를 보강해야 하면 보강한 뒤 [수정 완료]로 닫으세요."],
  needs_source_review: ["원문 확인 필요", "학교 원문 자체가 틀렸거나 낡은 것 같다. 담당 부서에 원문 수정을 요청한 상태로 둡니다."],
  resolve: ["수정 완료", "원인을 고쳤다(홈페이지 등록·장소 표·교내 문서 보강, 원문 수정 후 재수집 등). 제보를 닫습니다."],
  no_issue: ["이상 없음", "답변이 원문과 맞고 질문에도 맞다. 제보가 잘못되었습니다."],
  reject: ["반려", "장난·광고·질문과 무관한 내용. 90일 뒤 자동 삭제됩니다."],
};
const tip = (b, action) => {
  b.title = WRONG_ACTION[action]?.[1] || "";
  return b;
};

async function reportAction(row, action, label, { ask = true, text = null } = {}) {
  const reason = ask ? askReason(`제보 ${label}`) : label;
  if (!reason) return;
  await api(`/reports/${encodeURIComponent(row.id)}/${action}`, { method: "POST", body: text ? { reason, text } : { reason } });
  flash(`${label} 처리했습니다.`);
  openTab("reports");
}

// 제보함 에이전트 조치(교수님 #786·CLI '1번 전부', GPT5 #792): 조치안 → [승인·반영] 1회 → 2/2 재확인
const FIX_TYPE = { place: "장소 초안", recollect: "원문 재비교", unanswered: "미응답 연결" };
const FIX_STATUS = { proposed: "조치안 있음", no_draft: "초안 못 만듦", no_change: "원문 그대로", recollecting: "재수집 중", linked: "미응답 목록에 연결됨", applied: "반영함", verified: "재확인 2/2 통과", recheck_failed: "재확인 실패" };

async function agentStep(row, step, body) {
  const out = await api(`/reports/${encodeURIComponent(row.id)}/agent/${step}`, { method: "POST", body: body || {} });
  await openTab("reports");
  flash(out.status === "verified" ? "재확인 2/2 통과 — 수정 완료로 닫았습니다." : `에이전트: ${FIX_STATUS[out.status] || out.status}`, out.status === "recheck_failed" || out.status === "no_draft");
  return out;
}

function agentCell(row) {
  const f = row.agent_fix;
  const box = el("div", "agent-fix");
  if (!f) {
    box.append(btn("조치안 만들기", () => agentStep(row, "analyze")));
    return box;
  }
  box.append(el("strong", null, `${FIX_TYPE[f.type] || f.type} · ${FIX_STATUS[f.status] || f.status}`));
  if (f.type === "place" && f.draft) {
    const d = f.draft;
    if (d.ok) {
      box.append(el("p", null, `${d.name} — ${d.location}${d.phone ? ` · ${d.phone}` : ""}`), el("q", "hint", d.quote));
      const a = sourceLink(d.source_url);
      a.textContent = d.source_title || "근거 원문";
      box.append(a);
    } else {
      box.append(el("p", "hint", d.reason));
    }
  }
  if (f.type === "recollect") {
    for (const p of f.pages || []) box.append(el("p", "hint", `${p.status === "changed" ? "바뀜" : p.status === "same" ? "그대로" : "읽지 못함"} · ${p.title || p.id}`));
    if (f.status === "no_change") box.append(el("p", "hint", "인용한 원문이 그대로입니다. 학생 주장이 맞다면 학교 원문이 낡은 것이니 [원문 확인 필요]로 담당 부서에 알리세요."));
  }
  if (f.type === "unanswered" && f.status === "linked") box.append(el("p", "hint", "자료를 보강한 뒤(홈페이지 등록·용어 사전 등) [다시 확인]을 누르세요."));
  if (f.check) {
    for (const r of f.check.runs || []) box.append(el("p", r.ok ? "hint" : "msg err", `${r.ok ? "통과" : `실패(${r.reason})`} · ${r.query}`));
  }
  const acts = el("div");
  if (f.type === "place" && ["proposed", "recheck_failed"].includes(f.status) && f.draft?.ok) {
    acts.append(btn(f.status === "proposed" ? "승인·반영" : "다시 확인", () => {
      if (f.status === "proposed" && !confirm(`장소표에 반영합니다.\n${f.draft.name} — ${f.draft.location}\n근거: ${f.draft.quote}`)) return;
      return f.status === "proposed" ? agentStep(row, "apply") : agentStep(row, "verify");
    }, "act primary"));
  }
  if (f.type === "recollect" && f.status === "proposed") {
    acts.append(btn("재수집하고 다시 확인", async () => {
      const r = await api("/ingestion-runs", { method: "POST", body: { source_ids: f.source_ids, idempotency_key: rid() } });
      await agentStep(row, "apply", { run_id: r.id });
    }, "act primary"));
  }
  if ((f.type === "recollect" && ["recollecting", "recheck_failed"].includes(f.status)) || (f.type === "unanswered" && ["linked", "recheck_failed"].includes(f.status))) {
    acts.append(btn("다시 확인", () => agentStep(row, "verify"), "act primary"));
  }
  if (!["verified", "recollecting", "applied"].includes(f.status)) acts.append(btn("조치안 다시 만들기", () => agentStep(row, "analyze")));
  box.append(acts);
  return box;
}

// 새 제보는 화면을 열 때 에이전트가 스스로 조치안을 만든다(한 번에 3건)
async function autoAnalyze(items) {
  const todo = items.filter((r) => r.type === "wrong_info" && r.status === "pending" && !r.agent_fix).slice(0, 3);
  if (!todo.length) return false;
  flash(`에이전트가 새 제보 ${todo.length}건의 조치안을 만드는 중…`);
  for (const r of todo) await api(`/reports/${encodeURIComponent(r.id)}/agent/analyze`, { method: "POST", body: {} }).catch(() => null);
  return true;
}

function wrongGuide() {
  // 처리 버튼의 뜻 + '에이전트 의견이 맞을 때' 처리 순서(교수님 #776)
  const box = el("div", "msg");
  box.append(el("strong", null, "처리 버튼의 뜻"));
  const dl = el("dl", "cal-detail-body");
  for (const [label, text] of Object.values(WRONG_ACTION)) dl.append(el("dt", null, label), el("dd", null, text));
  box.append(dl, el("strong", null, "에이전트 의견이 맞을 때(예: 질문과 어긋난 답변)"));
  const ol = el("ol");
  for (const t of [
    "[확인됨]을 누릅니다 — 이 질문이 AI 개선용 평가 목록에 올라갑니다.",
    "원인이 자료 부족이면 보강합니다: 홈페이지 등록(노선표·안내 페이지), 장소 표, 교내 문서, 용어 사전.",
    "보강 뒤 [미응답·피드백]에서 같은 질문을 [재확인]하거나 학생 화면에서 다시 물어 확인합니다.",
    "맞게 답하면 [수정 완료]로 닫습니다. 학교 원문 자체가 틀렸으면 [원문 확인 필요]로 두고 담당 부서에 알립니다.",
  ]) ol.append(el("li", null, t));
  box.append(ol);
  return box;
}

async function viewReports(view) {
  let { items } = await api(`/reports?type=${reportType}`);
  if (reportType === "wrong_info" && (await autoAnalyze(items))) ({ items } = await api(`/reports?type=${reportType}`));
  const tabs = el("div");
  for (const [t, label] of [["tip", "꿀팁"], ["wrong_info", "잘못된 정보"]]) {
    tabs.append(btn(label, async () => { reportType = t; openTab("reports"); }, t === reportType ? "act primary" : "act"));
  }
  const isTip = reportType === "tip";
  const statusMap = isTip ? TIP_STATUS : WRONG_STATUS;
  const cols = isTip
    ? [[(r) => (r.seq ? `#${r.seq}` : "—"), "번호"], [(r) => badge(r.status), "상태"], [(r) => statusMap[r.status] || r.status, "설명"], ["text_masked", "제보 내용(마스킹)"], ["published_text", "다듬은 문장"],
       [(r) => `${r.confirm ?? 0} / ${r.dispute ?? 0} / 신고 ${r.flags ?? 0}`, "맞아요/달라요/신고"],
       [(r) => [r.safety && "안전", r.contested && "이견 많음", r.burst_risk && "몰표 의심", r.flag_review && "신고 확인 필요", r.recheck && "확인 권장"].filter(Boolean).join(" · ") || "—", "표시"],
       ["agent_reason", "에이전트 의견"], ["reason", "반려 사유"], ["created_at", "제보"]]
    : [[(r) => (r.seq ? `#${r.seq}` : "—"), "번호"], [(r) => badge(r.status), "상태"], [(r) => statusMap[r.status] || r.status, "설명"], [(r) => WRONG_KIND[r.kind] || "—", "유형"], ["text_masked", "제보 내용"],
       [(r) => r.snapshot?.question_masked || "—", "질문"], [(r) => (r.snapshot?.answer_text || "").slice(0, 160) || "—", "답변(당시)"],
       [(r) => (r.answer_claim ? `${r.answer_claim} → 원문: ${r.evidence_value || "?"}` : "—"), "불일치"],
       [(r) => { const w = el("span"); for (const c of r.snapshot?.cards || []) { const a = sourceLink(c.url); a.textContent = c.title || c.id; w.append(a, document.createTextNode(" ")); } return w; }, "인용 근거"],
       ["agent_reason", "에이전트 의견"], [agentCell, "에이전트 조치"], ["created_at", "제보"]];
  const actions = (row) => isTip
    ? [
        ["pending", "verifying", "student_approved", "hidden"].includes(row.status) ? btn("승인", async () => {
          const edited = prompt("학생 답변에 쓸 문장으로 다듬을 수 있습니다(그대로 두면 원문).", row.published_text || row.text_masked || "");
          if (edited === null) return;
          await reportAction(row, "approve", "승인", { ask: false, text: edited });
        }, "act primary") : null,
        row.status === "pending" ? btn("공개 투표로", () => reportAction(row, "to_vote", "공개 투표로 전환", { ask: false })) : null,
        ["pending", "verifying"].includes(row.status) ? btn("반려", () => reportAction(row, "reject", "반려")) : null,
        ["verifying", "student_approved", "approved"].includes(row.status) ? btn("가림", () => reportAction(row, "hide", "가림")) : null,
        row.status === "hidden" ? btn("복구", () => reportAction(row, "restore", "복구")) : null,
        ["student_approved", "approved"].includes(row.status) ? btn("회수", () => reportAction(row, "withdraw", "회수")) : null,
      ]
    : [
        row.status !== "confirmed" && row.status !== "resolved" ? tip(btn("확인됨", () => reportAction(row, "confirm", "확인됨"), "act primary"), "confirm") : null,
        row.status !== "resolved" ? tip(btn("수정 완료", () => reportAction(row, "resolve", "수정 완료")), "resolve") : null,
        ["pending", "confirmed"].includes(row.status) ? tip(btn("원문 확인 필요", () => reportAction(row, "needs_source_review", "원문 확인 필요")), "needs_source_review") : null,
        row.status === "pending" ? tip(btn("이상 없음", () => reportAction(row, "no_issue", "이상 없음")), "no_issue") : null,
        row.status === "pending" ? tip(btn("반려", () => reportAction(row, "reject", "반려")), "reject") : null,
      ];
  // 되돌리기(교수님 #763): 마지막 처리를 직전 상태로 — 그 뒤 상태가 바뀌었으면 서버가 거부
  const withUndo = (row) => [
    ...actions(row),
    row.undo_action ? btn("되돌리기", () => reportAction(row, "undo", "되돌리기", { ask: false })) : null,
  ];
  view.replaceChildren(
    el("div", "section-head", "제보함"),
    el("p", "hint", isTip
      ? "학생 꿀팁: 에이전트가 광고·개인정보·범위 밖은 자동 반려하고, 통과한 것은 공개 투표(24시간·10표·맞아요 80%)로 학생 확인 승인됩니다. 안전 관련·이견 많음·몰표 의심은 관리자가 결정합니다. 이벤트 경품은 관리자 확인 건만 인정합니다."
      : "잘못된 정보 제보: 답변과 인용 원문의 숫자·날짜가 명백히 다를 때만 에이전트가 '확인됨'으로 표시합니다. 원문과 일치한다는 이유로 자동 반려하지 않습니다(원문이 낡았을 수 있음)."),
    isTip ? "" : wrongGuide(),
    tabs,
    el("p", "hint", "처리를 잘못 눌렀으면 [되돌리기]로 직전 상태로 돌릴 수 있습니다(마지막 처리 1회, 감사 로그에 남음)."),
    table(items, cols, withUndo),
  );
}

async function viewPhonebook(view) {
  const { items } = await api("/sources");
  const pb = items.find((s) => s.id === "phonebook") || {};
  const form = el("form", "inline");
  form.append(
    field("전화번호부 파일(HWP 권장 — PDF는 번호가 빠질 수 있음)", "file", "file", { required: true }),
    field("사유", "reason", "text", { required: true, placeholder: "2026학년도 2학기 전화번호부 갱신" }),
  );
  const up = el("button", "act primary", "올리고 자동 적용");
  up.type = "submit";
  form.append(up);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!confirm("부서 연락처를 이 전화번호부로 통째로 교체합니다. 진행할까요?")) return;
    const fd = new FormData(form);
    fd.append("request_id", rid());
    try {
      await api("/directory", { method: "POST", form: fd });
      flash("업로드했습니다. 변환·적용이 끝나면 상태가 '적용됨'으로 바뀝니다(1~2분).");
      openTab("phonebook");
    } catch (err) {
      if (err.message !== "unauthorized") flash(err.message, true);
    }
  });
  const stats = pb.last_stats || {};
  view.replaceChildren(
    el("p", "hint", "학교 전화번호부를 올리면 부서 대표 번호가 자동으로 바뀌어 학생 답변의 '담당 부서'에 쓰입니다. 직원 이름은 저장하지 않습니다."),
    table([pb], [[(r) => badge(r.status || "없음"), "상태"], ["last_success_at", "마지막 적용"], ["doc_count", "부서 수"],
      [() => stats.with_phone, "번호 있음"], [() => stats.added, "추가"], [() => stats.removed, "삭제"], ["error_code", "실패 원인"]]),
    form,
  );
}

async function initVersion() {
  const v = await api("/versions");
  const b = $("#version-btn");
  b.textContent = `v${v.current}`;
  b.hidden = false;
  b.onclick = () => showVersions(v);
}

function showVersions(v) {
  const view = $("#view");
  const wrap = el("div");
  wrap.append(el("h3", null, `버전 내역 — 현재 코드 v${v.current}`));
  for (const it of v.items) {
    const box = el("section", "tile");
    const head = el("div");
    head.append(el("strong", null, `v${it.version}`), el("span", "hint", ` ${it.date} · `));
    head.append(it.deployed_at ? badge(`배포됨 ${fmt(it.deployed_at)}`) : el("span", "status warn", "미배포"));
    box.append(head, el("div", null, it.title));
    if (it.revision) box.append(el("div", "hint", `Cloud Run 리비전: ${it.revision}`));
    const ul = el("ul");
    for (const line of it.items || []) ul.append(el("li", null, line));
    box.append(ul);
    wrap.append(box);
  }
  document.querySelectorAll("#tabs button").forEach((x) => x.setAttribute("aria-selected", "false"));
  view.replaceChildren(wrap);
}

async function viewReview(view) {
  const [sources, places, docs] = await Promise.all([api("/sources"), api("/places"), api("/documents")]);
  const rules = sources.items.find((s) => s.id === "rules");
  // 사용 안내(교수님 #764: 어떻게 쓰는지 모르겠다)
  const guide = el("div", "msg");
  guide.append(el("strong", null, "검수 대기함 쓰는 법 — 자동 수집한 내용 중 사람이 확인해야 게시되는 것만 모였습니다."));
  const steps = el("ol");
  for (const s of [
    "장소·부서: 각 행의 근거(원문 문장)와 원문 링크를 보고 위치·전화가 맞는 행만 체크한 뒤 [선택 항목 검수 완료]를 누르세요. 검수한 항목만 학생 답변에 나옵니다.",
    "위치·전화가 틀린 행은 [장소 표] 탭에서 [편집]으로 고친 뒤 검수하세요. 맞지 않는 행은 그대로 두면 학생에게 보이지 않습니다.",
    "교내 문서: 여기서는 상태만 보여 줍니다. [교내 문서] 탭에서 미리보기를 확인하고 게시하세요.",
    "검수 실패 규정: 학칙 자동 파싱에서 형식이 깨져 뺀 규정입니다. 학생 답변에는 이전 버전이 계속 쓰이며, 처리할 일은 없습니다.",
  ]) steps.append(el("li", null, s));
  guide.append(steps);
  view.replaceChildren(
    guide,
    el("h3", null, "검수 실패 규정(색인 제외, 이전 버전 유지)"),
    el("p", rules?.rejected_rule_nos?.length ? "msg err" : "hint", rules?.rejected_rule_nos?.length ? `규정 번호: ${rules.rejected_rule_nos.join(", ")}` : "없음"),
    el("h3", null, "장소·부서 검수 대기 — 원문 문장을 보고 맞는 것만 체크"),
    placeTable(places.items.filter((p) => p.status === "pending"), { refreshTab: "review", includeEvidence: true }),
    el("h3", null, "교내 문서 검토 대기"),
    table(docs.items.filter((d) => ["staging", "publishing"].includes(d.status)), [["title", "제목"], [(r) => badge(r.status), "상태"], [(r) => badge(r.preview_status), "미리보기"], ["preview_error_code", "오류"]]),
  );
}

async function viewUnanswered(view) {
  const [un, fb] = await Promise.all([api("/unanswered"), api("/feedback")]);
  // 재확인(교수님 #765): 지금 답할 수 있으면 '검증완료'로 바뀌고 30일 뒤 목록에서 사라진다
  const state = (r) => {
    if (r.status === "verified") {
      const b = el("span", "status", `검증완료 ${String(r.verified_at || "").slice(0, 10)}`);
      if (r.verified_answer) b.title = r.verified_answer;
      return b;
    }
    const wrap = el("span");
    if (r.status === "unresolved") wrap.append(el("span", "hint", "아직 답 못 함 "));
    const b = btn("재확인", async () => {
      b.disabled = true;
      b.textContent = "확인 중…";
      let got = null;
      try {
        got = await api(`/unanswered/${encodeURIComponent(r.id)}/recheck`, { method: "POST" });
      } finally {
        await openTab("unanswered");
      }
      flash(got.status === "verified" ? "이제 답할 수 있습니다 — 검증완료로 표시했습니다." : "아직 답하지 못합니다.", got.status !== "verified");
    });
    wrap.append(b);
    return wrap;
  };
  const rank = (r) => (r.status === "verified" ? 1 : 0);
  const rows = [...un.items].sort((a, b) => rank(a) - rank(b) || (b.count || 0) - (a.count || 0));
  // 선택 재확인 / 전체 재확인(교수님 #777): 한 건씩 차례로 확인(한 건에 약 10초)
  const picked = new Set();
  const pick = (r) => {
    if (r.status === "verified") return "";
    const cb = el("input");
    cb.type = "checkbox";
    cb.setAttribute("aria-label", "재확인할 질문 선택");
    cb.addEventListener("change", () => (cb.checked ? picked.add(r.id) : picked.delete(r.id)));
    return cb;
  };
  const progress = el("span", "hint");
  const runMany = async (ids) => {
    if (!ids.length) return flash("재확인할 질문을 고르세요.", true);
    let ok = 0;
    bar.querySelectorAll("button").forEach((x) => (x.disabled = true));
    for (const [i, id] of ids.entries()) {
      progress.textContent = ` 확인 중 ${i + 1}/${ids.length}…`;
      const got = await api(`/unanswered/${encodeURIComponent(id)}/recheck`, { method: "POST" }).catch(() => null);
      if (got?.status === "verified") ok += 1;
    }
    await openTab("unanswered");
    flash(`${ids.length}건 재확인 — 검증완료 ${ok}건, 아직 답 못 함 ${ids.length - ok}건`, ok === 0);
  };
  const bar = el("div", "bulk-bar");
  bar.append(
    btn("선택 재확인", () => runMany([...picked])),
    btn("전체 재확인", () => {
      const ids = rows.filter((r) => r.status !== "verified").map((r) => r.id);
      if (ids.length > 5 && !confirm(`${ids.length}건을 차례로 확인합니다(한 건에 약 10초). 진행할까요?`)) return;
      return runMany(ids);
    }),
    progress,
  );
  view.replaceChildren(
    el("h3", null, "답하지 못한 질문(범위 밖 제외, 빈도순 참고)"),
    el("p", "hint", "자료를 보강한 뒤 [재확인]을 누르면 지금 답할 수 있는지 다시 확인합니다. 답할 수 있으면 '검증완료'로 바뀌고(마우스를 올리면 답변 요약) 30일 뒤 목록에서 사라집니다. 여러 건은 골라서 [선택 재확인], 검증 안 된 것 모두는 [전체 재확인]."),
    bar,
    table(rows, [[pick, "선택"], ["query_masked", "질문(마스킹)"], ["count", "횟수"], ["fallback_reason", "사유"], ["last_at", "최근"], [state, "확인"]]),
    el("h3", null, "피드백"),
    table(fb.items, [["rating", "평가"], ["comment_masked", "의견"], ["turn_id", "턴"], ["created_at", "시각"]]),
  );
}

async function viewStats(view) {
  const s = await api("/stats?days=30");
  const tiles = el("div", "tiles");
  const add = (label, v) => {
    const t = el("div", "tile");
    t.append(el("div", "num", fmt(v)), el("div", "label", label));
    tiles.append(t);
  };
  add("최근 30일 턴", s.total);
  for (const [k, v] of Object.entries(s.outcomes || {})) add(`결과: ${k}`, v);
  add("평균 응답(ms)", s.latency_ms?.average);
  add("p95 응답(ms)", s.latency_ms?.p95);
  // 운영 관측(#747): 최근 2시간 답변 품질 경고 — 위험 등급은 메일로도 온다
  const w = s.warning || { level: "ok", turns: 0, actionable: 0, rate: 0 };
  const warnText = { ok: "정상", warning: "경고", critical: "위험" }[w.level] || w.level;
  const warn = el("p", `msg${w.level === "ok" ? "" : " err"}`, `최근 2시간 답변 품질: ${warnText} — 범위 안 질문 ${w.turns}건 중 답을 못 한 질문 ${w.actionable}건(${Math.round(w.rate * 100)}%)`);
  const pairs = (o) => Object.entries(o || {}).map(([k, n]) => ({ k, n }));
  // 답을 못 한 이유를 한국어로, 이유별로 거르기(교수님 #779)
  const REASON = { out_of_scope: "서비스 범위 밖", no_evidence: "근거 자료 없음", verification_failed: "검증 탈락", deadline: "시간 초과", tool_failure: "도구 오류", unknown: "알 수 없음" };
  const ko = (k) => `${REASON[k] || k} (${k})`;
  const recent = s.recent_fallbacks || [];
  const filter = el("select");
  filter.setAttribute("aria-label", "사유로 거르기");
  filter.append(new Option(`전체 (${recent.length})`, ""));
  for (const k of Object.keys(REASON).filter((k) => recent.some((r) => r.reason === k))) {
    filter.append(new Option(`${ko(k)} ${recent.filter((r) => r.reason === k).length}건`, k));
  }
  const recentBox = el("div");
  const drawRecent = () => recentBox.replaceChildren(table(recent.filter((r) => !filter.value || r.reason === filter.value), [["created_at", "시각"], [(r) => REASON[r.reason] || r.reason, "사유"], ["query", "질문"]]));
  filter.addEventListener("change", drawRecent);
  drawRecent();
  view.replaceChildren(
    warn,
    tiles,
    el("h3", null, "답을 못 한 이유"),
    table(pairs(s.fallback_reasons), [[(r) => ko(r.k), "사유"], ["n", "건수"]]),
    el("h3", null, "최근 답을 못 한 질문(개인정보 가림)"),
    filter,
    recentBox,
    el("h3", null, "화면 오류(최근 14일, 종류만)"),
    table(pairs(s.client_errors), [["k", "화면:종류"], ["n", "건수"]]),
    el("h3", null, "일자별"),table(Object.entries(s.daily || {}).map(([d, n]) => ({ d, n: typeof n === "object" ? JSON.stringify(n) : n })), [["d", "날짜"], ["n", "건수"]]));
}

async function viewAdmins(view) {
  const data = await api("/admins");
  const form = el("form", "inline");
  form.append(
    field("관리자 이메일", "email", "email", { required: true, placeholder: "admin@example.edu" }),
    field("메모", "note", "text", { placeholder: "소속·용도(선택)" }),
    field("추가 사유", "reason", "text", { required: true }),
  );
  const submit = el("button", "act primary", "관리자 추가");
  submit.type = "submit";
  form.append(submit);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const values = Object.fromEntries(new FormData(form));
    const email = String(values.email || "").trim().toLowerCase();
    if (!confirm(`${email} 계정을 관리자로 추가할까요?`)) return;
    submit.disabled = true;
    try {
      await api("/admins", {
        method: "POST",
        body: { ...values, email, request_id: rid() },
      });
      flash(`${email} 계정을 추가했습니다.`);
      openTab("admins");
    } catch (err) {
      if (err.message !== "unauthorized") flash(err.message, true);
    } finally {
      submit.disabled = false;
    }
  });

  const isSuper = data.current_role === "super_admin";
  const bootstrapLabel = (row) =>
    row.bootstrap ? el("span", "status", "부트스트랩") : el("span", "hint", row.requested_at && !row.added_by ? "승인 요청" : "화면 등록");
  const roleLabel = (row) => el("span", row.role === "super_admin" ? "status" : "hint", row.role === "super_admin" ? "최고관리자" : "관리자");
  const decide = (row, action, role, label) => async () => {
    const reason = askReason(`${row.email} ${label}`);
    if (!reason) return;
    await api(`/admins/${encodeURIComponent(row.email)}/${action}`, { method: "POST", body: { role, reason, request_id: rid() } });
    flash(`${row.email} ${label} 완료`);
    openTab("admins");
  };
  view.replaceChildren(
    el("p", "hint", "관리자가 되려는 사람은 이 화면에서 Google 로그인 후 [관리자 승인 요청]을 누르고, 최고관리자가 여기서 승인합니다. 최고관리자만 승인·거절·삭제·역할 변경을 할 수 있습니다."),
    el("p", "hint", "Google 로그인 앱이 '테스트' 상태인 동안에는 새 계정도 GCP 콘솔의 테스트 사용자에 등록돼 있어야 로그인됩니다(프로덕션 게시 후에는 불필요). 부트스트랩 관리자는 배포 설정값이라 이 화면에서 바꿀 수 없습니다."),
    isSuper ? form : el("p", "hint", "일반 관리자는 목록만 볼 수 있습니다."),
    table(
      data.items,
      [
        ["email", "이메일"],
        [bootstrapLabel, "구분"],
        [roleLabel, "역할"],
        [(row) => badge(row.status), "상태"],
        ["note", "메모"],
        ["added_by", "추가한 사람"],
        ["added_at", "추가 시각"],
      ],
      (row) => [
        isSuper && row.status === "pending" ? btn("승인(관리자)", decide(row, "approve", "admin", "관리자로 승인"), "act primary") : null,
        isSuper && row.status === "pending" ? btn("승인(최고관리자)", decide(row, "approve", "super_admin", "최고관리자로 승인")) : null,
        isSuper && row.status === "pending" ? btn("거절", decide(row, "reject", "admin", "요청 거절"), "act danger") : null,
        isSuper && !row.bootstrap && row.status === "active" && row.email !== data.current_email
          ? btn(row.role === "super_admin" ? "관리자로 변경" : "최고관리자로 변경", decide(row, "role", row.role === "super_admin" ? "admin" : "super_admin", "역할 변경"))
          : null,
        isSuper && !row.bootstrap && row.status === "active" && row.email !== data.current_email
          ? btn("삭제", async () => {
              const reason = askReason(`${row.email} 관리자 삭제`);
              if (!reason || !confirm(`${row.email} 계정의 관리자 권한을 삭제할까요?`)) return;
              await api(`/admins/${encodeURIComponent(row.email)}/remove`, {
                method: "POST",
                body: { reason, request_id: rid() },
              });
              flash(`${row.email} 계정의 관리자 권한을 삭제했습니다.`);
              openTab("admins");
            }, "act danger")
          : null,
      ],
    ),
  );
}

async function viewAudit(view) {
  const { items } = await api("/audit");
  view.replaceChildren(table(items, [["created_at", "시각"], ["actor", "누가"], ["action", "무엇을"], ["target", "대상"], ["reason", "왜"], [(r) => badge(r.result), "결과"]]));
}

// 자주 묻는 질문(교수님 #781): 질문 신규·수정·삭제·복사, 1~12월 목록으로 끌어다 놓기
async function viewFaq(view) {
  const cfg = await api("/faq");
  let dirty = false;
  const nowMonth = new Date().getMonth() + 1;
  const byId = () => Object.fromEntries(cfg.questions.map((q) => [q.id, q]));
  const newId = () => `q${Date.now().toString(36)}${Math.random().toString(36).slice(2, 5)}`;
  const status = el("span", "hint");
  const pool = el("div", "faq-pool");
  const months = el("div", "faq-months");
  const saveBtn = btn("저장", async () => {
    const saved = await api("/faq", { method: "PUT", body: { questions: cfg.questions, months: cfg.months } });
    Object.assign(cfg, saved);
    dirty = false;
    draw();
    flash("저장했습니다. 5분 안에 첫 화면에 반영됩니다.");
  }, "act primary");
  const changed = () => {
    dirty = true;
    draw();
  };

  const addForm = el("form", "inline");
  addForm.append(
    field("새 질문", "text", "text", { required: true, placeholder: "예: 통학버스 시간표 알려 주세요" }),
    field("분류", "category", "text", { placeholder: "예: 학교생활" }),
  );
  const addBtn = el("button", "act", "추가");
  addBtn.type = "submit";
  addForm.append(addBtn);
  addForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = addForm.text.value.trim();
    if (!text) return;
    if (cfg.questions.some((q) => q.text === text)) return flash("같은 질문이 이미 있습니다.", true);
    cfg.questions.push({ id: newId(), text, category: addForm.category.value.trim() || "기타" });
    addForm.reset();
    changed();
  });

  // 끌어다 놓기: 질문 목록 → 월(넣기), 월 → 월(옮기기), 같은 달 안(순서 바꾸기)
  const drag = (node, payload) => {
    node.draggable = true;
    node.addEventListener("dragstart", (e) => {
      e.dataTransfer.setData("text/plain", JSON.stringify(payload));
      e.dataTransfer.effectAllowed = "copyMove";
    });
  };
  const dropInto = (m, beforeId) => (e) => {
    e.preventDefault();
    e.stopPropagation();
    let p;
    try {
      p = JSON.parse(e.dataTransfer.getData("text/plain"));
    } catch {
      return;
    }
    if (!p || !p.id) return;
    const list = cfg.months[m].filter((x) => x !== p.id);
    const at = beforeId ? list.indexOf(beforeId) : -1;
    list.splice(at < 0 ? list.length : at, 0, p.id);
    if (list.length > 12) return flash("한 달에 12개까지 넣을 수 있습니다.", true);
    if (p.from && p.from !== m) cfg.months[p.from] = cfg.months[p.from].filter((x) => x !== p.id);
    cfg.months[m] = list;
    changed();
  };

  function draw() {
    const q = byId();
    status.textContent = dirty ? " 저장하지 않은 변경이 있습니다." : "";
    pool.replaceChildren(el("h3", null, `질문 목록 (${cfg.questions.length})`), addForm);
    const ul = el("ul", "faq-q");
    for (const item of cfg.questions) {
      const li = el("li", "faq-item");
      drag(li, { id: item.id, from: null });
      const used = Object.entries(cfg.months).filter(([, ids]) => ids.includes(item.id)).map(([m]) => `${m}월`);
      li.append(el("span", "faq-cat", item.category), el("span", "faq-text", item.text), el("span", "hint", used.length ? used.join(" ") : "미배치"));
      const pick = el("select");
      pick.setAttribute("aria-label", "월에 넣기");
      pick.append(new Option("월에 넣기", ""));
      for (let m = 1; m <= 12; m += 1) pick.append(new Option(`${m}월`, String(m)));
      pick.addEventListener("change", () => {
        const m = pick.value;
        if (!m || cfg.months[m].includes(item.id)) return;
        if (cfg.months[m].length >= 12) return flash("한 달에 12개까지 넣을 수 있습니다.", true);
        cfg.months[m].push(item.id);
        changed();
      });
      li.append(
        pick,
        btn("수정", () => {
          const text = prompt("질문", item.text);
          if (text == null || !text.trim()) return;
          const cat = prompt("분류", item.category);
          item.text = text.trim();
          item.category = ((cat == null ? item.category : cat) || "기타").trim();
          changed();
        }),
        btn("복사", () => {
          const i = cfg.questions.indexOf(item);
          cfg.questions.splice(i + 1, 0, { id: newId(), text: `${item.text} (복사)`, category: item.category });
          changed();
        }),
        btn("삭제", () => {
          if (!confirm(`'${item.text}' 질문을 지울까요? 모든 달에서 빠집니다.`)) return;
          cfg.questions = cfg.questions.filter((x) => x !== item);
          for (const m of Object.keys(cfg.months)) cfg.months[m] = cfg.months[m].filter((x) => x !== item.id);
          changed();
        }),
      );
      ul.append(li);
    }
    pool.append(ul);
    months.replaceChildren();
    for (let m = 1; m <= 12; m += 1) {
      const key = String(m);
      const box = el("section", `faq-month${m === nowMonth ? " now" : ""}`);
      box.addEventListener("dragover", (e) => e.preventDefault());
      box.addEventListener("drop", dropInto(key, null));
      box.append(el("h4", null, `${m}월${m === nowMonth ? " (이번 달)" : ""} · ${cfg.months[key].length}개`));
      const ol = el("ol");
      for (const [i, id] of cfg.months[key].entries()) {
        if (!q[id]) continue;
        const li = el("li", `faq-chip${i < 6 ? "" : " extra"}`);
        drag(li, { id, from: key });
        li.addEventListener("dragover", (e) => e.preventDefault());
        li.addEventListener("drop", dropInto(key, id));
        const x = el("button", "faq-x", "×");
        x.type = "button";
        x.setAttribute("aria-label", `${m}월에서 빼기`);
        x.addEventListener("click", () => {
          cfg.months[key] = cfg.months[key].filter((v) => v !== id);
          changed();
        });
        li.append(el("span", null, q[id].text), x);
        ol.append(li);
      }
      if (!cfg.months[key].length) ol.append(el("li", "hint", "여기로 끌어다 놓으세요"));
      box.append(ol);
      months.append(box);
    }
  }
  draw();
  const bar = el("div", "bulk-bar");
  bar.append(saveBtn, status);
  const editor = el("div", "faq-editor");
  editor.append(pool, months);
  view.replaceChildren(
    el("div", "section-head", "자주 묻는 질문"),
    el("p", "hint", "첫 화면에는 이번 달 목록의 위에서 6개가 칩으로 나오고(7번째부터는 흐리게 표시), [전체 보기]에는 모든 질문이 분류별로 나옵니다. 질문을 오른쪽 달로 끌어다 놓으면 그 달에 들어가고(휴대폰은 '월에 넣기'), 달끼리 끌면 옮겨지며, 같은 달 안에서 끌면 순서가 바뀝니다. [저장]을 눌러야 반영됩니다."),
    bar,
    editor,
  );
}

// 용어 사전(교수님 #767): 경남대 고유 용어를 등록하면 질문 분석·답변 작성에 뜻이 참고로 들어간다
async function viewGlossary(view) {
  const g = await api("/glossary");
  const form = el("form", "inline");
  form.append(
    field("용어", "term", "text", { required: true, placeholder: "예: 너른마당" }),
    field("다른 이름(쉼표로)", "aliases", "text", { placeholder: "예: 너른 마당" }),
    field("뜻·위치", "meaning", "text", { required: true, placeholder: "학생이 이 말을 쓸 때 뜻하는 것" }),
  );
  const save = el("button", "act primary", "저장");
  save.type = "submit";
  form.append(save);
  const fill = (t) => {
    form.term.value = t.term;
    form.aliases.value = (t.aliases || []).join(", ");
    form.meaning.value = t.meaning;
    form.meaning.focus();
  };
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    await api("/glossary/terms", {
      method: "POST",
      body: { term: form.term.value.trim(), aliases: form.aliases.value.split(",").map((a) => a.trim()).filter(Boolean), meaning: form.meaning.value.trim() },
    });
    await openTab("glossary");
    flash("저장했습니다. 5분 안에 답변에 반영됩니다.");
  });
  const terms = table(g.campus_terms || [], [["term", "용어"], [(r) => (r.aliases || []).join(", ") || "—", "다른 이름"], ["meaning", "뜻·위치"], ["source", "출처"]], (row) => [
    btn("수정", () => fill(row)),
    btn("삭제", async () => {
      if (!confirm(`'${row.term}' 용어를 지울까요?`)) return;
      await api("/glossary/terms/delete", { method: "POST", body: { term: row.term } });
      await openTab("glossary");
      flash("삭제했습니다.");
    }),
  ]);
  const pairs = (g.protected_confusions || []).map((p) => ({ p: p.join(" ↔ ") }));
  view.replaceChildren(
    el("div", "section-head", "경남대 용어"),
    el("p", "hint", "학생들이 쓰는 학교 고유의 말(너른마당·월영지 등)을 등록하면, 질문에 그 말이 나올 때 AI가 뜻을 알고 알맞은 자료를 찾습니다. 답변의 근거(인용)는 여전히 학교 원문만 씁니다. '기본'은 처음 넣어 둔 용어이며 수정·삭제하면 관리자 값이 우선합니다."),
    form,
    terms,
    el("h3", null, "혼동 주의 단어(자동 교정 금지)"),
    el("p", "hint", "오타 교정 중에 뜻이 크게 바뀌는 쌍입니다. AI가 둘 사이를 멋대로 바꾸지 않고 학생에게 되묻습니다(예: 휴학을 퇴학으로 고치지 않음). 코드 설정(config/glossary.yml)으로 관리합니다."),
    table(pairs, [["p", "단어 쌍"]]),
  );
}

window.addEventListener("hashchange", () => token && openTab(location.hash.slice(1)));
hydrateIcons();
initLogin();
