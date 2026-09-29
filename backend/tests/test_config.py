from backend.app.config import Settings


def test_defaults_match_design(monkeypatch):
    for k in (
        "GEMINI_MODEL",
        "GEMINI_FALLBACK_MODEL",
        "MAX_TOOL_CALLS",
        "HARD_DEADLINE_MS",
        "ADMIN_EMAILS",
    ):
        monkeypatch.delenv(k, raising=False)
    s = Settings()
    assert s.gemini_model == "gemini-3.5-flash"
    assert s.gemini_fallback_model == "gemini-3.5-flash-lite"
    assert (s.max_tool_calls, s.max_llm_calls, s.hard_deadline_ms) == (4, 6, 25000)
    assert s.admin_emails == frozenset()
    assert "yz.kyungnam.ac.kr" in s.allowed_hosts


def test_env_overrides_and_admin_check(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", " A@Kyungnam.ac.kr , b@kyungnam.ac.kr ,")
    monkeypatch.setenv("MAX_TOOL_CALLS", "3")
    s = Settings()
    assert s.max_tool_calls == 3
    assert s.is_admin("a@kyungnam.ac.kr") and s.is_admin("B@KYUNGNAM.AC.KR")
    assert not s.is_admin("x@gmail.com") and not s.is_admin(None)


def test_admin_emails_accept_pipe_separator(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "a@kyungnam.ac.kr|b@kyungnam.ac.kr")
    assert Settings().admin_emails == frozenset({"a@kyungnam.ac.kr", "b@kyungnam.ac.kr"})


def test_app_version_comes_from_changelog(monkeypatch):
    from backend.app.version import CHANGELOG, VERSION

    monkeypatch.delenv("APP_VERSION", raising=False)
    assert Settings().app_version == VERSION == CHANGELOG[0]["version"]
    versions = [c["version"] for c in CHANGELOG]
    assert versions == sorted(versions, key=lambda v: tuple(map(int, v.split("."))), reverse=True)
