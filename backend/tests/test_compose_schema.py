import pytest
from pydantic import ValidationError

from backend.agent.state import ComposeOut


def test_compose_out_rejects_uncited_sentences():
    with pytest.raises(ValidationError):
        ComposeOut.model_validate(
            {"sentences": [{"text": "x", "cite_ids": [], "supporting_quotes": []}]}
        )
    with pytest.raises(ValidationError):
        ComposeOut.model_validate({"sentences": []})


def test_compose_out_to_draft():
    out = ComposeOut.model_validate(
        {"sentences": [{"text": "a", "cite_ids": ["1"], "supporting_quotes": ["quote here"]}]}
    )
    d = out.to_draft()
    assert d.sentences[0].cite_ids == ["1"] and d.checklist == []
