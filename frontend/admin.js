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
        await api("/glossary"); // 허용 목록 검증(서버)
        $("#who").textContent = decodeEmail(credential);
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
  events: ["학사 일정", viewEvents, "calendar"],
  places: ["장소 표", viewPlaces, "pin"],
  phonebook: ["전화번호부", viewPhonebook, "phone"],
  review: ["검수 대기함", viewReview, "check"],
  unanswered: ["미응답·피드백", viewUnanswered, "help"],
  stats: ["통계", viewStats, "bolt"],
  admins: ["관리자", viewAdmins, "bot"],
  audit: ["감사 로그", viewAudit, "info"],
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
const EVENT_STATUS = { pending: "검수 대기", active: "게시됨", disabled: "숨김" };
const EVENT_SOURCE = { calendar: "공식 학사일정", regex: "공지(자동 추출)", llm: "공지(AI 추출)" };

async function viewEvents(view) {
  const { items } = await api("/events");
  const setStatus = (row, status, label) => async () => {
    const reason = askReason(`「${row.title}」 ${label}`);
    if (!reason) return;
    await api(`/events/${encodeURIComponent(row.id)}`, { method: "PATCH", body: { status, reason, request_id: rid() } });
    await openTab("events");
    flash(`「${row.title}」 ${label} 완료`);
  };
  view.replaceChildren(
    el("div", "section-head", "학사 일정"),
    el("p", "hint", "첫 화면 '오늘·이번 주 학사 일정'에 나가는 데이터입니다. 공식 학사일정과 기간이 명확한 학사·장학 공지는 자동 게시되고, AI가 추출한 일정은 원문을 확인한 뒤 게시하세요. 매일 수집 때 갱신되며, 숨긴 일정은 다시 나타나지 않습니다."),
  );
  if (!items.length) {
    view.append(el("p", "hint", "아직 수집된 일정이 없습니다. '데이터 출처'에서 변경분 다시 수집을 실행하면 채워집니다."));
    return;
  }
  view.append(
    table(
      items,
      [[(r) => el("span", `status${r.status === "pending" ? " warn" : r.status === "disabled" ? " err" : ""}`, EVENT_STATUS[r.status] || r.status), "상태"], ["title", "일정"],
       [(r) => el("span", null, `${r.start_date || ""} ~ ${r.end_date}`), "기간"],
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

async function viewRuns(view) {
  const { items } = await api("/ingestion-runs");
  view.replaceChildren(
    btn("새로고침", () => openTab("runs")),
    table(items, [["id", "실행"], [(r) => badge(r.status), "상태"], ["phase", "단계"],
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
  view.replaceChildren(
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
  view.replaceChildren(
    el("h3", null, "답하지 못한 질문(범위 밖 제외, 빈도순 참고)"),
    table([...un.items].sort((a, b) => (b.count || 0) - (a.count || 0)), [["query_masked", "질문(마스킹)"], ["count", "횟수"], ["fallback_reason", "사유"], ["last_at", "최근"]]),
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
  view.replaceChildren(tiles, el("h3", null, "일자별"), table(Object.entries(s.daily || {}).map(([d, n]) => ({ d, n: typeof n === "object" ? JSON.stringify(n) : n })), [["d", "날짜"], ["n", "건수"]]));
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

  const bootstrapLabel = (row) =>
    row.bootstrap ? el("span", "status", "부트스트랩") : el("span", "hint", "화면 등록");
  view.replaceChildren(
    el("p", "hint", "OAuth 앱이 '테스트' 상태이면 Google 콘솔의 테스트 사용자에도 추가해야 로그인됩니다."),
    el("p", "hint", "부트스트랩 관리자는 배포 설정값(최초·비상용)이므로 이 화면에서 삭제할 수 없습니다."),
    form,
    table(
      data.items,
      [
        ["email", "이메일"],
        [bootstrapLabel, "구분"],
        [(row) => badge(row.status), "상태"],
        ["note", "메모"],
        ["added_by", "추가한 사람"],
        ["added_at", "추가 시각"],
      ],
      (row) => [
        !row.bootstrap && row.status === "active" && row.email !== data.current_email
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

async function viewGlossary(view) {
  const g = await api("/glossary");
  view.replaceChildren(el("p", "hint", "용어 사전은 읽기 전용입니다. 편집은 코드 리뷰 경로로만 합니다."), el("pre", "preview", JSON.stringify(g, null, 2)));
}

window.addEventListener("hashchange", () => token && openTab(location.hash.slice(1)));
hydrateIcons();
initLogin();
