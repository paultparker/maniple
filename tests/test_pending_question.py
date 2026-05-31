"""Tests for AskUserQuestion pending-question detection and answer validation."""

import json
from pathlib import Path

from maniple_mcp.session_state import find_pending_question, validate_answer_index


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _ask(tool_use_id: str, *, multi=False, questions=None) -> dict:
    """An assistant message that calls AskUserQuestion."""
    qs = questions or [
        {
            "question": "Which logging level should this project use?",
            "header": "Log Level",
            "multiSelect": multi,
            "options": [
                {"label": "INFO", "description": "Standard verbosity."},
                {"label": "DEBUG", "description": "Verbose diagnostics."},
            ],
        }
    ]
    return {
        "type": "assistant",
        "uuid": "a1",
        "parentUuid": None,
        "timestamp": "2026-05-31T18:00:00Z",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": tool_use_id, "name": "AskUserQuestion",
                 "input": {"questions": qs}},
            ],
        },
    }


def _answer(tool_use_id: str) -> dict:
    """A user message carrying the tool_result that answers the question."""
    return {
        "type": "user",
        "uuid": "u2",
        "parentUuid": "a1",
        "timestamp": "2026-05-31T18:01:00Z",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id, "content": "INFO"},
            ],
        },
    }


def test_detects_pending_single_select(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [_ask("toolu_1")])
    q = find_pending_question(p)
    assert q is not None
    assert q["tool_use_id"] == "toolu_1"
    assert q["question"].startswith("Which logging level")
    assert q["header"] == "Log Level"
    assert q["multiSelect"] is False
    assert [o["label"] for o in q["options"]] == ["INFO", "DEBUG"]
    assert q["answerable"] is True
    assert q["reason"] is None


def test_answered_question_is_not_pending(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [_ask("toolu_1"), _answer("toolu_1")])
    assert find_pending_question(p) is None


def test_multiselect_is_pending_but_not_answerable(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [_ask("toolu_1", multi=True)])
    q = find_pending_question(p)
    assert q is not None
    assert q["answerable"] is False
    assert q["reason"] == "multiSelect"


def test_multi_question_is_pending_but_not_answerable(tmp_path):
    p = tmp_path / "s.jsonl"
    two = [
        {"question": "Q1?", "header": "H1", "multiSelect": False,
         "options": [{"label": "A", "description": ""}]},
        {"question": "Q2?", "header": "H2", "multiSelect": False,
         "options": [{"label": "B", "description": ""}]},
    ]
    _write_jsonl(p, [_ask("toolu_1", questions=two)])
    q = find_pending_question(p)
    assert q["answerable"] is False
    assert q["reason"] == "multi_question"
    assert q["num_questions"] == 2


def test_returns_latest_unanswered_when_multiple(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [_ask("toolu_1"), _answer("toolu_1"), _ask("toolu_2")])
    q = find_pending_question(p)
    assert q["tool_use_id"] == "toolu_2"


def test_no_question_returns_none(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [{"type": "assistant", "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "hi"}]}}])
    assert find_pending_question(p) is None


def test_missing_file_returns_none(tmp_path):
    assert find_pending_question(tmp_path / "nope.jsonl") is None


def test_validate_answer_index_ok():
    q = {"answerable": True, "reason": None, "options": [{"label": "A"}, {"label": "B"}]}
    assert validate_answer_index(q, 1) is None
    assert validate_answer_index(q, 2) is None


def test_validate_answer_index_out_of_range():
    q = {"answerable": True, "reason": None, "options": [{"label": "A"}]}
    assert validate_answer_index(q, 2) is not None
    assert validate_answer_index(q, 0) is not None


def test_validate_answer_index_not_answerable():
    q = {"answerable": False, "reason": "multiSelect", "options": [{"label": "A"}]}
    assert "multiSelect" in validate_answer_index(q, 1)
