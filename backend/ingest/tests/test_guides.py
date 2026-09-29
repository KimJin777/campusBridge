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
