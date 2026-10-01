"""HWP conversion and deterministic regulation article parsing.

This module deliberately contains no network access. Downloading allowlisted HWP
files and importing prepared documents are separate pipeline stages.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from collections.abc import Iterable, Sequence
from enum import StrEnum
from pathlib import Path

ARTICLE_HEAD = re.compile(r"^제\s*(\d+)\s*조(?:\s*의\s*(\d+))?\s*")
CHAPTER_HEAD = re.compile(r"^제\s*\d+\s*장(?:\s|$)")
SECTION_HEAD = re.compile(r"^제\s*\d+\s*절(?:\s|$)")
ADDENDA_HEAD = re.compile(r"^부\s*칙")
APPENDIX_HEAD = re.compile(r"^[\(\[]?별(?:표|지)\s*\d*")
# 별표(표) 머리: "[별표 2]", "(별표 1-1)", "별표 3" — 번호와 가지 번호. 별지(서식)는 색인하지 않는다
TABLE_APPENDIX_HEAD = re.compile(r"^[\(\[]?\s*별\s*표\s*(\d+)(?:\s*[-의]\s*(\d+))?")
DELETED = re.compile(r"(?:<\s*삭\s*제\s*>|^\s*삭\s*제(?=\s|<|$))")
INLINE_NOTE = re.compile(r"<\s*(?:개정|본조신설|전문개정|제목개정)[^>]*>")
NOTE_LINE = re.compile(r"^[\[<]\s*(?:본조신설|종전|전문개정|제목개정|개정)[^\]>]*[\]>]$")
DOT_DATE = re.compile(r"(?<!\d)(\d{2,4})\s*\.\s*(\d{1,2})\s*\.\s*(\d{1,2})(?:\s*\.)?")
KOREAN_DATE = re.compile(r"(?<!\d)(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일")
TABLE_PREFIX = "[표] "  # 표 행 머리(조문 제목 정규식에 걸리지 않게)
TABLE_TOKEN = re.compile(r"<\s*표\s*>")
PARAGRAPH_HEAD = re.compile(r"^([①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳])")

_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
_CIRCLED_TO_NUMBER = {char: index for index, char in enumerate(_CIRCLED, start=1)}
_PROTECTED_CIRCLED = {char: chr(0xE000 + index) for index, char in enumerate(_CIRCLED)}
_RESTORE_CIRCLED = {value: key for key, value in _PROTECTED_CIRCLED.items()}


class ParseMode(StrEnum):
    MAIN = "main"
    ADDENDA = "addenda"
    APPENDIX = "appendix"


@dataclasses.dataclass(slots=True)
class Article:
    rule_no: str
    number: int
    branch: int | None = None
    title: str | None = None
    mode: ParseMode = ParseMode.MAIN
    addenda_seq: int = 0
    addenda_date: dt.date | None = None
    chapter: str | None = None
    section: str | None = None
    body_lines: list[str] = dataclasses.field(default_factory=list)
    notes: list[str] = dataclasses.field(default_factory=list)
    deleted: bool = False
    chunk_suffix: str | None = None

    @property
    def article_id(self) -> str:
        if self.mode is ParseMode.ADDENDA:
            base = f"{self.rule_no}_add_s{self.addenda_seq:02d}_{self.number}"
        elif self.mode is ParseMode.APPENDIX:
            base = f"{self.rule_no}_app_{self.number}"
            if self.branch is not None:
                base += f"_{self.branch}"
        else:
            base = f"{self.rule_no}_main_{self.number}"
            if self.branch is not None:
                base += f"_{self.branch}"
        return base + (self.chunk_suffix or "")

    @property
    def body(self) -> str:
        return "\n".join(self.body_lines).strip()

    @property
    def has_table(self) -> bool:
        return any(t in self.body for t in ("[표 — 원문 참조]", "<표>", TABLE_PREFIX))

    @property
    def indexable(self) -> bool:
        return not self.deleted and bool(self.body)


@dataclasses.dataclass(slots=True)
class ValidationReport:
    article_count: int
    indexable_count: int
    missing_main_numbers: list[int] = dataclasses.field(default_factory=list)
    empty_body_ids: list[str] = dataclasses.field(default_factory=list)
    addenda_dates_missing: int = 0
    duplicate_ids: list[str] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)
    warnings: list[str] = dataclasses.field(default_factory=list)
    hold_index_update: bool = False

    @property
    def can_update_index(self) -> bool:
        return not self.errors and not self.hold_index_update


@dataclasses.dataclass(slots=True)
class ConversionResult:
    output_path: Path
    converter: str
    text: str


def normalize_line(raw: str) -> str:
    """Normalize layout noise without destroying circled paragraph numbers."""

    protected = raw.translate(str.maketrans(_PROTECTED_CIRCLED))
    normalized = unicodedata.normalize("NFKC", protected).translate(str.maketrans(_RESTORE_CIRCLED))
    normalized = normalized.replace("\u3000", " ").replace("\ufeff", "")
    return re.sub(r"\s+", " ", normalized).strip()


def parse_addenda_date(text: str) -> dt.date | None:
    match = DOT_DATE.search(text) or KOREAN_DATE.search(text)
    if match is None:
        return None
    year, month, day = (int(part) for part in match.groups())
    if year < 100:
        year += 1900 if year >= 50 else 2000
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def _scan_title(text: str) -> tuple[str | None, str]:
    if not text.startswith("("):
        return None, text
    depth = 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[1:index].strip() or None, text[index + 1 :].strip()
    return None, text


def _strip_inline_notes(text: str) -> tuple[str, list[str]]:
    notes = [match.group(0).strip() for match in INLINE_NOTE.finditer(text)]
    return INLINE_NOTE.sub("", text).strip(), notes


def _replace_table_tokens(text: str) -> str:
    return re.sub(r"<\s*표\s*>", "[표 — 원문 참조]", text)


def split_articles(text: str | Iterable[str], *, rule_no: str) -> list[Article]:
    """Split converted regulation text into main and addenda articles."""

    lines = text.splitlines() if isinstance(text, str) else text
    result: list[Article] = []
    started = False
    mode = ParseMode.MAIN
    chapter: str | None = None
    section: str | None = None
    addenda_seq = 0
    addenda_date: dt.date | None = None
    pending_date_lines = 0
    current: Article | None = None
    appendix_seen: dict[tuple[int, int | None], int] = {}

    def finish() -> None:
        nonlocal current
        if current is None:
            return
        current.body_lines = [_replace_table_tokens(line) for line in current.body_lines]
        unreadable = current.mode is ParseMode.APPENDIX and all(
            line == "[표 — 원문 참조]" for line in current.body_lines
        )  # 표를 못 읽은 별표는 내용이 없어 색인하지 않는다
        if not unreadable and (current.deleted or current.body):
            result.append(current)
        current = None

    for raw in lines:
        line = normalize_line(raw)
        if not line:
            continue

        if CHAPTER_HEAD.match(line):
            started = True
            chapter = line
            section = None
            continue
        if SECTION_HEAD.match(line):
            started = True
            section = line
            continue

        article_match = ARTICLE_HEAD.match(line)
        if not started and article_match is None and ADDENDA_HEAD.match(line) is None:
            continue

        if ADDENDA_HEAD.match(line):
            finish()
            started = True
            mode = ParseMode.ADDENDA
            addenda_seq += 1
            addenda_date = parse_addenda_date(line)
            pending_date_lines = 0 if addenda_date else 2
            chapter = None
            section = None
            current = Article(
                rule_no=rule_no,
                number=0,
                mode=mode,
                addenda_seq=addenda_seq,
                addenda_date=addenda_date,
            )
            continue

        if pending_date_lines > 0:
            pending_date_lines -= 1
            parsed_date = parse_addenda_date(line)
            if parsed_date is not None:
                addenda_date = parsed_date
                if current is not None:
                    current.addenda_date = parsed_date
                pending_date_lines = 0

        if APPENDIX_HEAD.match(line):
            finish()
            mode = ParseMode.APPENDIX
            # 별표는 표를 읽게 된 뒤로 따로 색인(교수님 2026-10-01 #883). 별지(서식)는 건너뜀
            head = TABLE_APPENDIX_HEAD.match(line)
            if head is not None:
                number = int(head.group(1))
                branch = int(head.group(2)) if head.group(2) else None
                key = (number, branch)
                appendix_seen[key] = appendix_seen.get(key, 0) + 1
                _, notes = _strip_inline_notes(line[head.end() :])
                current = Article(
                    rule_no=rule_no,
                    number=number,
                    branch=branch,
                    mode=ParseMode.APPENDIX,
                    notes=notes,
                    chunk_suffix=(
                        f"_v{appendix_seen[key]}" if appendix_seen[key] > 1 else None
                    ),  # 같은 번호 별표가 또 나오면(신·구) ID 충돌 방지
                )
            continue
        if mode is ParseMode.APPENDIX and article_match is None:
            if current is None:
                continue  # 별지 구간
            if (
                current.title is None
                and not line.startswith(TABLE_PREFIX.strip())
                and not TABLE_TOKEN.fullmatch(line)
            ):
                current.title = _strip_inline_notes(line)[0][:100] or None
                if current.title is not None:
                    continue
            body, notes = _strip_inline_notes(line)
            current.notes.extend(notes)
            if body:
                current.body_lines.append(body)
            continue

        if article_match is not None:
            started = True
            rest = line[article_match.end() :].strip()
            title, rest = _scan_title(rest)
            finish()
            if mode is ParseMode.APPENDIX:
                mode = ParseMode.MAIN
            number = int(article_match.group(1))
            branch = int(article_match.group(2)) if article_match.group(2) else None
            deleted = DELETED.search(rest) is not None
            rest, notes = _strip_inline_notes(rest)
            if deleted:
                rest = DELETED.sub("", rest).strip()
            current = Article(
                rule_no=rule_no,
                number=number,
                branch=branch,
                title=title,
                mode=mode,
                addenda_seq=addenda_seq,
                addenda_date=addenda_date,
                chapter=chapter,
                section=section,
                notes=notes,
                deleted=deleted,
            )
            if rest:
                current.body_lines.append(rest)
            continue

        if current is None:
            continue
        if NOTE_LINE.match(line):
            current.notes.append(line)
        else:
            body, notes = _strip_inline_notes(line)
            current.notes.extend(notes)
            if body:
                current.body_lines.append(body)

    finish()
    return result


def _article_heading(article: Article) -> str:
    if article.mode is ParseMode.APPENDIX:
        branch = f"-{article.branch}" if article.branch is not None else ""
        return f"[별표 {article.number}{branch}] {article.title or ''}".strip()
    branch = f"의{article.branch}" if article.branch is not None else ""
    title = f"({article.title})" if article.title else ""
    return f"제{article.number}조{branch}{title}"


def chunk_article(
    article: Article,
    *,
    max_chars: int = 2_000,
    slice_chars: int = 1_500,
) -> list[Article]:
    """Split an oversized article at circled paragraph boundaries when possible."""

    if len(article.body) <= max_chars:
        return [article]

    heading = _article_heading(article)
    paragraphs: list[tuple[int, list[str]]] = []
    preface: list[str] = []
    for line in article.body_lines:
        match = PARAGRAPH_HEAD.match(line)
        if match:
            paragraphs.append((_CIRCLED_TO_NUMBER[match.group(1)], [line]))
        elif paragraphs:
            paragraphs[-1][1].append(line)
        else:
            preface.append(line)

    chunks: list[Article] = []
    if paragraphs:
        paragraphs[0][1][:0] = preface
        for paragraph_number, body_lines in paragraphs:
            chunks.append(
                dataclasses.replace(
                    article,
                    body_lines=[heading, *body_lines],
                    chunk_suffix=f"{article.chunk_suffix or ''}_p{paragraph_number}",
                )
            )
        return chunks

    if article.mode is ParseMode.APPENDIX:
        # 별표는 표 행 단위로 묶는다(행 중간에서 자르면 머리글·값이 갈라짐)
        groups: list[list[str]] = [[]]
        size = 0
        for line in article.body_lines:
            if groups[-1] and size + len(line) + 1 > slice_chars:
                groups.append([])
                size = 0
            groups[-1].append(line)
            size += len(line) + 1
        return [
            dataclasses.replace(
                article,
                body_lines=[heading, *lines],
                chunk_suffix=f"{article.chunk_suffix or ''}_c{index}",
            )
            for index, lines in enumerate(groups, start=1)
        ]

    body = article.body
    for index, start in enumerate(range(0, len(body), slice_chars), start=1):
        chunks.append(
            dataclasses.replace(
                article,
                body_lines=[heading, body[start : start + slice_chars]],
                chunk_suffix=f"{article.chunk_suffix or ''}_c{index}",
            )
        )
    return chunks


def validate_articles(
    articles: Sequence[Article],
    *,
    previous_article_count: int | None = None,
    converted_text_length: int | None = None,
    previous_text_length: int | None = None,
) -> ValidationReport:
    ids = [article.article_id for article in articles]
    duplicate_ids = sorted({article_id for article_id in ids if ids.count(article_id) > 1})
    main_numbers = {article.number for article in articles if article.mode is ParseMode.MAIN}
    missing = sorted(set(range(1, max(main_numbers, default=0) + 1)) - main_numbers)
    empty_ids = [
        article.article_id for article in articles if not article.deleted and not article.body
    ]
    indexable_count = sum(article.indexable for article in articles)
    report = ValidationReport(
        article_count=len(articles),
        indexable_count=indexable_count,
        missing_main_numbers=missing,
        empty_body_ids=empty_ids,
        addenda_dates_missing=sum(
            article.mode is ParseMode.ADDENDA and article.addenda_date is None
            for article in articles
        ),
        duplicate_ids=duplicate_ids,
    )

    if not articles:
        report.errors.append("no articles parsed")
    if duplicate_ids:
        report.errors.append("duplicate article ids")
    if missing:
        report.warnings.append("main article numbering has gaps")
    if empty_ids:
        report.warnings.append("non-deleted articles have empty bodies")
    non_deleted = [article for article in articles if not article.deleted]
    if non_deleted and len(empty_ids) / len(non_deleted) >= 0.10:
        report.warnings.append("empty-body ratio is at least 10%; heading text may be lost")
    if previous_article_count and len(articles) < previous_article_count * 0.5:
        report.errors.append("article count decreased by more than 50%")
    if previous_text_length and converted_text_length is not None:
        if converted_text_length >= previous_text_length * 2:
            report.warnings.append("converted text is at least twice the previous length")
            report.hold_index_update = True
    return report


_NUMERIC_CELL = re.compile(r"^[\d\s.,:~%()\-]+$")


def _header_rows(rows: list[list[str]], spans: set[tuple[int, int]]) -> int:
    """머리글 행 수(0~2). 첫 행에 숫자 칸이 있으면 머리글이 없는 표로 본다.

    첫 행에 가로 병합(colspan) 칸이 있으면 둘째 행까지 머리글(상위 > 하위)로 묶는다.
    """
    if len(rows) < 2 or len(rows[0]) < 2:
        return 0
    if any(cell and _NUMERIC_CELL.match(cell) for cell in rows[0]):
        return 0
    if any(r == 0 for r, _ in spans) and len(rows) >= 3:
        if not any(cell and _NUMERIC_CELL.match(cell) for cell in rows[1]):
            return 2
    return 1


def render_table(table) -> list[str]:
    """HTML 표 → 행마다 '[표] 머리글: 값 · 머리글: 값' 한 줄(교수님 2026-10-01 #883).

    병합 칸(rowspan·colspan)은 값을 펼쳐 채운다. 머리글을 행마다 붙여야 검색·근거 대조가
    "등급 A+ 평점 4.5"처럼 한 행만으로 뜻이 통한다. 머리글이 없는 표는 '칸 | 칸'으로 둔다.
    """
    grid: dict[tuple[int, int], str] = {}
    spans: set[tuple[int, int]] = set()  # 가로 병합으로 채운 칸
    for r, tr in enumerate(table.find_all("tr", recursive=False) or table.find_all("tr")):
        c = 0
        for cell in tr.find_all(["td", "th"], recursive=False):
            while (r, c) in grid:
                c += 1
            text = " ".join(cell.get_text(" ", strip=True).split())
            rs, cs = int(cell.get("rowspan", 1) or 1), int(cell.get("colspan", 1) or 1)
            for dr in range(rs):
                for dc in range(cs):
                    grid[(r + dr, c + dc)] = text
                    if cs > 1:
                        spans.add((r + dr, c + dc))
            c += cs
    if not grid:
        return []
    n_rows = max(r for r, _ in grid) + 1
    n_cols = max(c for _, c in grid) + 1
    rows = [[grid.get((r, c), "") for c in range(n_cols)] for r in range(n_rows)]
    rows = [row for row in rows if any(row)]
    head = _header_rows(rows, spans)
    if head == 0:
        return [TABLE_PREFIX + " | ".join(row) for row in rows]
    labels = []
    for c in range(n_cols):
        parts: list[str] = []
        for r in range(head):
            if rows[r][c] and rows[r][c] not in parts:
                parts.append(rows[r][c])
        labels.append(" > ".join(parts))
    out = [TABLE_PREFIX + " | ".join(labels)]
    for row in rows[head:]:
        pairs: list[str] = []
        for label, value in zip(labels, row, strict=True):
            if not value:
                continue
            pair = f"{label}: {value}" if label and label != value else value
            if not pairs or pairs[-1] != pair:  # 가로 병합으로 같은 값이 이어지면 한 번만
                pairs.append(pair)
        if pairs:
            out.append(TABLE_PREFIX + " · ".join(pairs))
    return out


def hwp_tables(source: Path, *, command: str = "hwp5html") -> list[list[str]] | None:
    """HWP 속 표들을 문서 순서대로(바깥 표만). 변환 실패면 None."""
    from bs4 import BeautifulSoup

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    with tempfile.TemporaryDirectory(prefix="campusbridge-hwphtml-") as temp_dir:
        out = Path(temp_dir, "doc.html")
        done = subprocess.run(
            [command, "--html", "--output", str(out), str(source)],
            check=False,
            capture_output=True,
            env=env,
        )
        if done.returncode != 0 or not out.exists():
            return None
        soup = BeautifulSoup(out.read_text(encoding="utf-8", errors="replace"), "html.parser")
    tables = [t for t in soup.find_all("table") if t.find_parent("table") is None]
    return [render_table(t) for t in tables]


def fill_tables(text: str, tables: list[list[str]] | None) -> tuple[str, bool]:
    """hwp5txt의 '<표>' 자리에 표 내용을 순서대로 넣는다. 개수가 다르면 손대지 않는다."""
    tokens = TABLE_TOKEN.findall(text)
    if not tokens or tables is None or len(tokens) != len(tables):
        return text, False
    it = iter(tables)
    return TABLE_TOKEN.sub(lambda _m: "\n".join(next(it)) or "<표>", text), True


def convert_hwp(
    source: Path,
    *,
    output_dir: Path,
    output_stem: str | None = None,
    hwp5txt_command: str = "hwp5txt",
    allow_libreoffice_fallback: bool = True,
) -> ConversionResult:
    """Convert an HWP file to UTF-8 text, with local LibreOffice fallback."""

    source = source.resolve(strict=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    first = subprocess.run(
        [hwp5txt_command, str(source)],
        check=False,
        capture_output=True,
        env=env,
    )
    text = first.stdout.decode("utf-8", errors="replace") if first.returncode == 0 else ""
    converter = "hwp5txt 0.1b15"

    if not text.strip() and allow_libreoffice_fallback:
        soffice = shutil.which("soffice") or shutil.which("libreoffice")
        if soffice:
            with tempfile.TemporaryDirectory(prefix="campusbridge-hwp-") as temp_dir:
                second = subprocess.run(
                    [
                        soffice,
                        "--headless",
                        "--convert-to",
                        "txt",
                        "--outdir",
                        temp_dir,
                        str(source),
                    ],
                    check=False,
                    capture_output=True,
                    env=env,
                )
                converted = Path(temp_dir, f"{source.stem}.txt")
                if second.returncode == 0 and converted.exists():
                    text = converted.read_text(encoding="utf-8", errors="replace")
                    converter = "LibreOffice"

    if not text.strip():
        stderr = first.stderr.decode("utf-8", errors="replace").strip()
        message = stderr or "unknown converter error"
        raise RuntimeError(f"HWP conversion produced no text: {message}")

    if TABLE_TOKEN.search(text):  # 표는 hwp5html로 읽어 본문에 넣는다(교수님 2026-09-30)
        text, filled = fill_tables(text, hwp_tables(source))
        if filled:
            converter += " + hwp5html tables"

    output = output_dir / f"{output_stem or source.stem.split('_', 1)[0]}.txt"
    output.write_text(text, encoding="utf-8", newline="\n")
    return ConversionResult(output_path=output, converter=converter, text=text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Convert and split a regulation HWP")
    parser.add_argument("source", type=Path)
    parser.add_argument("--rule-no", required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("data/interim"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    converted = convert_hwp(args.source, output_dir=args.output_dir, output_stem=args.rule_no)
    articles = split_articles(converted.text, rule_no=args.rule_no)
    report = validate_articles(articles, converted_text_length=len(converted.text))
    print(
        f"converter={converted.converter} articles={report.article_count} "
        f"indexable={report.indexable_count} can_update={report.can_update_index}"
    )
    return 0 if report.can_update_index else 1


if __name__ == "__main__":
    raise SystemExit(main())
