// 학교 일정 전체보기(월 달력). 우리가 가진 campus_events 중 검수 완료(active)만 보인다.
// 모든 외부 텍스트는 textContent로만 넣는다(innerHTML 금지).
import { calendarButtons } from "./ics.js";

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
const pad = (n) => String(n).padStart(2, "0");
const isoOf = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const TAG = { event: "행사", scholarship: "장학" };
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

function render(ym, items) {
  const [y, m] = ym.split("-").map(Number);
  $("#month-title").textContent = `${y}년 ${m}월`;
  document.title = `${y}년 ${m}월 학교 일정 — 캠퍼스 브릿지`;
  const byDay = new Map();
  for (const e of items) {
    for (const day of days(e.start, e.end)) {
      if (!day.startsWith(ym)) continue;
      if (!byDay.has(day)) byDay.set(day, []);
      byDay.get(day).push(e.title);
    }
  }
  const box = el("div", "cal-month");
  const grid = el("div", "cal-grid");
  grid.setAttribute("role", "grid");
  for (const [i, w] of ["일", "월", "화", "수", "목", "금", "토"].entries()) grid.append(el("div", `cal-dow${i === 0 ? " sun" : i === 6 ? " sat" : ""}`, w));
  const first = new Date(y, m - 1, 1).getDay();
  const count = new Date(y, m, 0).getDate();
  for (let i = 0; i < first; i += 1) grid.append(el("div", "cal-cell empty"));
  const today = isoOf(new Date());
  for (let d = 1; d <= count; d += 1) {
    const iso = `${ym}-${pad(d)}`;
    const dow = (first + d - 1) % 7;
    const titles = byDay.get(iso) || [];
    const cell = el("div", `cal-cell${titles.length ? " has" : ""}${dow === 0 ? " sun" : dow === 6 ? " sat" : ""}${iso === today ? " today" : ""}`);
    cell.append(el("span", "cal-num", String(d)));
    for (const t of titles.slice(0, 3)) cell.append(el("span", "cal-ev", t));
    if (titles.length > 3) cell.append(el("span", "cal-ev", `+${titles.length - 3}`));
    if (titles.length) cell.title = titles.join("\n");
    grid.append(cell);
  }
  box.append(grid);
  const list = el("ul", "cal-list calpage-list");
  for (const e of items) {
    const li = el("li");
    const when = e.start === e.end ? md(e.start) : `${md(e.start)} ~ ${md(e.end)}`;
    const title = el(SCHOOL.test(e.url || "") ? "a" : "span", "calpage-t", e.title);
    if (title.tagName === "A") {
      title.href = e.url;
      title.target = "_blank";
      title.rel = "noopener noreferrer";
    }
    li.append(
      el("span", "cal-when", when),
      el("i", `tag${e.category === "event" ? " ev" : ""}`, TAG[e.category] || "학사"),
      title,
      calendarButtons({ title: e.title, start: e.start, end: e.end, url: e.url }, el),
    );
    list.append(li);
  }
  if (!items.length) list.append(el("li", "hint", "이 달에 등록된 일정이 없습니다."));
  box.append(list);
  $("#month").replaceChildren(box);
}

async function load(ym) {
  const url = new URL(location.href);
  url.searchParams.set("ym", ym);
  history.replaceState(null, "", url);
  try {
    const { items } = await fetch(`/api/events/month?ym=${ym}`).then((r) => r.json());
    render(ym, items || []);
  } catch {
    render(ym, []);
  }
}

let current = /^\d{4}-\d{2}$/.test(new URLSearchParams(location.search).get("ym") || "") ? new URLSearchParams(location.search).get("ym") : thisMonth();
$("#prev").addEventListener("click", () => load((current = shift(current, -1))));
$("#next").addEventListener("click", () => load((current = shift(current, 1))));
$("#this-month").addEventListener("click", () => load((current = thisMonth())));
load(current);
