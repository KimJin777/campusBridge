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
    assert all("[표" not in article.body for article in articles)


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
    paragraph_text = "제1조(긴 조문) " + "\n".join(
        ["① " + "가" * 1_100, "② " + "나" * 1_100]
    )
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

