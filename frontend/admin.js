// 관리자 화면(상세설계 05 §3). Google ID 토큰을 메모리에만 두고 Bearer로 보낸다.
// 모든 서버 텍스트는 textContent로만 넣는다(innerHTML 금지).

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
  b.style.color = err ? "var(--err)" : "";
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
  sources: ["데이터 출처", viewSources],
  runs: ["수집 실행", viewRuns],
  disable: ["긴급 회수", viewDisable],
  documents: ["교내 문서", viewDocuments],
  places: ["장소 표", viewPlaces],
  review: ["검수 대기함", viewReview],
  unanswered: ["미응답·피드백", viewUnanswered],
  stats: ["통계", viewStats],
  audit: ["감사 로그", viewAudit],
  glossary: ["용어 사전", viewGlossary],
};

function openTab(name) {
  if (!TABS[name]) name = "sources";
  if (location.hash.slice(1) !== name) {
    location.hash = name; // hashchange가 다시 불러 그린다(이중 로드 방지)
    return;
  }
  const nav = $("#tabs");
  nav.replaceChildren();
  for (const [key, [label]] of Object.entries(TABS)) {
    const b = el("button", null, label);
    b.type = "button";
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", String(key === name));
    b.addEventListener("click", () => openTab(key));
    nav.append(b);
  }
  $("#banner").hidden = true;
  const view = $("#view");
  view.replaceChildren(el("p", "hint", "불러오는 중…"));
  TABS[name][1](view).catch((e) => {
    if (e.message !== "unauthorized") view.replaceChildren(el("p", "msg err", e.message));
  });
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
      [["id", "출처"], ["kind", "종류"], ["schedule", "주기"], [(r) => (r.paused ? "일시정지" : "동작"), "상태"],
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
  view.replaceChildren(
    el("p", "hint", "지도·좌표는 쓰지 않습니다. 검수 완료(verified)된 행만 학생 답변에 나갑니다."),
    form,
    table(items, [["place_id", "ID"], ["name", "이름"], ["kind", "종류"], ["raw_location", "위치"],
      [(r) => badge(r.status), "상태"], ["source_url", "원문"], ["snapshot_at", "확인일"], ["verified_by", "검수자"]],
      (row) => [
        row.status !== "verified" ? btn("검수 완료", async () => {
          const reason = askReason(`「${row.name}」 위치 검수 완료`);
          if (!reason) return;
          await api(`/places/${encodeURIComponent(row.place_id || row.id)}`, { method: "PATCH", body: { status: "verified", reason, request_id: rid() } });
          openTab("places");
        }, "act primary") : null,
        btn("위치 수정", async () => {
          const loc = prompt("새 위치 문구", row.raw_location || "");
          if (!loc) return;
          const reason = askReason("위치 수정(검수 대기로 돌아갑니다)");
          if (!reason) return;
          await api(`/places/${encodeURIComponent(row.place_id || row.id)}`, { method: "PATCH", body: { raw_location: loc, reason, request_id: rid() } });
          openTab("places");
        }),
      ]),
  );
}

async function viewReview(view) {
  const [sources, places, docs] = await Promise.all([api("/sources"), api("/places"), api("/documents")]);
  const rules = sources.items.find((s) => s.id === "rules");
  view.replaceChildren(
    el("h3", null, "검수 실패 규정(색인 제외, 이전 버전 유지)"),
    el("p", rules?.rejected_rule_nos?.length ? "msg err" : "hint", rules?.rejected_rule_nos?.length ? `규정 번호: ${rules.rejected_rule_nos.join(", ")}` : "없음"),
    el("h3", null, "장소 표 검수 대기"),
    table(places.items.filter((p) => p.status === "pending"), [["name", "이름"], ["raw_location", "위치"], ["source_url", "원문"]]),
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

async function viewAudit(view) {
  const { items } = await api("/audit");
  view.replaceChildren(table(items, [["created_at", "시각"], ["actor", "누가"], ["action", "무엇을"], ["target", "대상"], ["reason", "왜"], [(r) => badge(r.result), "결과"]]));
}

async function viewGlossary(view) {
  const g = await api("/glossary");
  view.replaceChildren(el("p", "hint", "용어 사전은 읽기 전용입니다. 편집은 코드 리뷰 경로로만 합니다."), el("pre", "preview", JSON.stringify(g, null, 2)));
}

window.addEventListener("hashchange", () => token && openTab(location.hash.slice(1)));
initLogin();
