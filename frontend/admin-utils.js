const SCHOOL_URL = /^https:\/\/([a-z0-9-]+\.)*kyungnam\.ac\.kr(?:\/|$)/i;
const PHONE = /^0\d{1,2}-\d{3,4}-\d{4}$/;

export function normalizePhoneInput(value) {
  const clean = String(value || "").trim();
  if (!clean) return "";
  if (!PHONE.test(clean)) {
    throw new Error("대표전화는 055-249-1234처럼 지역번호를 포함한 형식으로 입력해 주세요.");
  }
  return clean;
}

export function placeVerificationIssue(row) {
  if (!SCHOOL_URL.test(String(row.source_url || ""))) return "학교 HTTPS 원문 URL 없음";
  if (!row.snapshot_at) return "확인일 없음";
  const phone = String(row.phone || "").trim();
  if (phone && !PHONE.test(phone)) return "대표전화 형식 오류";
  const hasLocation = row.raw_location || row.parent_place_id;
  const hasUnitPhone = row.kind === "unit" && phone;
  if (!hasLocation && !hasUnitPhone) return "위치 또는 부서 대표전화 없음";
  return null;
}

export function schoolSourceUrl(url) {
  return SCHOOL_URL.test(String(url || ""));
}
