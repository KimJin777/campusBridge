// 일정을 내 캘린더에 담기(#613 합의 2순위): .ics 파일(휴대폰·Outlook) + 구글 캘린더 링크.
// 서버에 아무것도 저장하지 않는다 — 브라우저에서 파일을 만들어 내려받는다.

const pad = (n) => String(n).padStart(2, "0");

// "2026-10-06" → "20261006", 종일 일정의 DTEND는 다음 날(배타적)
function ymd(iso) {
  return iso.replaceAll("-", "");
}

function nextDay(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  const t = new Date(y, m - 1, d + 1);
  return `${t.getFullYear()}${pad(t.getMonth() + 1)}${pad(t.getDate())}`;
}

// RFC 5545 텍스트 이스케이프
function esc(text) {
  return String(text || "").replace(/\\/g, "\\\\").replace(/;/g, "\\;").replace(/,/g, "\\,").replace(/\r?\n/g, "\\n");
}

export function icsText({ title, start, end, url }) {
  const last = end || start;
  const stamp = new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d{3}/, "");
  const uid = `${ymd(start)}-${Math.abs([...title].reduce((h, c) => (h * 31 + c.charCodeAt(0)) | 0, 7))}@campusbridge`;
  return [
    "BEGIN:VCALENDAR",
    "VERSION:2.0",
    "PRODID:-//CampusBridge//KO",
    "CALSCALE:GREGORIAN",
    "BEGIN:VEVENT",
    `UID:${uid}`,
    `DTSTAMP:${stamp}`,
    `DTSTART;VALUE=DATE:${ymd(start)}`,
    `DTEND;VALUE=DATE:${nextDay(last)}`,
    `SUMMARY:${esc(title)}`,
    `DESCRIPTION:${esc(`캠퍼스 브릿지 — 학교 원문: ${url || ""}`)}`,
    url ? `URL:${url}` : null,
    "BEGIN:VALARM",
    "TRIGGER:-P1D",
    "ACTION:DISPLAY",
    `DESCRIPTION:${esc(title)} D-1`,
    "END:VALARM",
    "END:VEVENT",
    "END:VCALENDAR",
  ].filter(Boolean).join("\r\n");
}

export function downloadIcs(ev) {
  const blob = new Blob([icsText(ev)], { type: "text/calendar;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${ev.title.replace(/[\\/:*?"<>|]/g, " ").slice(0, 40)}.ics`;
  document.body.append(a);
  a.click();
  setTimeout(() => {
    URL.revokeObjectURL(a.href);
    a.remove();
  }, 1000);
}

export function googleCalendarUrl({ title, start, end, url }) {
  const q = new URLSearchParams({
    action: "TEMPLATE",
    text: title,
    dates: `${ymd(start)}/${nextDay(end || start)}`,
    details: `캠퍼스 브릿지 — 학교 원문: ${url || ""}`,
  });
  return `https://calendar.google.com/calendar/render?${q}`;
}

// 작은 [+캘린더] 버튼 묶음: .ics 내려받기 + 구글 캘린더 새 창
export function calendarButtons(ev, el) {
  const wrap = el("span", "cal-add");
  const ics = el("button", "cal-add-btn", "+ 캘린더");
  ics.type = "button";
  ics.title = "휴대폰·Outlook 캘린더에 담기(.ics)";
  ics.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    downloadIcs(ev);
  });
  const g = el("a", "cal-add-btn", "구글");
  g.href = googleCalendarUrl(ev);
  g.target = "_blank";
  g.rel = "noopener noreferrer";
  g.title = "구글 캘린더에 담기";
  g.addEventListener("click", (e) => e.stopPropagation());
  wrap.append(ics, g);
  return wrap;
}
