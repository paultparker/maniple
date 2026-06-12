"""Tests for AskUserQuestion pending-question detection and answer validation."""

import json

import maniple_mcp.session_state as ss
from maniple_mcp.session_state import find_pending_question, validate_answer_index


def _write_marker(tmp_path, monkeypatch, marker_id, payload):
    monkeypatch.setattr(ss, "PENDING_DIR", tmp_path)
    (tmp_path / f"{marker_id}.json").write_text(json.dumps(payload))


def _payload(multi=False, questions=None):
    qs = questions or [{
        "question": "Which logging level should this project use?",
        "header": "Log Level", "multiSelect": multi,
        "options": [{"label": "INFO", "description": "x"}, {"label": "DEBUG", "description": "y"}],
    }]
    return {"tool_name": "AskUserQuestion", "tool_use_id": "toolu_1",
            "tool_input": {"questions": qs}}


def test_detects_pending_single_select(tmp_path, monkeypatch):
    _write_marker(tmp_path, monkeypatch, "w1", _payload())
    q = find_pending_question("w1")
    assert q is not None and q["tool_use_id"] == "toolu_1"
    assert [o["label"] for o in q["options"]] == ["INFO", "DEBUG"]
    assert q["answerable"] is True and q["reason"] is None


def test_no_marker_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "PENDING_DIR", tmp_path)
    assert find_pending_question("nope") is None


def test_multiselect_not_answerable(tmp_path, monkeypatch):
    _write_marker(tmp_path, monkeypatch, "w1", _payload(multi=True))
    q = find_pending_question("w1")
    assert q["answerable"] is False and q["reason"] == "multiSelect"


def test_multi_question_not_answerable(tmp_path, monkeypatch):
    two = [
        {"question": "Q1?", "header": "H1", "multiSelect": False, "options": [{"label": "A", "description": ""}]},
        {"question": "Q2?", "header": "H2", "multiSelect": False, "options": [{"label": "B", "description": ""}]},
    ]
    _write_marker(tmp_path, monkeypatch, "w1", _payload(questions=two))
    q = find_pending_question("w1")
    assert q["answerable"] is False and q["reason"] == "multi_question" and q["num_questions"] == 2


def test_malformed_marker_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "PENDING_DIR", tmp_path)
    (tmp_path / "w1.json").write_text("{not json")
    assert find_pending_question("w1") is None


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


def test_worker_waiting_input_event_roundtrips(tmp_path, monkeypatch):
    import maniple.events as events
    # Both append_event and read_events_since resolve the log via get_events_path().
    monkeypatch.setattr(events, "get_events_path", lambda: tmp_path / "events.jsonl")
    ev = events.WorkerEvent(
        ts="2026-05-31T18:00:00Z",
        type="worker_waiting_input",
        worker_id="abc123",
        data={"question": "Which DB?", "tool_use_id": "toolu_9"},
    )
    events.append_event(ev)
    loaded = events.read_events_since()
    assert any(e.type == "worker_waiting_input" and e.worker_id == "abc123" for e in loaded)
