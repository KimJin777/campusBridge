"""FastAPI 앱 — 학생 채팅(SSE)·피드백·추적·상태(상세설계 04). 관리자 API는 backend.admin 라우터.

실행: uv run uvicorn backend.app.main:app --port 8080
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.agent.nodes import AgentDeps
from backend.agent.prompts import PROMPT_VERSION
from backend.api.schemas import SCHEMA_VERSION, ChatRequest, FeedbackRequest, TrackRequest
from backend.app.clients import deadline_after
from backend.app.config import Settings, get_settings
from backend.app.events import month_events, today_events
from backend.domain.errors import RETRY_MESSAGE, AppError
from backend.domain.evidence import EvidenceCard
from backend.store.base import Acquire, Completion, TurnStore
from backend.store.mask import mask

log = logging.getLogger("campusbridge.api")

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
PING_SECONDS = 15.0
RATE_PER_MINUTE = 20
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Content-Type-Options": "nosniff",
    "X-Accel-Buffering": "no",
}
CSP = (
    "default-src 'self'; script-src 'self' https://dapi.kakao.com https://*.daumcdn.net; "
    # 카카오 지도 SDK의 인라인 스타일 때문에 스타일만 'unsafe-inline'(스크립트는 불허)
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' data: https://*.daumcdn.net https://*.kakao.com https://*.kakaocdn.net; "
    "connect-src 'self' https://dapi.kakao.com https://*.daumcdn.net; frame-ancestors 'none'"
)
# 관리자 화면은 Google 로그인(GIS) 스크립트·창과 공통 웹폰트를 허용한다.
ADMIN_CSP = (
    "default-src 'self'; script-src 'self' https://accounts.google.com/gsi/client; "
    "style-src 'self' https://accounts.google.com/gsi/style https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; img-src 'self' data: https:; "
    "connect-src 'self' https://accounts.google.com/gsi/; "
    "frame-src https://accounts.google.com/gsi/; frame-ancestors 'none'"
)


# ── 의존성 조립 ───────────────────────────────────────────────────────────
def load_confusables(path: Path) -> list[set[str]]:
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return []
    return [set(g) for g in data.get("protected_confusions", []) if isinstance(g, list)]


def default_deps(s: Settings) -> AgentDeps:
    """운영 조립. 도구·부서 조회는 backend.tools가 제공하면 연결한다(GPT5 소유)."""
    from backend.agent.llm import GeminiLLM

    tools: dict[str, Any] = {}
    dept_lookup = None
    try:
        import backend.tools as t

        if hasattr(t, "build_tools"):
            tools = t.build_tools(s)
        if hasattr(t, "dept_lookup"):
            dept_lookup = t.dept_lookup
    except ImportError:
        log.warning("backend.tools not available — running without tools")
    confusables = load_confusables(Path(__file__).resolve().parents[2] / "config" / "glossary.yml")
    deps = AgentDeps(
        llm=GeminiLLM(s),
        tools=tools,
        glossary_confusables=confusables,
        max_tool_calls=s.max_tool_calls,
        max_llm_calls=s.max_llm_calls,
    )
    if dept_lookup is not None:
        deps.dept_lookup = dept_lookup
    return deps


def default_store(s: Settings) -> TurnStore:
    if s.gcp_project_id:
        from backend.store.threads import FirestoreStore

        return FirestoreStore(s)
    from backend.store.memory import MemoryStore

    log.warning("GCP_PROJECT_ID not set — using in-memory store")
    return MemoryStore()


# ── SSE 유틸 ──────────────────────────────────────────────────────────────
def sse(event: str, data: dict[str, Any]) -> str:
    """한 줄 JSON(data: 줄 안에 개행 금지)."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def replay_events(turn_id: str, payload: dict[str, Any], attempt: int, version: str) -> list[str]:
    out = [
        sse(
            "meta",
            {
                "schema_version": SCHEMA_VERSION,
                "turn_id": turn_id,
                "replay": True,
                "attempt": attempt,
            },
        )
    ]
    cards = payload.get("cards") or []
    if cards:
        out.append(sse("evidence", {"items": cards}))
    outcome = payload.get("outcome", "answer")
    if payload.get(outcome) is not None:
        out.append(sse(outcome, payload[outcome]))
    out.append(
        sse("done", {"turn_id": turn_id, "outcome": outcome, "elapsed_ms": 0, "version": version})
    )
    return out


class RateLimiter:
    """IP당 분당 N회 — 인스턴스별 메모리(best-effort, 04 §7)."""

    def __init__(self, per_minute: int = RATE_PER_MINUTE):
        self.per_minute = per_minute
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, ip: str, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        q = self.hits[ip]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.per_minute:
            return False
        q.append(now)
        return True


def client_ip(request: Request) -> str:
    """실제 접속 IP(오른쪽부터 구글 프록시 제외 — XFF 첫 값 위조로 제한 우회 방지, GPT5 #746)."""
    from backend.app.netutil import request_ip

    return request_ip(request)


TRACK_PER_NET_DAY = 500  # 한 네트워크(학교 와이파이 포함)의 하루 클릭 기록 상한
TRACK_GLOBAL_DAY = 20000


async def track_quota_ok(request: Request) -> bool:
    """클릭 기록의 네트워크별·전체 하루 상한(화면 오류 수집과 같은 방식, GPT5 #756)."""
    from backend.app.config import get_settings
    from backend.app.telemetry import KST as _KST
    from backend.reports import rules
    from backend.reports.api import get_report_store

    store = get_report_store(get_settings())
    day = datetime.now(_KST).date().isoformat()
    net = rules.net_hash(await store.secret(), client_ip(request), day)
    if not await store.take_quota(f"track_{net}", TRACK_PER_NET_DAY, day):
        return False
    return await store.take_quota("track_all", TRACK_GLOBAL_DAY, day)


def log_turn(outcome: str, reason: str | None, elapsed_ms: int, version: str) -> None:
    """운영 경보용 구조화 로그 한 줄(질문·ID 없음). Cloud Logging이 jsonPayload로 읽는다."""
    print(
        json.dumps(
            {
                "severity": "INFO",
                "message": "turn_completed",
                "event": "turn_completed",
                "outcome": outcome,
                "fallback_reason": reason or "",
                "elapsed_ms": elapsed_ms,
                "app_version": version,
            }
        ),
        flush=True,
    )


# ── 턴 실행 ───────────────────────────────────────────────────────────────
@dataclass
class TurnRunner:
    graph: Any
    store: TurnStore
    settings: Settings
    deps: Any = None

    def initial_state(self, req: ChatRequest, q: str, acq: Acquire) -> dict[str, Any]:
        now = time.monotonic()
        t = acq.thread
        return {
            "thread_id": req.thread_id,
            "request_id": req.request_id,
            "query": q,
            "started": now,
            "deadline": deadline_after(self.settings.hard_deadline_ms),
            "profile": t.profile,
            "pending_question": t.pending_question,
            "clarification_count": t.clarification_count,
            "last_turn": t.last_turn,
            "evidence": [],
            "tool_calls_count": 0,
            "llm_calls_count": 0,
            "tool_failures": [],
            "review_flags": [],
            "act_used": False,
            "pending_tool_calls": [],
        }

    def completion(self, final: dict[str, Any], q: str, elapsed_ms: int) -> Completion:
        outcome = final["outcome"]
        evidence = final.get("evidence", [])
        cited: list[str] = []
        data: dict[str, Any] | None = None
        if outcome == "answer" and final.get("answer"):
            data = final["answer"].model_dump(mode="json")
            cited = final["answer"].cited
        elif outcome == "fallback" and final.get("fallback"):
            data = final["fallback"].model_dump(mode="json")
        elif outcome == "ask":
            data = final.get("ask")
        ev = {e.id: e for e in evidence}
        cards = [
            EvidenceCard.from_evidence(ev[c], "cited").model_dump(mode="json")
            for c in cited
            if c in ev
        ]
        thread_update: dict[str, Any] = {
            "profile": final.get("profile"),
            "pending_question": final.get("pending_question") if outcome == "ask" else None,
            "clarification_count": final.get("clarification_count", 0) if outcome == "ask" else 0,
            "last_intent": final.get("intent"),
        }
        if outcome == "answer" and final.get("last_turn") is not None:
            thread_update["last_turn"] = final["last_turn"]
        reason = final.get("fallback_reason") if outcome == "fallback" else None
        dept = final.get("fallback").dept if final.get("fallback") else None
        return Completion(
            outcome=outcome,
            final_payload={"outcome": outcome, outcome: data, "cards": cards},
            turn_fields={
                "intent": final.get("intent"),
                "retrieved_ids": [e.id for e in evidence],
                "cited_ids": cited,
                # 오류 제보 시 비교할 인용 원문 스냅샷(교수님 #715 — 잘못된 정보 제보)
                "cited_quotes": {c: ev[c].text[:1500] for c in cited if c in ev},
                "fallback_reason": reason,
                "elapsed_ms": elapsed_ms,
                "llm_calls": final.get("llm_calls_count", 0),
                "model": self.settings.gemini_model,
                "prompt_version": PROMPT_VERSION,
                "app_version": self.settings.app_version,
            },
            thread_update=thread_update,
            unanswered=(
                {
                    "query_masked": final.get("effective_query") or q,
                    "intent": final.get("intent"),
                    "fallback_reason": reason,
                    "suggested_dept_id": dept.dept_id if dept else None,
                }
                if reason
                else None
            ),
        )

    async def stream(self, req: ChatRequest, q: str, acq: Acquire) -> AsyncIterator[str]:
        turn_id = req.turn_id
        started = time.monotonic()
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        done_ok = False
        outcome = "error"

        async def produce() -> None:
            final: dict[str, Any] = {}
            try:
                async with asyncio.timeout(self.settings.hard_deadline_ms / 1000 + 5):
                    async for mode, chunk in self.graph.astream(
                        self.initial_state(req, q, acq), stream_mode=["custom", "values"]
                    ):
                        if mode == "custom":
                            await queue.put(("event", chunk))
                        else:
                            final = chunk
                await queue.put(("final", final))
            except TimeoutError:
                await queue.put(("error", "TIMEOUT"))
            except Exception:  # noqa: BLE001 — 스트림 시작 후 오류는 error 이벤트로
                log.exception("graph failed turn_id=%s", turn_id)
                await queue.put(("error", "INTERNAL"))

        task = asyncio.create_task(produce())
        try:
            yield sse(
                "meta",
                {
                    "schema_version": SCHEMA_VERSION,
                    "turn_id": turn_id,
                    "replay": False,
                    "attempt": acq.attempt,
                },
            )
            while True:
                try:
                    kind, item = await asyncio.wait_for(queue.get(), timeout=PING_SECONDS)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                if kind == "event":
                    yield sse(item["event"], item["data"])
                    continue
                if kind == "final" and item.get("outcome") in ("answer", "fallback", "ask"):
                    elapsed = int((time.monotonic() - started) * 1000)
                    try:
                        await self.store.complete(
                            req.thread_id, req.request_id, self.completion(item, q, elapsed)
                        )
                        done_ok, outcome = True, item["outcome"]
                        log_turn(
                            outcome,
                            item.get("fallback_reason") if outcome == "fallback" else None,
                            elapsed,
                            self.settings.app_version,
                        )
                    except Exception:  # noqa: BLE001 — 저장 실패는 재시도 가능한 오류로
                        log.exception("completion failed turn_id=%s", turn_id)
                        yield sse("error", {"code": "INTERNAL", "message": RETRY_MESSAGE})
                else:
                    code = item if kind == "error" else (item.get("error_code") or "INTERNAL")
                    code = (
                        code if code in ("LLM_UNAVAILABLE", "TIMEOUT", "INTERNAL") else "INTERNAL"
                    )
                    log_turn(
                        "error",
                        code.lower(),
                        int((time.monotonic() - started) * 1000),
                        self.settings.app_version,
                    )
                    yield sse("error", {"code": code, "message": RETRY_MESSAGE})
                elapsed = int((time.monotonic() - started) * 1000)
                yield sse(
                    "done",
                    {
                        "turn_id": turn_id,
                        "outcome": outcome,
                        "elapsed_ms": elapsed,
                        "version": self.settings.app_version,
                    },
                )
                break
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            await self.store.release(req.thread_id, req.request_id, failed=not done_ok)


# ── 심층 점검(/api/status?deep=1) ─────────────────────────────────────────
class _Ping(BaseModel):
    ok: bool


async def _timed(coro) -> dict[str, Any]:
    t = time.monotonic()
    try:
        detail = await coro
        return {"ok": True, "elapsed_ms": int((time.monotonic() - t) * 1000), **(detail or {})}
    except Exception as e:  # noqa: BLE001 — 점검 결과로 보고
        return {
            "ok": False,
            "elapsed_ms": int((time.monotonic() - t) * 1000),
            "error": type(e).__name__,
        }


async def deep_checks(r: TurnRunner) -> dict[str, Any]:
    """Gemini 1회·검색 1회·Firestore 쓰기 1회(04 §5). 결과는 구조화 필드만 반환한다."""
    deps: AgentDeps = r.deps

    async def gemini() -> dict[str, Any]:
        out = await deps.llm.structured(
            _Ping, "Return ok=true.", "ping", node="classify", deadline=deadline_after(15000)
        )
        if not out.ok:
            raise RuntimeError("unexpected")
        return {}

    async def search() -> dict[str, Any]:
        tool = deps.tools.get("search_academic_knowledge")
        if tool is None:
            raise RuntimeError("not configured")
        args = {"query": "학칙 휴학", "kinds": ["rule"]}
        res = await (tool.ainvoke(args) if hasattr(tool, "ainvoke") else tool(**args))
        if not res.ok:
            raise RuntimeError(res.error_code or "error")
        return {"count": len(res.items)}

    async def firestore() -> dict[str, Any]:
        await r.store.add_event({"thread_id": "health", "event": "health_check"})
        return {}

    names = ("gemini", "search", "firestore")
    results = await asyncio.gather(_timed(gemini()), _timed(search()), _timed(firestore()))
    return dict(zip(names, results, strict=True))


# ── 앱 ───────────────────────────────────────────────────────────────────
def create_app(
    settings: Settings | None = None,
    deps: AgentDeps | None = None,
    store: TurnStore | None = None,
) -> FastAPI:
    from backend.agent.graph import build_graph

    s = settings or get_settings()
    app = FastAPI(title="campusBridge", version=s.app_version)
    lazy: dict[str, Any] = {"deps": deps, "store": store}

    def runner() -> TurnRunner:
        if lazy.get("runner") is None:
            d = lazy["deps"] or default_deps(s)
            st = lazy["store"] or default_store(s)
            lazy["store"] = st
            lazy["runner"] = TurnRunner(graph=build_graph(d), store=st, settings=s, deps=d)
        return lazy["runner"]

    app.state.runner = runner
    limiter = RateLimiter()

    @app.exception_handler(AppError)
    async def _app_error(_: Request, e: AppError) -> JSONResponse:
        return JSONResponse(e.to_model().model_dump(), status_code=e.status)

    @app.exception_handler(RequestValidationError)
    async def _bad_request(_: Request, e: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {"code": "BAD_REQUEST", "message": "요청 형식이 올바르지 않습니다."}, status_code=400
        )

    @app.middleware("http")
    async def headers(request: Request, call_next):
        resp: Response = await call_next(request)
        path = request.url.path
        if path.startswith("/api/"):
            resp.headers.setdefault("Cache-Control", "no-store")
        elif path.endswith((".html", ".js", ".css")) or path == "/" or path.endswith("/"):
            resp.headers["Cache-Control"] = "no-cache"
        elif path.endswith((".png", ".jpg", ".svg", ".ico", ".woff2")):
            resp.headers["Cache-Control"] = "public, max-age=604800"
        if resp.headers.get("content-type", "").startswith("text/html"):
            resp.headers["Content-Security-Policy"] = (
                ADMIN_CSP if path.startswith("/admin") else CSP
            )
            resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    @app.post("/api/chat")
    async def chat(request: Request):
        try:
            body = await request.json()
        except ValueError as e:
            raise AppError("BAD_REQUEST", "요청 형식이 올바르지 않습니다.") from e
        if isinstance(body, dict) and body.get("schema_version") != SCHEMA_VERSION:
            raise AppError("SCHEMA_MISMATCH", "화면을 새로고침해 주세요.")
        try:
            req = ChatRequest.model_validate(body)
        except ValueError as e:
            raise AppError("BAD_REQUEST", "요청 형식이 올바르지 않습니다.") from e
        if not limiter.allow(client_ip(request)):
            raise AppError("RATE_LIMITED", "잠시 후 다시 시도해 주세요.")
        q = mask(req.message)  # 이후 원문 미사용
        r = runner()
        acq = await r.store.acquire(req.thread_id, req.request_id, q)
        if acq.kind == "busy":
            raise AppError(
                "THREAD_BUSY", "이전 질문을 처리하고 있습니다. 잠시 후 다시 시도해 주세요."
            )
        if acq.kind == "replay":
            events = replay_events(req.turn_id, acq.final_payload or {}, acq.attempt, s.app_version)

            async def replay() -> AsyncIterator[str]:
                for e in events:
                    yield e

            return StreamingResponse(
                replay(), media_type="text/event-stream; charset=utf-8", headers=SSE_HEADERS
            )
        return StreamingResponse(
            r.stream(req, q, acq),
            media_type="text/event-stream; charset=utf-8",
            headers=SSE_HEADERS,
        )

    @app.post("/api/feedback", status_code=204)
    async def feedback(req: FeedbackRequest) -> Response:
        await runner().store.put_feedback(
            {
                "thread_id": req.thread_id,
                "turn_id": req.turn_id,
                "rating": req.rating,
                "comment_masked": mask(req.comment) if req.comment else None,
            }
        )
        return Response(status_code=204)

    @app.post("/api/track", status_code=204)
    async def track(req: TrackRequest, request: Request) -> Response:
        try:  # fire-and-forget — 화면에는 항상 204, 저장 실패는 본문 없이 경고만(GPT5 #746)
            if await track_quota_ok(request):  # 공개 쓰기 상한(GPT5 #756) — 넘치면 조용히 버림
                await runner().store.add_event(req.model_dump(exclude_none=True))
        except Exception:  # noqa: BLE001
            log.warning("track store failed event=%s", req.event)
        return Response(status_code=204)

    @app.get("/api/status")
    async def status(
        deep: int = 0, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        base = {
            "status": "ok",
            "version": s.app_version,
            "schema_version": SCHEMA_VERSION,
            "model": s.gemini_model,
            "index": s.search_datastore_id,
            "time": datetime.now(UTC).isoformat(),
        }
        if not deep:
            return base
        # 외부 호출·쓰기가 생기므로 관리자 인증 강제(04 §5) — 인증은 backend.admin(GPT5 소유)
        from backend.admin.auth import require_admin

        await require_admin(authorization)
        checks = await deep_checks(runner())
        base["checks"] = checks
        base["status"] = "ok" if all(c["ok"] for c in checks.values()) else "degraded"
        return base

    @app.get("/api/suggestions")
    async def suggestions() -> dict[str, Any]:
        return {"items": list(s.suggestions)}

    @app.get("/api/campus/map")
    async def campus_map_api() -> dict[str, Any]:
        from backend.tools.campus_route import campus_map

        return {**campus_map(), "kakao_js_key": s.kakao_js_key or None}

    @app.get("/api/campus/route")
    async def campus_route_api(to: str = "", start: str = "") -> dict[str, Any]:
        """도보 경로(교수님이 표시한 도보길 기반). start 없으면 문장 속 'OO에서' 또는 정문.

        지도에 없는 곳이면 found=false.
        """
        from backend.tools.campus_route import resolve_place, route

        origin = resolve_place(start[:50]) if start.strip() else None
        r = route(to[:200], origin) if to.strip() else None
        return {"found": bool(r), **(r or {})}

    @app.get("/api/events/today")
    async def events_today() -> dict[str, Any]:
        return {"items": await today_events(s)}

    @app.get("/api/events/month")
    async def events_month(ym: str = "") -> dict[str, Any]:
        """학교 일정 전체보기(월 달력). ym=YYYY-MM"""
        return {"items": await month_events(s, ym[:7])}

    @app.get("/api/events/calendar.ics", include_in_schema=False)
    async def events_ics(g: str = "") -> Response:
        """학교 일정 구독(구글 캘린더 'URL로 추가'·애플 캘린더 구독). 게시된 일정만.

        g=academic,events 처럼 달력에서 켠 분류만 담는다(교수님 2026-09-30)."""
        from backend.app.events import build_ics, feed_events, parse_groups

        groups = parse_groups(g[:100])
        items = [e for e in await feed_events(s) if e.get("group") in groups]
        body = build_ics(items, datetime.now(UTC))
        return Response(
            body,
            media_type="text/calendar; charset=utf-8",
            headers={
                "Cache-Control": "public, max-age=3600",
                "Content-Disposition": 'inline; filename="campusbridge-schedule.ics"',
            },
        )

    @app.get("/calendar", include_in_schema=False)
    async def calendar_page() -> RedirectResponse:
        return RedirectResponse("/calendar.html")

    @app.get("/admin", include_in_schema=False)
    async def admin_page() -> RedirectResponse:
        return RedirectResponse("/admin.html")

    @app.get("/api/auth/config")
    async def auth_config() -> dict[str, Any]:
        """관리자 화면의 Google 로그인 버튼용 공개 클라이언트 ID(비밀 아님)."""
        return {"google_client_id": s.google_oauth_client_id}

    @app.get("/privacy", include_in_schema=False)
    async def privacy() -> RedirectResponse:
        return RedirectResponse("/privacy.html")

    from backend.reports.api import router as reports_router

    app.include_router(reports_router)  # 익명 꿀팁·오류 제보(교수님 #715)
    from backend.app.telemetry import router as telemetry_router

    app.include_router(telemetry_router)  # 화면 오류 종류 수집(운영 관측 #747)

    @app.get("/tips", include_in_schema=False)
    async def tips_page() -> RedirectResponse:
        return RedirectResponse("/tips.html")

    try:  # 관리자 API(GPT5 소유)가 router를 내보내면 등록
        from backend.admin import router as admin_router  # type: ignore[attr-defined]

        app.include_router(admin_router)
    except ImportError:
        pass

    if FRONTEND_DIR.is_dir():
        app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    return app


app = create_app()
