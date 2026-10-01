from __future__ import annotations

import datetime as dt

from backend.ingest.rules import (
    ParseMode,
    chunk_article,
    parse_addenda_date,
    split_articles,
    validate_articles,
)


def test_article_heads_body_nested_title_and_metadata() -> None:
    text = """
문서 표제
제1장 총칙
제1절 목적
제 1 조 (목적) 이 법인은 목적을 달성한다.
제12조의2(타대학 취득학점(교환학생) 인정) 교환학생 학점을 인정할 수 있다.
"""

    articles = split_articles(text, rule_no="101")

    assert [article.article_id for article in articles] == ["101_main_1", "101_main_12_2"]
    assert articles[0].body == "이 법인은 목적을 달성한다."
    assert articles[0].chapter == "제1장 총칙"
    assert articles[0].section == "제1절 목적"
    assert articles[1].title == "타대학 취득학점(교환학생) 인정"


def test_deleted_notes_and_table_are_not_confused() -> None:
    text = """
제1조(기준) <개정 2021.3.30.> 다음 표에 따른다.
<표>
[본조신설 2022.4.27.]
제2조 <삭  제>
"""

    articles = split_articles(text, rule_no="101")

    assert articles[0].notes == ["<개정 2021.3.30.>", "[본조신설 2022.4.27.]"]
    assert articles[0].has_table is True
    assert "[표 — 원문 참조]" in articles[0].body
    assert articles[1].deleted is True
    assert articles[1].indexable is False
    assert validate_articles(articles).missing_main_numbers == []


def test_plain_deleted_marker_is_not_indexed() -> None:
    articles = split_articles(
        "제1조(목적) 본문\n제2조 삭제 <2026.2.25.>",
        rule_no="101",
    )

    assert articles[1].deleted is True
    assert articles[1].body == "<2026.2.25.>"
    assert articles[1].indexable is False


def test_addenda_dates_ids_and_appendix_exclusion() -> None:
    text = """
제1조(본문) 본문이다.
부   칙
이 규정은 2025년 3월 1일부터 시행한다.
제1조(시행일) 시행한다.
(별표 1)
<표>
부칙 (68. 1. 23 문교대 승인)
본문만 있는 부칙이다.
"""

    articles = split_articles(text, rule_no="101")

    assert [article.article_id for article in articles] == [
        "101_main_1",
        "101_add_s01_0",
        "101_add_s01_1",
        "101_add_s02_0",
    ]
    assert articles[1].addenda_date == dt.date(2025, 3, 1)
    assert articles[2].addenda_date == dt.date(2025, 3, 1)
    assert articles[3].addenda_date == dt.date(1968, 1, 23)
    assert all("[표" not in article.body for article in articles)  # 표를 못 읽은 별표는 내지 않음


def test_table_appendix_is_indexed_with_title_and_rows() -> None:
    """별표는 독립 청크로(교수님 2026-10-01 #883). 별지(서식)는 건너뛴다."""
    text = """
제4조(입학정원) 입학정원은 별표 1과 같다.
부   칙
이 학칙은 2026년 3월 1일부터 시행한다.
[별표 1] <개정 2026.6.12.>
2027학년도 입학정원
[표] 단과대학 | 학과(부) | 입학정원
[표] 단과대학: 경영대학 · 학과(부): 경영학과 · 입학정원: 80
[별표 1-1]
계약학과의 편성
[표] 학과(부): 기계융합공학과 · 입학정원: 20
[별지 제1호서식]
신청서 양식 칸
[별표 1]
옛 입학정원
[표] 학과(부): 경영학과 · 입학정원: 70
"""

    articles = split_articles(text, rule_no="29")
    app = [a for a in articles if a.mode is ParseMode.APPENDIX]

    assert [a.article_id for a in app] == ["29_app_1", "29_app_1_1", "29_app_1_v2"]
    assert app[0].title == "2027학년도 입학정원"
    assert "입학정원: 80" in app[0].body and app[0].has_table
    assert all("신청서 양식" not in a.body for a in articles)
    assert not validate_articles(articles).errors


def test_long_appendix_is_chunked_on_row_boundaries() -> None:
    rows = [f"[표] 학과(부): 학과{i:03d} · 입학정원: {i}" for i in range(200)]
    text = "제1조(목적) 목적.\n[별표 1]\n입학정원\n" + "\n".join(rows)
    chunks = chunk_article(split_articles(text, rule_no="29")[-1])

    assert len(chunks) > 1
    assert all(c.body_lines[0] == "[별표 1] 입학정원" for c in chunks)
    assert all(line.startswith("[표] ") for c in chunks for line in c.body_lines[1:])
    assert [c.article_id for c in chunks][:2] == ["29_app_1_c1", "29_app_1_c2"]


def test_date_parser_is_tolerant_but_rejects_invalid_dates() -> None:
    assert parse_addenda_date("1998.  4. 25") == dt.date(1998, 4, 25)
    assert parse_addenda_date("2026. 8. 20. 2026학년도") == dt.date(2026, 8, 20)
    assert parse_addenda_date("49. 1. 2") == dt.date(2049, 1, 2)
    assert parse_addenda_date("2025. 13. 1") is None
    assert parse_addenda_date("날짜 없음") is None


def test_addenda_heading_can_touch_parenthesis() -> None:
    articles = split_articles("제1조 본문\n부   칙(68. 1. 23 승인)\n시행한다.", rule_no="101")
    assert articles[-1].article_id == "101_add_s01_0"
    assert articles[-1].addenda_date == dt.date(1968, 1, 23)


def test_long_article_splits_by_paragraph_then_by_character() -> None:
    paragraph_text = "제1조(긴 조문) " + "\n".join(["① " + "가" * 1_100, "② " + "나" * 1_100])
    article = split_articles(paragraph_text, rule_no="101")[0]
    chunks = chunk_article(article)
    assert [chunk.article_id for chunk in chunks] == ["101_main_1_p1", "101_main_1_p2"]
    assert all(chunk.body.startswith("제1조(긴 조문)") for chunk in chunks)

    plain = split_articles("제2조(장문) " + "다" * 3_100, rule_no="101")[0]
    plain_chunks = chunk_article(plain)
    assert [chunk.article_id for chunk in plain_chunks] == [
        "101_main_2_c1",
        "101_main_2_c2",
        "101_main_2_c3",
    ]


def test_validation_blocks_duplicates_and_large_count_drop() -> None:
    articles = split_articles("제1조(목적) 본문", rule_no="101")
    duplicate = [articles[0], articles[0]]

    duplicate_report = validate_articles(duplicate)
    assert duplicate_report.can_update_index is False
    assert duplicate_report.duplicate_ids == ["101_main_1"]

    drop_report = validate_articles(articles, previous_article_count=3)
    assert drop_report.can_update_index is False
    assert "article count decreased by more than 50%" in drop_report.errors


def test_two_digit_year_boundary() -> None:
    assert parse_addenda_date("50. 1. 1") == dt.date(1950, 1, 1)
    assert parse_addenda_date("00. 1. 1") == dt.date(2000, 1, 1)


def test_addenda_without_number_gets_zero_id() -> None:
    articles = split_articles("부칙\n이 규정은 공포한 날부터 시행한다.", rule_no="101")
    assert len(articles) == 1
    assert articles[0].mode is ParseMode.ADDENDA
    assert articles[0].article_id == "101_add_s01_0"


def test_empty_addenda_preamble_is_not_emitted_before_numbered_article() -> None:
    articles = split_articles("부칙(2025. 1. 1.)\n제1조(시행일) 시행한다.", rule_no="101")
    assert [article.article_id for article in articles] == ["101_add_s01_1"]


def test_tables_are_filled_in_order_with_merged_cells_expanded():
    """표는 '[표] 칸 | 칸'으로 본문에 들어간다(교수님 2026-09-30: 학기당 최대 수강학점)."""
    from bs4 import BeautifulSoup

    from backend.ingest.registry import is_excluded_rule
    from backend.ingest.rules import fill_tables, render_table

    html = (
        "<table><tr><td rowspan='2'>구분</td><td colspan='2'>총졸업소요이수학점</td></tr>"
        "<tr><td>120학점</td><td>130학점</td></tr>"
        "<tr><td>최대 수강신청학점</td><td>18학점</td><td>19학점</td></tr></table>"
    )
    rows = render_table(BeautifulSoup(html, "html.parser").table)
    # 첫 행 가로 병합 → 머리글 2행(상위 > 하위), 데이터 행마다 머리글을 붙인다(#883)
    assert rows == [
        "[표] 구분 | 총졸업소요이수학점 > 120학점 | 총졸업소요이수학점 > 130학점",
        "[표] 구분: 최대 수강신청학점 · 총졸업소요이수학점 > 120학점: 18학점"
        " · 총졸업소요이수학점 > 130학점: 19학점",
    ]
    text, ok = fill_tables("제30조(학점)\n① 다음과 같다.\n<표>\n② 끝", [rows])
    assert ok and "120학점: 18학점" in text
    assert fill_tables("<표> <표>", [rows]) == ("<표> <표>", False)  # 개수가 다르면 그대로
    assert is_excluded_rule("대학원 학칙 시행규정") and not is_excluded_rule("학사운영 규정")


def test_appendix_chunks_follow_year_subheadings() -> None:
    """한 별표에 학년도별 표가 여러 벌이면 청크 머리가 그 학년도를 따른다(#898)."""
    rows = [f"[표] 학과(부): 학과{i:03d} · 입학정원: {i}" for i in range(60)]
    text = "\n".join(
        [
            "제1조(목적) 목적.",
            "[별표 1]",
            "2027학년도 입학정원",
            *rows,
            "※ 주석은 소제목이 아님",
            "2026학년도 입학정원",
            *rows,
        ]
    )
    chunks = chunk_article(split_articles(text, rule_no="29")[-1])
    heads = [c.body_lines[0] for c in chunks]

    assert heads[0] == "[별표 1] 2027학년도 입학정원"
    assert heads[-1] == "[별표 1] 2026학년도 입학정원"
    assert all(h.endswith(("2027학년도 입학정원", "2026학년도 입학정원")) for h in heads)
    assert not any(
        "2026학년도" in c.body for c in chunks if c.body_lines[0].endswith("2027학년도 입학정원")
    )
    assert any("※ 주석은 소제목이 아님" in c.body for c in chunks)


def test_appendix_chunk_title_follows_subheading() -> None:
    rows = [f"[표] 학과(부): 학과{i:03d} · 입학정원: {i}" for i in range(60)]
    text = "\n".join(
        [
            "제1조(목적) 목적.",
            "[별표 1]",
            "2027학년도 입학정원",
            *rows,
            "2026학년도 입학정원",
            *rows,
        ]
    )
    chunks = chunk_article(split_articles(text, rule_no="29")[-1])
    assert chunks[0].title == "2027학년도 입학정원" and chunks[-1].title == "2026학년도 입학정원"
