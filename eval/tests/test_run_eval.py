from eval.run_eval import Grade, judge, metrics


def run(outcome="answer", retrieved=(("29_main_38", "article"),), cited=("29_main_38",), **kw):
    turn = {
        "outcome": outcome,
        "fallback_reason": kw.get("reason"),
        "elapsed_ms": kw.get("ms", 5000),
        "retrieved": [{"id": i, "kind": k} for i, k in retrieved],
        "cited": list(cited),
        "has_actions": kw.get("actions", False),
        "dept": kw.get("dept"),
        "verify_total": 2,
        "verify_kept": 2,
    }
    return {"turns": [turn]}


RULE = {
    "id": "x",
    "type": "rule",
    "expected_evidence_ids": {"any_of": ["29_main_38"]},
    "expected_source_kinds": ["rule"],
    "key_facts": ["a"],
}


def test_rule_item_all_criteria():
    ok = judge(RULE, run(), Grade())
    assert ok["correct"] and set(ok["criteria"]) == {
        "retrieval",
        "key_facts",
        "no_errors",
        "citation",
    }
    miss = judge(RULE, run(), Grade(missing_key_facts=["a"]))
    assert not miss["correct"] and not miss["criteria"]["key_facts"]
    wrong = judge(RULE, run(retrieved=(("99_main_1", "article"),), cited=()), Grade())
    assert not wrong["criteria"]["retrieval"] and not wrong["criteria"]["citation"]


def test_procedure_needs_actions_and_oos_needs_out_of_scope():
    proc = {**RULE, "type": "procedure", "expected_dept": "학사관리팀"}
    assert not judge(proc, run(actions=True, dept=None), Grade())["criteria"]["action"]
    assert judge(proc, run(actions=True, dept="학사관리팀"), Grade())["correct"]
    oos = {"id": "o", "type": "oos"}
    assert judge(oos, run(outcome="fallback", reason="out_of_scope", cited=()), None)["correct"]
    assert not judge(oos, run(outcome="fallback", reason="no_evidence", cited=()), None)["correct"]


def test_metrics():
    r1 = {"run": run(ms=4000), "judge": judge(RULE, run(), Grade())}
    r2 = {"run": run(ms=12000), "judge": judge(RULE, run(), Grade(missing_key_facts=["a"]))}
    m = metrics([r1, r2])
    assert m["accuracy"] == 0.5 and m["recall_at_5"] == 1.0 and m["latency_ms"]["mean"] == 8000


def test_recall_at_5_does_not_credit_citation_outside_top_five():
    retrieved = tuple((f"rule-{i}", "article") for i in range(1, 7))
    item = {
        **RULE,
        "expected_evidence_ids": {"any_of": ["rule-6"]},
    }
    result = judge(item, run(retrieved=retrieved, cited=("rule-6",)), Grade())
    assert not result["criteria"]["retrieval"]


def test_unlabeled_metrics_have_no_accuracy_and_keep_workflow_rates():
    result = {
        "labeled": False,
        "run": run(),
        "judge": {"correct": True, "criteria": {"citation": True}},
    }
    measured = metrics([result])
    assert measured["accuracy"] is None
    assert measured["labeled_items"] == 0
    assert measured["answer_with_citation_rate"] == 1.0
    assert measured["workflow_criteria_rate"] == 1.0
