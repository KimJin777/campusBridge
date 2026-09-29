// 인라인 SVG 아이콘(외부 아이콘 폰트 없이 — CSP 'self' 유지). 24x24 stroke 아이콘.
const P = {
  chat: ["M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"],
  check: ["M22 11.1V12a10 10 0 1 1-5.9-9.1", "M22 4 12 14.01l-3-3"],
  bolt: ["M13 2 3 14h9l-1 8 10-12h-9l1-8z"],
  phone: ["M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.5 2.1L8 9.9a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.5c.9.3 1.8.6 2.8.7a2 2 0 0 1 1.7 2z"],
  help: ["M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z", "M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3", "M12 17h.01"],
  info: ["M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z", "M12 16v-4", "M12 8h.01"],
  up: ["M12 19V5", "m5 12 7-7 7 7"],
  bot: ["M12 8V4H8", "M4 8h16v12H4z", "M2 14h2", "M20 14h2", "M15 13v2", "M9 13v2"],
  spark: ["M12 3l1.9 5.8L20 11l-6.1 2.2L12 19l-1.9-5.8L4 11l6.1-2.2z"],
  book: ["M4 19.5A2.5 2.5 0 0 1 6.5 17H20", "M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"],
  thumbup: ["M7 10v12", "M15 5.9 14 10h5.8a2 2 0 0 1 2 2.3l-1.4 8a2 2 0 0 1-2 1.7H7V10l4.3-8a2.4 2.4 0 0 1 3.7 3.9z"],
  thumbdown: ["M17 14V2", "M9 18.1 10 14H4.2a2 2 0 0 1-2-2.3l1.4-8A2 2 0 0 1 5.6 2H17v12l-4.3 8a2.4 2.4 0 0 1-3.7-3.9z"],
  calendar: ["M3 4h18v18H3z", "M16 2v4", "M8 2v4", "M3 10h18"],
  alert: ["M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h16.9a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z", "M12 9v4", "M12 17h.01"],
  meal: ["M3 2v7c0 1.1.9 2 2 2h4a2 2 0 0 0 2-2V2", "M7 2v20", "M21 15V2a5 5 0 0 0-5 5v6c0 1.1.9 2 2 2h3zm0 0v7"],
  coin: ["M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z", "M16 8h-6a2 2 0 1 0 0 4h4a2 2 0 1 1 0 4H8", "M12 18V6"],
  pin: ["M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0z", "M12 13a3 3 0 1 0 0-6 3 3 0 0 0 0 6z"],
  external: ["M15 3h6v6", "M10 14 21 3", "M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"],
  building: ["M6 22V4a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v18z", "M6 12H4a2 2 0 0 0-2 2v8h4", "M18 9h2a2 2 0 0 1 2 2v11h-4", "M10 6h4", "M10 10h4", "M10 14h4", "M10 18h4"],
  retry: ["M3 12a9 9 0 0 1 15-6.7L21 8", "M21 3v5h-5", "M21 12a9 9 0 0 1-15 6.7L3 16", "M8 16H3v5"],
};

const NS = "http://www.w3.org/2000/svg";

export function icon(name, size = 16) {
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  for (const d of P[name] || P.info) {
    const path = document.createElementNS(NS, "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}

// <span class="ico" data-icon="name"> 자리를 SVG로 채운다
export function hydrateIcons(root = document) {
  root.querySelectorAll(".ico[data-icon]").forEach((span) => {
    if (!span.firstChild) span.append(icon(span.dataset.icon));
  });
}

// 추천 질문 문구 → 아이콘
export function chipIcon(text) {
  if (/식당|학식|메뉴|식단/.test(text)) return "meal";
  if (/장학/.test(text)) return "coin";
  if (/어디|위치|건물/.test(text)) return "pin";
  if (/경고|재수강|성적/.test(text)) return "alert";
  if (/일정|언제|기간/.test(text)) return "calendar";
  return "calendar";
}
