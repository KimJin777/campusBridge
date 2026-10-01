# ruff: noqa: E501 — HTML 픽스처 문자열
from backend.ingest.guides import build_guide_documents, extract_sections

HTML = """<html><head><title>휴학</title></head><body>
<div id="_contentBuilder"><h2 class="hidden">컨텐츠영역</h2><script>x()</script>
<h3 class="objHeading_h3">휴학처리절차</h3>
<ul><li>학생이 학과 또는 학사관리팀 방문 신청 -> 지도교수 상담 -> 접수</li></ul>
<a href="/ko/form.do">휴학원 서식</a><a href="https://evil.example/x">외부</a>
<h3 class="objHeading_h3">짧음</h3><p>짧다</p>
<h3 class="objHeading_h3">유의사항</h3><p>휴학원을 쓸 때 본인확인, 복학년도, 학기, 학년을 반드시 확인할 것 문의 055-249-2000</p>
</div></body></html>"""


def test_sections_links_and_meta():
    title, secs = extract_sections(
        HTML,
        page_id="leave",
        page_url="https://www.kyungnam.ac.kr/ko/4401/subview.do",
        allowed_hosts={"www.kyungnam.ac.kr"},
    )
    assert title == "휴학" and [s["n"] for s in secs] == [1, 3]  # 너무 짧은 절 제외
    assert "x()" not in secs[0]["body"] and "컨텐츠영역" not in secs[0]["body"]
    assert secs[0]["links"] == [
        {"text": "휴학원 서식", "url": "https://www.kyungnam.ac.kr/ko/form.do"}
    ]
    docs = build_guide_documents(
        "leave", "https://www.kyungnam.ac.kr/ko/4401/subview.do", title, secs, "t"
    )
    first, second = (d["structData"] for d in docs)
    assert (
        docs[0]["id"] == "guide-leave-1"
        and first["article_id"] == "guide:leave:1"
        and first["source_kind"] == "guide"
    )
    assert first["department"] == "학사관리팀"
    assert second["department"] == "학사관리팀"  # 절에 부서가 없으면 페이지 기준
    assert second["phone"] == "055-249-2000"
    assert "휴학원" not in str(second["department"])


def test_page_without_content_root_yields_nothing():
    assert (
        extract_sections(
            "<html><title>x</title></html>", page_id="x", page_url="u", allowed_hosts=set()
        )[1]
        == []
    )


def test_intro_before_first_heading_becomes_overview_section():
    html = (
        '<html><title>복학</title><div id="_contentBuilder">'
        "<p>복학은 휴학기간 만료 전 등록 및 복학절차를 이행해야 하며 학사관리팀에서 처리한다.</p>"
        '<h3 class="objHeading_h3">복학 절차</h3><p>학생정보시스템에서 복학을 신청한 뒤 정해진 기간 안에 등록금을 납부한다.</p>'
        "</div></html>"
    )
    _, secs = extract_sections(html, page_id="return", page_url="u", allowed_hosts=set())
    assert secs[0]["n"] == 0 and secs[0]["heading"] == "개요"
    assert "복학절차를 이행" in secs[0]["body"] and secs[1]["n"] == 1


def test_tables_become_header_value_rows():
    """홈페이지 본문 표도 '[표] 머리글: 값' 행으로(#904-4 — 주차요금 환산표)."""
    from backend.ingest.guides import extract_sections

    html = (
        "<html><head><title>주차요금안내</title></head><body><div id='_contentBuilder'>"
        "<h3 class='objHeading_h3'>주차요금 환산표</h3>"
        "<table><tr><th>시간</th><th>적용요금</th></tr>"
        "<tr><td>30분</td><td>0</td></tr><tr><td>1시간</td><td>￦1,000</td></tr></table>"
        "</div></body></html>"
    )
    _, sections = extract_sections(
        html,
        page_id="p",
        page_url="https://www.kyungnam.ac.kr/ko/4444/subview.do",
        allowed_hosts={"www.kyungnam.ac.kr"},
    )
    body = str(sections[0]["body"])
    assert "[표] 시간: 1시간 · 적용요금: ￦1,000" in body
    assert "\n30분\n" not in body


def test_attachment_links_only_school_files_in_content():
    """본문 첨부(HWP·PDF)만, 학교 도메인만(교수님 2026-10-01: 통학버스 요금 HWP)."""
    from backend.ingest.guides import attachment_links

    html = (
        "<div id='_contentBuilder'>"
        "<a href='/sites/ko/download/2026%ED%95%99%EB%85%84%EB%8F%84%20%EC%9A%94%EA%B8%88.hwp'>안내</a>"
        "<a href='https://evil.example.com/a.pdf'>외부</a><a href='/ko/4401/subview.do'>메뉴</a></div>"
        "<footer><a href='/sites/ko/download/privacy.pdf'>개인정보</a></footer>"
    )
    links = attachment_links(
        html, "https://www.kyungnam.ac.kr/ko/4319/subview.do", {"www.kyungnam.ac.kr"}
    )
    assert links == [
        (
            "https://www.kyungnam.ac.kr/sites/ko/download/2026%ED%95%99%EB%85%84%EB%8F%84%20%EC%9A%94%EA%B8%88.hwp",
            "2026학년도 요금.hwp",
        )
    ]


async def test_attachment_text_becomes_numbered_sections():
    from backend.ingest.guides import ATTACH_N0, collect_attachments

    html = "<div id='_contentBuilder'><a href='/f/요금안내.md'>x</a><a href='/f/a.txt'>y</a></div>"
    got_urls = []

    async def get_bytes(url):
        got_urls.append(url)
        return ("통학버스 요금 안내\n\n밀양 노선 요금은 3,650원입니다. " * 3).encode()

    # md·txt는 첨부 대상 확장자가 아니므로 무시 → 빈 목록
    assert (
        await collect_attachments(
            html,
            "https://www.kyungnam.ac.kr/ko/1/subview.do",
            {"www.kyungnam.ac.kr"},
            get_bytes,
            interval=0,
        )
        == []
    )
    html2 = html.replace("요금안내.md", "요금안내.pdf")

    async def pdf_bytes(url):
        got_urls.append(url)
        return b"not a pdf"  # 깨진 파일은 건너뛴다(페이지 수집은 계속)

    assert (
        await collect_attachments(
            html2,
            "https://www.kyungnam.ac.kr/ko/1/subview.do",
            {"www.kyungnam.ac.kr"},
            pdf_bytes,
            interval=0,
        )
        == []
    )
    assert ATTACH_N0 == 100 and got_urls  # 내려받기는 시도함
