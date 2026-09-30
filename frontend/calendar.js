// 학교 일정 전체보기(월 달력). 우리가 가진 campus_events 중 검수 완료(active)만 보인다.
// 모든 외부 텍스트는 textContent로만 넣는다(innerHTML 금지).
import { calendarButtons, downloadIcsMany } from "./ics.js";
import { initTelemetry, reportError } from "./telemetry.js";

initTelemetry("calendar");

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
const pad = (n) => String(n).padStart(2, "0");
const isoOf = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
// 보기 옵션(교수님 2026-09-30): 분류를 켜고 끄면 달력·목록·[+ 캘린더]·[구글]에 켠 것만
const GROUPS = [
  ["calendar", "학사일정"],
  ["academic", "학사공지"],
  ["scholarship", "장학공지"],
  ["general", "일반공지"],
  ["events", "행사·세미나"],
];
const TAG = { calendar: "학사", academic: "학사공지", scholarship: "장학", general: "공지", events: "행사" };
const GROUP_KEY = "campusbridge.calendar.groups";
const groupOf = (e) => e.group || (e.category === "event" ? "events" : e.category) || "calendar";
const isEv = (e) => ["general", "events"].includes(groupOf(e));
let shown = new Set(GROUPS.map(([g]) => g));
try {
  const saved = JSON.parse(localStorage.getItem(GROUP_KEY) || "null");
  if (Array.isArray(saved)) shown = new Set(saved.filter((g) => GROUPS.some(([k]) => k === g)));
} catch {}
let monthItems = [];
const SCHOOL = /^https:\/\/([a-z0-9-]+\.)*kyungnam\.ac\.kr(\/|$)/i;

function thisMonth() {
  const d = new Date();
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}`;
}

function shift(ym, delta) {
  const [y, m] = ym.split("-").map(Number);
  const d = new Date(y, m - 1 + delta, 1);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}`;
}

function days(start, end) {
  const [ys, ms, ds] = start.split("-").map(Number);
  const [ye, me, de] = end.split("-").map(Number);
  const out = [];
  const d = new Date(ys, ms - 1, ds);
  const last = new Date(ye, me - 1, de);
  for (let i = 0; d <= last && i < 62; i += 1) {
    out.push(isoOf(d));
    d.setDate(d.getDate() + 1);
  }
  return out;
}

const md = (iso) => `${iso.slice(5, 7)}.${iso.slice(8, 10)}`;

function showDetail(e) {
  const box = $("#detail");
  const when = e.start === e.end ? e.start : `${e.start} ~ ${e.end}`;
  const head = el("div", "cal-detail-head");
  head.append(el("i", `tag${isEv(e) ? " ev" : ""}`, TAG[groupOf(e)] || "학사"), el("strong", null, e.title));
  const close = el("button", "cal-add-btn", "닫기");
  close.type = "button";
  close.addEventListener("click", () => (box.hidden = true));
  head.append(close);
  const body = el("dl", "cal-detail-body");
  const row = (k, v) => v && body.append(el("dt", null, k), el("dd", null, v));
  row("기간", when);
  row("구분", e.label);
  box.replaceChildren(head, body);
  if (SCHOOL.test(e.url || "")) {
    const a = el("a", "calpage-t", "학교 원문 보기");
    a.href = e.url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    box.append(a);
  }
  box.append(calendarButtons({ title: e.title, start: e.start, end: e.end, url: e.url }, el));
  box.hidden = false;
  box.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

// 주 단위로 그린다: 기간 일정은 시작~끝을 화살표 막대로 잇고 제목을 막대 위에(교수님 2026-09-30)
function weekRow(weekDays, items, ym, today) {
  const lanes = [];
  const segs = [];
  const firstIso = weekDays.find(Boolean);
  const lastIso = [...weekDays].reverse().find(Boolean);
  for (const e of items) {
    if (e.end < firstIso || e.start > lastIso) continue;
    const s = e.start < firstIso ? firstIso : e.start;
    const t = e.end > lastIso ? lastIso : e.end;
    const c0 = weekDays.indexOf(s);
    const c1 = weekDays.indexOf(t);
    if (c0 < 0 || c1 < 0) continue;
    let lane = lanes.findIndex((busy) => busy.every((b) => b[1] < c0 || b[0] > c1));
    if (lane < 0) lane = lanes.push([]) - 1;
    lanes[lane].push([c0, c1]);
    segs.push({ e, c0, c1, lane, head: s === e.start, tail: t === e.end });
  }
  const row = el("div", "cal-week");
  row.style.gridTemplateRows = `auto repeat(${Math.max(lanes.length, 1)}, 22px) 6px`;
  weekDays.forEach((iso, i) => {
    const dow = i;
    const cell = el("div", `cal-cell${iso ? "" : " empty"}${dow === 0 ? " sun" : dow === 6 ? " sat" : ""}${iso === today ? " today" : ""}`);
    cell.style.gridColumn = `${i + 1}`;
    cell.style.gridRow = `1 / span ${Math.max(lanes.length, 1) + 2}`;
    if (iso) cell.append(el("span", "cal-num", String(Number(iso.slice(8)))));
    row.append(cell);
  });
  for (const g of segs) {
    const range = g.e.start !== g.e.end;
    const bar = el("button", `cal-bar${range ? " range" : " single"}${g.head ? " head" : ""}${g.tail ? " tail" : ""}${isEv(g.e) ? " ev" : ""}`);
    bar.type = "button";
    bar.style.gridColumn = `${g.c0 + 1} / ${g.c1 + 2}`;
    bar.style.gridRow = `${g.lane + 2}`;
    bar.title = `${g.e.title} (${g.e.start === g.e.end ? g.e.start : `${g.e.start} ~ ${g.e.end}`})`;
    bar.append(el("span", "cal-bar-t", g.e.title));
    bar.addEventListener("click", () => showDetail(g.e));
    row.append(bar);
  }
  return row;
}

function render(ym, items) {
  const [y, m] = ym.split("-").map(Number);
  $("#month-title").textContent = `${y}년 ${m}월`;
  document.title = `${y}년 ${m}월 학교 일정 — 캠퍼스 브릿지`;
  $("#detail").hidden = true;
  const box = el("div", "cal-month");
  const dows = el("div", "cal-grid");
  for (const [i, w] of ["일", "월", "화", "수", "목", "금", "토"].entries()) dows.append(el("div", `cal-dow${i === 0 ? " sun" : i === 6 ? " sat" : ""}`, w));
  box.append(dows);
  const first = new Date(y, m - 1, 1).getDay();
  const count = new Date(y, m, 0).getDate();
  const cells = [...Array(first).fill(null), ...Array.from({ length: count }, (_, i) => `${ym}-${pad(i + 1)}`)];
  while (cells.length % 7) cells.push(null);
  const today = isoOf(new Date());
  for (let w = 0; w < cells.length; w += 7) box.append(weekRow(cells.slice(w, w + 7), items, ym, today));
  const list = el("ul", "cal-list calpage-list");
  for (const e of items) {
    const li = el("li");
    const when = e.start === e.end ? md(e.start) : `${md(e.start)} ~ ${md(e.end)}`;
    const title = el("button", "calpage-t link", e.title);
    title.type = "button";
    title.addEventListener("click", () => showDetail(e));
    li.append(
      el("span", "cal-when", when),
      el("i", `tag${isEv(e) ? " ev" : ""}`, TAG[groupOf(e)] || "학사"),
      title,
      calendarButtons({ title: e.title, start: e.start, end: e.end, url: e.url }, el),
    );
    list.append(li);
  }
  if (!items.length) list.append(el("li", "hint", shown.size ? "이 달에 등록된 일정이 없습니다." : "보기 옵션에서 분류를 하나 이상 켜 주세요."));
  box.append(list);
  $("#month").replaceChildren(box);
  const allBtn = $("#all-ics");
  allBtn.disabled = !items.length;
  allBtn.onclick = () => downloadIcsMany(items.map((e) => ({ title: e.title, start: e.start, end: e.end, url: e.url })), `${y}년 ${m}월 학교 일정`);
}

function renderGroups() {
  const box = $("#groups");
  box.replaceChildren();
  for (const [g, label] of GROUPS) {
    const n = monthItems.filter((e) => groupOf(e) === g).length;
    const b = el("button", `cal-group g-${g}`, `${label} ${n}`);
    b.type = "button";
    b.setAttribute("aria-pressed", String(shown.has(g)));
    b.addEventListener("click", () => {
      if (shown.has(g)) shown.delete(g);
      else shown.add(g);
      try {
        localStorage.setItem(GROUP_KEY, JSON.stringify([...shown]));
      } catch {}
      show();
    });
    box.append(b);
  }
  const qs = shown.size && shown.size < GROUPS.length ? `?g=${[...shown].join(",")}` : "";
  $("#all-google").href = `https://calendar.google.com/calendar/r?cid=${encodeURIComponent(`webcal://${location.host}/api/events/calendar.ics${qs}`)}`;
  const g = $("#all-google");
  g.setAttribute("aria-disabled", String(!shown.size));
  g.classList.toggle("off", !shown.size);
}

function show() {
  renderGroups();
  render(current, monthItems.filter((e) => shown.has(groupOf(e))));
}

async function load(ym) {
  const url = new URL(location.href);
  url.searchParams.set("ym", ym);
  history.replaceState(null, "", url);
  try {
    const res = await fetch(`/api/events/month?ym=${ym}`);
    if (!res.ok) throw new Error(String(res.status));
    const { items } = await res.json();
    monthItems = items || [];
  } catch {
    reportError("calendar_fetch");
    monthItems = [];
  }
  show();
}

let current = /^\d{4}-\d{2}$/.test(new URLSearchParams(location.search).get("ym") || "") ? new URLSearchParams(location.search).get("ym") : thisMonth();
$("#prev").addEventListener("click", () => load((current = shift(current, -1))));
$("#next").addEventListener("click", () => load((current = shift(current, 1))));
$("#this-month").addEventListener("click", () => load((current = thisMonth())));
load(current);
