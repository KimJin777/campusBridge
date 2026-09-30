// 화면 오류 '종류'만 서버로 보낸다(운영 관측 #747, GPT5 #746 엄격안).
// 오류 원문·스택·URL·입력은 보내지 않는다. 지문(fp)은 우리 스크립트 파일명+줄 위치의 짧은 해시다.

const KINDS = new Set(["js_error", "unhandled_rejection", "sse_disconnect", "map_load", "calendar_fetch"]);
const MAX_PER_SESSION = 3;
const SENT_KEY = "campusbridge.cerr.sent";
let sent = 0; // 탭 세션 단위로 센다(새로고침해도 유지, GPT5 #756)
try {
  sent = Number(sessionStorage.getItem(SENT_KEY)) || 0;
} catch {}
let page = "chat";
let version = "";

export function hash36(s) {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619);
  return (h >>> 0).toString(36).slice(0, 8);
}

// 우리 도메인 스크립트에서 난 오류만. 확장 프로그램·외부 스크립트·브라우저 잡음은 버린다(Gemini #745).
export function isNoise(message, filename, origin) {
  const msg = String(message || "");
  if (/ResizeObserver|^Script error\.?$/i.test(msg)) return true;
  const file = String(filename || "");
  if (!file) return true;
  if (/^(chrome|moz|safari(-web)?)-extension:/.test(file)) return true;
  return !file.startsWith(origin);
}

export function reportError(kind, fp = "") {
  if (!KINDS.has(kind) || sent >= MAX_PER_SESSION) return;
  sent += 1;
  try {
    sessionStorage.setItem(SENT_KEY, String(sent));
  } catch {}
  fetch("/api/client-error", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind, page, app_version: version, fp }),
    keepalive: true,
  }).catch(() => {});
}

export function setVersion(v) {
  version = /^[0-9.]{1,20}$/.test(v || "") ? v : "";
}

export function initTelemetry(pageName) {
  page = pageName;
  window.addEventListener("error", (e) => {
    if (!(e instanceof ErrorEvent) || isNoise(e.message, e.filename, location.origin)) return;
    const file = e.filename.slice(location.origin.length).split("?")[0];
    reportError("js_error", hash36(`${file}:${e.lineno}:${e.colno}`));
  });
  window.addEventListener("unhandledrejection", (e) => {
    const stack = String(e.reason?.stack || "");
    if (/extension:\/\//.test(stack)) return;
    reportError("unhandled_rejection");
  });
}
