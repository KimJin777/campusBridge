# ruff: noqa: E501 — 안내 문구·정규식은 줄바꿈하지 않는다
"""익명 제보(꿀팁·잘못된 정보)의 결정적 규칙 — 네트워크·DB·LLM 없음(교수님 #697~#715).

- 사전검사: 결정적으로 판정 가능한 위반만 자동 반려(광고·연락처·욕설·범위 밖).
  애매한 것(주입 시도처럼 보이는 문장 등)은 반려하지 않고 관리자 확인으로 보낸다.
- 제보 번호: 80비트 난수. 서버에는 해시만 저장(이벤트 때 번호 소지자 확인용).
- 꿀팁 자동 승인: 공개 24시간 + 유효 투표 10표 + '맞아요' 80% + 미해결 신고 없음 + 몰표 위험 없음.
  안전 관련 꿀팁은 표가 모여도 관리자 확인. 반대가 많으면 삭제가 아니라 '이견 많음' 표시.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

TIP_CATEGORIES = {"place": "장소·길", "facility": "편의시설", "life": "캠퍼스 생활"}
MAX_TIP_CHARS = 300
MIN_TIP_CHARS = 10
MAX_REPORT_CHARS = 200
MIN_ELAPSED_MS = 3000  # 너무 빠른 제출(자동화) 차단

# 하루 한도(일 단위 네트워크 해시 기준 — 원문 IP는 저장하지 않는다)
TIP_PER_NET_DAY = 3
REPORT_PER_NET_DAY = 5
SUBMIT_GLOBAL_DAY = 100
VOTES_PER_TOKEN_DAY = 30
VOTES_PER_NET_DAY = 2000  # 비용 보호용 느슨한 상한(교내 NAT 수천 명 고려). 판정 보호는 네트워크 다양성 규칙이 맡는다

# 자동 승인 기준(교수님 #709) — 운영 중 조정 가능
MIN_VOTES = 10
MIN_CONFIRM_RATIO = 0.8
MIN_PUBLIC_HOURS = 24
MAX_NET_SHARE = 0.5  # 한 네트워크가 표의 절반을 넘으면 몰표 위험 → 자동 승인 보류
CONTEST_RATIO = 0.5  # 반대가 이보다 많으면 '이견 많음'(삭제 아님)
FLAGS_TO_HIDE = 3  # 문제 신고 3건 → 되돌릴 수 있는 임시 가림
FLAG_NETS_TO_HIDE = (
    3  # 단, 서로 다른 네트워크 3곳 이상일 때만(한 곳의 여러 토큰은 관리자 우선 검토 — GPT5 #728)
)
RECHECK_DAYS = {"student_approved": 30, "approved": 180}  # 관리자 '확인 권장' 목록 주기

URL_RE = re.compile(r"https?://|www\.|\.(com|net|kr|io|me|ly)\b|open\.kakao", re.I)
CONTACT_RE = re.compile(
    r"\d{2,3}[-.\s]?\d{3,4}[-.\s]?\d{4}|[\w.+-]+@[\w-]+\.[\w.]+|카톡\s*(아이디|id)|인스타|@\w{3,}",
    re.I,
)
PROFANITY_RE = re.compile(r"시발|씨발|ㅅㅂ|병신|ㅂㅅ|개새|좆|존나|닥쳐|꺼져|미친놈|미친년")
AD_RE = re.compile(
    r"할인|쿠폰|이벤트\s*참여|광고|홍보합니다|가입\s*하세요|문의\s*주세요|판매|구매\s*링크"
)
# 꿀팁으로 받지 않는 범위(공식 규정 영역) — 학칙·장학·학적·등록금
OUT_OF_SCOPE_RE = re.compile(
    r"장학|등록금|휴학|복학|졸업\s*(요건|학점|사정)|학칙|수강\s*신청|성적\s*(정정|처리)"
)
INJECTION_RE = re.compile(
    r"(이전|위의?|앞의?)\s*(지시|명령|프롬프트)[^.]{0,20}(무시|잊)|system\s*prompt|ignore\s+(all|previous)",
    re.I,
)
SAFETY_RE = re.compile(r"엘리베이터|장애|휠체어|야간|밤길|비상|대피|소화기|AED|응급|심폐")

Verdict = Literal["ok", "reject", "review"]


@dataclass(frozen=True)
class Screen:
    verdict: Verdict
    reason: str = ""
    keep_body: bool = True  # 개인정보 사유 반려는 본문을 남기지 않는다


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text or "").split())


def text_hash(text: str) -> str:
    key = re.sub(r"[\s\W_]+", "", normalize(text)).casefold()
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def screen_common(text: str) -> Screen:
    if URL_RE.search(text):
        return Screen("reject", "링크·주소는 적을 수 없습니다")
    if CONTACT_RE.search(text):
        return Screen("reject", "연락처·계정은 적을 수 없습니다", keep_body=False)
    if PROFANITY_RE.search(text):
        return Screen("reject", "욕설·비방이 포함되어 있습니다")
    if AD_RE.search(text):
        return Screen("reject", "광고·홍보성 글입니다")
    if INJECTION_RE.search(text):
        return Screen("review", "지시문처럼 보이는 문장 — 관리자 확인")
    return Screen("ok")


def screen_tip(text: str) -> Screen:
    base = screen_common(text)
    if base.verdict != "ok":
        return base
    if len(normalize(text)) < MIN_TIP_CHARS:
        return Screen("reject", "내용이 너무 짧습니다")
    if OUT_OF_SCOPE_RE.search(text):
        return Screen("reject", "학칙·장학·학적 안내는 꿀팁으로 받지 않습니다(공식 규정으로 안내)")
    return Screen("ok")


def screen_report(text: str) -> Screen:
    """잘못된 정보 제보: 내용이 원문과 맞는다는 이유로는 절대 반려하지 않는다(규칙 위반만)."""
    return screen_common(text) if text.strip() else Screen("ok")


def is_safety(text: str) -> bool:
    return bool(SAFETY_RE.search(text or ""))


# ── 제보 번호 ───────────────────────────────────────────────────────────
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_receipt() -> tuple[str, str]:
    """(보여 줄 번호 'XXXX-XXXX-XXXX-XXXX', 저장할 해시). 80비트 난수."""
    n = int.from_bytes(secrets.token_bytes(10), "big")
    chars = "".join(_CROCKFORD[(n >> (5 * i)) & 31] for i in range(16))
    shown = "-".join(chars[i : i + 4] for i in range(0, 16, 4))
    return shown, receipt_hash(shown)


def receipt_hash(shown: str) -> str:
    return hashlib.sha256(re.sub(r"[^0-9A-Z]", "", shown.upper()).encode()).hexdigest()


def receipt_matches(shown: str, stored_hash: str) -> bool:
    return hmac.compare_digest(receipt_hash(shown), stored_hash)


# ── 네트워크·투표 토큰(원문은 저장하지 않는다) ─────────────────────────────
def net_hash(secret: bytes, ip: str, day: str) -> str:
    """일 단위 HMAC — 같은 날 같은 네트워크 판별만 가능, 원래 IP로 되돌릴 수 없다."""
    return hmac.new(secret, f"{day}|{ip}".encode(), hashlib.sha256).hexdigest()[:20]


def new_vote_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(f"vote|{token}".encode()).hexdigest()[:32]


# ── 꿀팁 상태 판정 ──────────────────────────────────────────────────────
def evaluate_tip(tip: dict[str, Any], now: datetime) -> dict[str, Any]:
    """투표·신고 집계를 보고 바꿀 필드를 돌려준다(변경 없으면 빈 dict).

    - 신고 3건 이상: 되돌릴 수 있는 임시 가림(hidden, 관리자 복구)
    - 반대 50% 초과: '이견 많음' 표시(자동 승인 중지, 관리자 우선 검토) — 삭제하지 않음
    - 자동 승인: 검증 중(verifying)이고 모든 조건 충족, 안전 관련 아님
    """
    status = tip.get("status")
    confirm, dispute = int(tip.get("confirm", 0)), int(tip.get("dispute", 0))
    total = confirm + dispute
    out: dict[str, Any] = {}
    flag_nets = len([n for n, c in (tip.get("flag_nets") or {}).items() if c])
    if int(tip.get("flags", 0)) >= FLAGS_TO_HIDE and status in (
        "verifying",
        "student_approved",
        "approved",
    ):
        if flag_nets >= FLAG_NETS_TO_HIDE:
            return {"status": "hidden", "hidden_from": status, "hidden_reason": "학생 신고 누적"}
        if not tip.get("flag_review"):
            out["flag_review"] = True  # 신고가 한두 네트워크에 몰림 → 가리지 않고 관리자 우선 검토
    # 반대 과반이어도 반대표가 여러 네트워크(3곳 이상, 한 곳이 절반 이하)에서 왔을 때만 '이견 많음'.
    # 한두 네트워크의 반대 몰표는 표시를 붙이지 않고 몰표 의심으로 관리자에게(GPT5 #733-1)
    dnets = [c for c in (tip.get("dispute_nets") or {}).values() if c]
    diverse = (
        len(dnets) >= FLAG_NETS_TO_HIDE and dispute > 0 and max(dnets) / dispute <= MAX_NET_SHARE
    )
    majority = total >= MIN_VOTES and dispute / total > CONTEST_RATIO
    contested = majority and diverse
    if contested != bool(tip.get("contested")):
        out["contested"] = contested
    if majority and not diverse and not tip.get("burst_risk"):
        out["burst_risk"] = True
    if (
        status != "verifying"
        or contested
        or tip.get("safety")
        or tip.get("burst_risk")
        or out.get("burst_risk")
        or int(tip.get("flags", 0)) > 0
    ):
        # 미해결 신고가 있으면 자동 승인하지 않는다(관리자가 신고를 소진한 뒤에만)
        return out
    published = tip.get("published_at")
    if not isinstance(published, datetime) or now - published < timedelta(hours=MIN_PUBLIC_HOURS):
        return out
    if total < MIN_VOTES or confirm / total < MIN_CONFIRM_RATIO:
        return out
    nets = tip.get("net_counts") or {}
    if nets and max(nets.values()) / total > MAX_NET_SHARE:
        if not tip.get("burst_risk"):
            out["burst_risk"] = True  # 몰표 위험 → 관리자 확인
        return out
    return {**out, "status": "student_approved", "approved_at": now, "approved_by": "students"}


FRESH_DAYS = 30  # 학생 확인 꿀팁: 승인 또는 최근 '맞아요'가 30일 안이어야 챗봇이 인용(GPT5 #717-2)
ADMIN_FRESH_DAYS = 180


FRESH_CONFIRMS = 2  # 최근 30일 서로 다른 유효 '맞아요' 표 2개 이상이면 학생 확인 꿀팁 인용 연장


def chat_eligible(tip: dict[str, Any], now: datetime) -> bool:
    """챗봇 인용 자격: 승인 상태 + 신선도. 페이지에는 남기되 낡으면 답변에서만 뺀다.

    - 관리자 승인: 승인 후 180일. 학생 표로는 연장하지 않는다(GPT5 #722-4).
    - 학생 확인: 승인 후 30일, 이후엔 '현재 유효한' 맞아요 표 중 최근 30일 것이 2개 이상일 때.
      철회된 표는 confirm_at에서 빠지므로 잠깐 눌렀다 바꾸는 것으로는 연장되지 않는다.
    """
    status = tip.get("status")
    approved = tip.get("approved_at")
    if status == "approved":
        return isinstance(approved, datetime) and now - approved <= timedelta(days=ADMIN_FRESH_DAYS)
    if status != "student_approved":
        return False
    window = timedelta(days=FRESH_DAYS)
    if isinstance(approved, datetime) and now - approved <= window:
        return True
    recent = [
        t
        for t in (tip.get("confirm_at") or {}).values()
        if isinstance(t, datetime) and now - t <= window
    ]
    return len(recent) >= FRESH_CONFIRMS


def reconcile_tips(rows: list[dict[str, Any]], now: datetime) -> dict[str, dict[str, Any]]:
    """매일 수집 Job에서 호출: 새 투표가 없어도 24시간이 지난 검증 중 꿀팁을 판정한다."""
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("type") == "tip" and row.get("status") == "verifying":
            change = evaluate_tip(row, now)
            if change:
                out[row["id"]] = change
    return out


def public_badge(status: str) -> str:
    return {
        "approved": "관리자 확인",
        "student_approved": "학생 확인",
        "verifying": "검증 중",
    }.get(status, "")
