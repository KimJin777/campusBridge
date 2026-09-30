// 대학생활 꿀팁(교수님 #697~#715): 익명 제보, 검증 중 목록의 학생 확인 투표, 승인된 꿀팁.
// 모든 서버 텍스트는 textContent로만 넣는다(innerHTML 금지).

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
const opened = Date.now();
const FLAG_REASONS = [["privacy", "개인정보"], ["abuse", "비방·광고"], ["safety", "안전 문제"]];

function receiptBox(d) {
  const box = el("div", `receipt${d.accepted ? "" : " err"}`);
  box.append(el("p", null, d.message || (d.accepted ? "접수되었습니다." : "접수하지 않았습니다.")));
  if (d.accepted && d.receipt) {
    box.append(el("p", "receipt-no", `제보 번호 ${d.receipt}`), el("p", "hint", "이 번호를 가진 사람이 제보자로 인정됩니다(이벤트용). 잃어버리면 다시 발급할 수 없으니 캡처해 두세요."));
  }
  return box;
}

async function vote(tip, value, reason) {
  const r = await fetch(`/api/tips/${encodeURIComponent(tip.id)}/vote`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify(reason ? { value, reason } : { value }),
  }).catch(() => null);
  if (!r || !r.ok) {
    const d = r ? await r.json().catch(() => ({})) : {};
    alertText(d.message || "투표하지 못했습니다. 잠시 후 다시 시도해 주세요.");
    return;
  }
  await load();
}

function alertText(text) {
  const box = $("#tip-result");
  box.replaceChildren(el("div", "receipt err", text));
}

function tipItem(t, rule) {
  const li = el("li", "tip");
  const top = el("div", "tip-top");
  top.append(el("span", `tip-badge${t.status === "verifying" ? " warn" : ""}`, t.badge || "검증 중"), el("span", null, t.category));
  if (t.created) top.append(el("span", null, t.created));
  if (t.contested) top.append(el("span", "tip-badge warn", "이견 많음 · 관리자 확인 중"));
  if (t.safety && t.status === "verifying") top.append(el("span", "tip-badge warn", "안전 관련 · 관리자 확인 후 승인"));
  li.append(top, el("p", "tip-text", t.text));
  const bar = el("div", "tip-votes");
  const b = (label, value, reason) => {
    const x = el("button", "fb", label);
    x.type = "button";
    x.setAttribute("aria-pressed", String(t.mine === value));
    x.addEventListener("click", () => vote(t, value, reason));
    return x;
  };
  bar.append(b(`직접 확인했어요 · 맞아요 ${t.confirm}`, "confirm"), b(`사실과 달라요 ${t.dispute}`, "dispute"));
  if (t.status === "verifying" && rule) {
    const total = t.confirm + t.dispute;
    const ratio = total ? Math.round((t.confirm / total) * 100) : 0;
    bar.append(el("span", "tip-progress", `${total}/${rule.min_votes}표 · 맞아요 ${ratio}%`));
  }
  const flag = el("select");
  flag.setAttribute("aria-label", "문제 신고");
  flag.append(new Option("문제 신고…", ""));
  for (const [v, label] of FLAG_REASONS) flag.append(new Option(label, v));
  flag.addEventListener("change", () => flag.value && vote(t, "flag", flag.value));
  bar.append(flag);
  li.append(bar);
  return li;
}

async function load() {
  const d = await fetch("/api/tips", { credentials: "same-origin" }).then((r) => r.json()).catch(() => null);
  if (!d) {
    $("#approved").replaceChildren(el("li", "hint", "꿀팁을 불러오지 못했습니다."));
    return;
  }
  const rule = d.rule;
  $("#rule").textContent = `공개 ${rule.min_hours}시간이 지나고 ${rule.min_votes}표 이상, '맞아요'가 ${Math.round(rule.min_ratio * 100)}% 이상이면 승인됩니다. 한 사람이 한 꿀팁에 한 번만 투표할 수 있습니다. 검증 중인 꿀팁은 챗봇이 쓰지 않습니다.`;
  $("#approved").replaceChildren(...(d.approved.length ? d.approved.map((t) => tipItem(t, null)) : [el("li", "hint", "아직 승인된 꿀팁이 없습니다.")]));
  $("#verifying").replaceChildren(...(d.verifying.length ? d.verifying.map((t) => tipItem(t, rule)) : [el("li", "hint", "검증 중인 꿀팁이 없습니다. 첫 꿀팁을 제보해 주세요!")]));
}

$("#tip-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  const btn = $("button[type=submit]", f);
  btn.disabled = true;
  const r = await fetch("/api/reports/tip", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ category: f.category.value, text: f.text.value, website: f.website.value, elapsed_ms: Date.now() - opened }),
  }).catch(() => null);
  const d = r ? await r.json().catch(() => ({})) : {};
  $("#tip-result").replaceChildren(receiptBox(r && r.ok ? d : { accepted: false, message: d.message || "보내지 못했습니다. 잠시 후 다시 시도해 주세요." }));
  if (r && r.ok && d.accepted) f.text.value = "";
  btn.disabled = false;
  await load();
});

load();
