"""공통 계약 단일 원본(상세설계 00 공통 규약). 변경은 게시판 합의 후 소유자(Opus5)가 반영한다."""

from backend.domain.answer import (
    Answer,
    Dept,
    Draft,
    DraftSentence,
    Fallback,
    Resolution,
    ReviewFlag,
    VerifyReport,
)
from backend.domain.errors import ApiError, AppError, StreamError
from backend.domain.evidence import Evidence, EvidenceCard, ToolResult
from backend.domain.thread import Correction, LastTurn, PendingQuestion, Profile, ThreadDoc

SCHEMA_VERSION = 1

__all__ = [
    "SCHEMA_VERSION",
    "Answer",
    "ApiError",
    "AppError",
    "Correction",
    "Dept",
    "Draft",
    "DraftSentence",
    "Evidence",
    "EvidenceCard",
    "Fallback",
    "LastTurn",
    "PendingQuestion",
    "Profile",
    "Resolution",
    "ReviewFlag",
    "StreamError",
    "ThreadDoc",
    "ToolResult",
    "VerifyReport",
]
