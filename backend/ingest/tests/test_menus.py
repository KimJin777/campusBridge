from datetime import UTC, date, datetime
from pathlib import Path

from backend.ingest.menus import MenuLayout, MenuSection, collect_menus, parse_week

PDF = (Path(__file__).parent / "fixtures" / "menu_2026-09-28.pdf").read_bytes()
REF = date(2026, 9, 30)


def test_parse_week_columns_and_fallback_layout():
    days = parse_week(PDF, REF)  # AI 없이 대안 배치
    assert [d.day for d in days] == [
        date(2026, 9, 28) + (date(2026, 9, 29) - date(2026, 9, 28)) * i for i in range(5)
    ]
    mon = {s["name"]: s["items"] for s in days[0].sections}
    all_items = [i for items in mon.values() for i in items]
    assert "쉬림프콘마요브레드" in all_items and "황태설렁탕" in all_items
    assert "등심돈까스덮밥" not in all_items  # 화요일 칸 글자가 월요일로 새지 않음
    assert all("♥" not in i for i in all_items)


def test_parse_week_uses_ai_layout_only_when_it_covers_every_row():
    seen = {}

    def good(text):
        ys = [
            int(line.split("|")[0].split("=")[1])
            for line in text.splitlines()
            if line.startswith("y=")
        ]
        seen["ys"] = ys
        return MenuLayout(sections=[MenuSection(name="전체", row_ys=ys)])

    days = parse_week(PDF, REF, good)
    assert [s["name"] for s in days[0].sections] == ["전체"]

    def partial(text):
        return MenuLayout(sections=[MenuSection(name="일부", row_ys=seen["ys"][:3])])

    days = parse_week(PDF, REF, partial)  # 행이 빠지면 AI 결과를 버림
    assert "일부" not in [s["name"] for s in days[0].sections]


def test_parse_week_rejects_non_menu_pdf():
    assert parse_week(b"%PDF-1.4 not really", REF) == []


class Docs:
    def __init__(self):
        self.rows = {}

    def get(self, c, i):
        return self.rows.get((c, i))

    def merge(self, c, i, d):
        self.rows.setdefault((c, i), {}).update(d)

    def find(self, c, field, value):
        return [(i, r) for (cc, i), r in self.rows.items() if cc == c and r.get(field) == value]


def test_collect_menus_stores_days_and_skips_known_weeks():
    docs = Docs()
    weeks = [
        {"title": "9/28~10/2", "record_id": "10", "url": "u10"},
        {"title": "9/21~9/25", "record_id": "9", "url": "u9"},
        {"title": "9/14~9/18", "record_id": "8", "url": "u8"},
    ]
    downloads = []

    def download(url):
        downloads.append(url)
        return PDF

    now = datetime(2026, 9, 30, tzinfo=UTC)
    stats = collect_menus(docs, today=REF, now=now, list_weeks=lambda p: weeks, download=download)
    assert stats["weeks"] == 6 and stats["days"] > 0
    assert docs.get("campus_menus", "학생식당_2026-09-28")["sections"]
    downloads.clear()
    # 학교 식단표에서 빠진 날(예: 휴무로 삭제) — 같은 주를 다시 읽으면 unavailable
    docs.merge(
        "campus_menus", "학생식당_2026-10-03", {"source_record_id": "학생식당_10", "status": "ok"}
    )
    stats = collect_menus(docs, today=REF, now=now, list_weeks=lambda p: weeks, download=download)
    assert downloads == ["u10", "u9", "u10", "u9"]  # 상위 2주만 다시 읽음(GPT5 #667-3)
    assert docs.get("campus_menus", "학생식당_2026-10-03")["status"] == "unavailable"
    assert docs.get("campus_menus", "학생식당_2026-09-28")["status"] == "ok"
    assert stats["unavailable"] == 1


def test_collect_menus_ocr_fallback_for_scanned_pdf():
    from backend.ingest.menus import OcrDay, OcrSection, OcrWeek

    docs = Docs()
    weeks = [{"title": "9/21~9/23", "record_id": "8", "url": "u8"}]
    week = OcrWeek(
        days=[
            OcrDay(
                date="2026-09-21",
                sections=[OcrSection(name="중식 차림(정식)", items=["육개장", "♥", " "])],
            ),
            OcrDay(date="bad", sections=[OcrSection(name="x", items=["y"])]),
        ]
    )
    stats = collect_menus(
        docs,
        today=REF,
        now=datetime(2026, 9, 30, tzinfo=UTC),
        list_weeks=lambda p: weeks,
        download=lambda u: b"%PDF-1.7 scanned",
        ocr=lambda pdf, today: week,
    )
    assert stats["ocr"] == 2 and stats["failed"] == 0
    assert docs.get("campus_menus", "학생식당_2026-09-21")["sections"] == [
        {"name": "중식 차림(정식)", "items": ["육개장"]}
    ]
