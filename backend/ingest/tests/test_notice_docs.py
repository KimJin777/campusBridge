import datetime as dt

from backend.ingest.notice_docs import index_event_notices

TODAY = dt.date(2026, 9, 30)
URL = "https://www.kyungnam.ac.kr/bbs/ko/1408/{}/artclView.do"


class Docs:
    def __init__(self):
        self.rows = {}

    def merge(self, c, i, d):
        self.rows.setdefault((c, i), {}).update(d)

    def find(self, c, field, value):
        return [
            (i, dict(r)) for (cc, i), r in self.rows.items() if cc == c and r.get(field) == value
        ]


class Index:
    def __init__(self):
        self.deleted = []

    def delete(self, ids):
        self.deleted += ids
        return len(ids)


def ev(n, **kw):
    return {
        "title": f"행사{n}",
        "status": "active",
        "source_type": "notice",
        "source_url": URL.format(n),
        "start_date": "2026-10-12",
        "end_date": "2026-10-28",
        "date_label": "신청 기간",
        **kw,
    }


def run(docs, index, transcribe, imported):
    return index_event_notices(
        docs,
        index,
        today=TODAY,
        now="t",
        fetch_body=lambda u: "" if "1" in u.rsplit("/", 2)[-2] else "본문 " * 50,
        fetch_images=lambda u: [b"img"],
        transcribe=transcribe,
        import_docs=lambda d: imported.extend(d) or True,
    )


def test_poster_notice_is_transcribed_and_indexed_hidden_removed():
    """그림 공지는 글자를 옮겨 적어 색인, 숨김·대체 일정은 색인에서 뺀다(교수님 2026-09-30)."""
    docs, index, imported = Docs(), Index(), []
    docs.merge("campus_events", "e1", ev(1))  # 그림 안내문
    docs.merge("campus_events", "e2", ev(2))  # 글 본문
    docs.merge("campus_events", "cal", {**ev(3), "source_type": "calendar"})
    docs.merge(
        "campus_events",
        "old",
        {**ev(4), "status": "disabled", "vertex_ids": ["guide-notice-old-1"]},
    )
    stats = run(
        docs, index, lambda imgs: "지원자격: 2학년 이상 재학생\n접수처: 한마관 5층", imported
    )
    assert (stats["read"], stats["poster"], stats["indexed"], stats["removed"]) == (2, 1, 2, 1)
    e1 = docs.rows[("campus_events", "e1")]
    assert e1["content_source"] == "poster" and "한마관 5층" in e1["content_text"]
    body = next(d for d in imported if d["id"] == "guide-notice-e1-1")["structData"]["body"]
    assert "신청 기간 2026-10-12 ~ 2026-10-28" in body and "지원자격" in body
    assert index.deleted == ["guide-notice-old-1"]
    assert ("campus_events", "cal") in docs.rows and not docs.rows[("campus_events", "cal")].get(
        "vertex_ids"
    )
    imported.clear()
    assert run(docs, index, lambda imgs: "x", imported)["read"] == 0 and not imported  # 한 번만


def test_transcribe_failure_retries_next_run():
    docs, index, imported = Docs(), Index(), []
    docs.merge("campus_events", "e1", ev(1))
    assert run(docs, index, lambda imgs: None, imported)["failed"] == 1
    assert not docs.rows[("campus_events", "e1")].get("content_checked_at")
    assert run(docs, index, lambda imgs: "지원자격: 재학생 " * 5, imported)["indexed"] == 1
