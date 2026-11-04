"""Offline tests only: upstream is httpx.MockTransport, never DeepSeek."""
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

import app


def reviews(count=50, start=1):
    return [{
        "batch_item_id": f"comment:{i}",
        "source_comment_id": str(i),
        "content": f"豆包评价 {i}: 回答经常出错，请改进第{i}项",
        "rating": 3,
        "review_date": "2026-10-31",
        "source": "应用宝",
        "app_version": "1.0.0",
    } for i in range(start, start + count)]


def batch_body(stage, rows, prompt_suffix=""):
    return {
        "model": "deepseek-flash",
        "messages": [
            {"role": "system", "content": f"fixed {stage} system instructions {prompt_suffix}"},
            {"role": "user", "content": "处理这些评论：\n<reviews>\n" +
             json.dumps(rows, ensure_ascii=False).replace("<", "\\u003c") +
             "\n</reviews>"},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 6000,
    }


def issue():
    return {
        "category": "AI效果问题", "feature": "AI对话", "sentiment": "negative",
        "severity": 3, "user_need": "回答准确", "summary": "回答有误", "confidence": 0.9,
    }


def fake_upstream(calls):
    def respond(request):
        body = json.loads(request.content)
        stage = body["messages"][0]["content"]
        rows = app.extract_tagged_json(app.get_user_message(body), "reviews")
        assert isinstance(rows, list) and rows
        calls.append({"stage": stage, "rows": rows})
        if "quality-batch" in stage:
            content = {"results": [{
                "batch_item_id": row["batch_item_id"],
                "is_valid": True,
                "invalid_reason": None,
            } for row in rows]}
        else:
            content = {"results": [{
                "batch_item_id": row["batch_item_id"], "issues": [issue()],
            } for row in rows]}
        return httpx.Response(200, json={
            "id": "mock", "model": "deepseek-flash",
            "choices": [{"index": 0, "message": {
                "role": "assistant", "content": json.dumps(content, ensure_ascii=False)
            }, "finish_reason": "stop"}],
            "usage": {"total_tokens": 20},
        })
    return respond


def make_client(tmp_path, monkeypatch, calls):
    monkeypatch.setenv("REVIEW_CACHE_ENABLED", "1")
    monkeypatch.setenv("CACHE_NAMESPACE", "review-cache-test")
    instance = app.create_app(
        db_path=tmp_path / "cache.sqlite3", api_key="fake-key",
        transport=httpx.MockTransport(fake_upstream(calls)),
    )
    return TestClient(instance)


def run_in_batches(client, stage, rows, size, suffix=""):
    out = []
    for n in range(0, len(rows), size):
        selection = rows[n:n+size]
        response = client.post(
            "/chat/completions",
            headers={"X-Cache-Stage": stage},
            json=batch_body(stage, selection, suffix),
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        parsed = app.parse_response_json(payload)
        assert app.VALIDATORS[stage](payload, batch_body(stage, selection, suffix))
        assert [x["batch_item_id"] for x in parsed["results"]] == [
            x["batch_item_id"] for x in selection
        ]
        out.append(response)
    return out


def test_fifty_original_reordered_and_five_new(tmp_path, monkeypatch):
    calls = []
    with make_client(tmp_path, monkeypatch, calls) as client:
        assert client.get("/health").json()["review_cache_enabled"] is True
        first = reviews()
        for stage, size in [("quality-batch", 3), ("issue-batch", 2)]:
            calls.clear()
            batch = run_in_batches(client, stage, first, size)
            assert len(calls) == (50 + size - 1) // size
            assert all(r.headers["X-AI-Cache"] == "MISS" for r in batch)
            calls.clear()
            repeated = run_in_batches(client, stage, first, size)
            assert not calls
            assert all(r.headers["X-AI-Cache"] == "HIT" for r in repeated)
            calls.clear()
            shuffled = first[1:] + first[:1]
            reordered = run_in_batches(client, stage, shuffled, size)
            assert not calls, "new batch grouping must not cause DeepSeek calls"
            assert all(r.headers["X-AI-Cache"] == "HIT" for r in reordered)
            calls.clear()
            mixed = reviews(5, start=51) + shuffled
            updated = run_in_batches(client, stage, mixed, size)
            sent_ids = [str(row["source_comment_id"]) for call in calls for row in call["rows"]]
            assert sorted(sent_ids) == ["51", "52", "53", "54", "55"]
            assert len(calls) <= 5
            assert all(r.headers["X-AI-Cache"] in ("HIT", "MISS", "PARTIAL") for r in updated)

    with sqlite3.connect(tmp_path / "cache.sqlite3") as connection:
        per_stage = dict(connection.execute(
            "SELECT stage, count(*) FROM ai_review_cache GROUP BY stage"
        ))
    assert per_stage == {"quality-batch": 55, "issue-batch": 55}


def test_one_old_two_new_partial_and_all_hit(tmp_path, monkeypatch):
    calls = []
    with make_client(tmp_path, monkeypatch, calls) as client:
        existing = reviews(1)
        new = reviews(2, start=2)
        run_in_batches(client, "quality-batch", existing, 3)
        calls.clear()
        result = run_in_batches(client, "quality-batch", [existing[0], *new], 3)
        assert result[0].headers["X-AI-Cache"] == "PARTIAL"
        assert len(calls) == 1
        assert [x["source_comment_id"] for x in calls[0]["rows"]] == ["2", "3"]
        calls.clear()
        again = run_in_batches(client, "quality-batch", [new[1], existing[0], new[0]], 3)
        assert again[0].headers["X-AI-Cache"] == "HIT"
        assert not calls


def test_review_and_prompt_changes_invalidate_individual_cache(tmp_path, monkeypatch):
    calls = []
    with make_client(tmp_path, monkeypatch, calls) as client:
        rows = reviews(2)
        run_in_batches(client, "quality-batch", rows, 3)
        calls.clear()
        edited = [dict(rows[0]), rows[1]]
        edited[0]["content"] = "修改过的评论，必须重新分析"
        result = run_in_batches(client, "quality-batch", edited, 3)
        assert result[0].headers["X-AI-Cache"] == "PARTIAL"
        assert [x["source_comment_id"] for x in calls[0]["rows"]] == ["1"]
        calls.clear()
        updated_prompt = run_in_batches(client, "quality-batch", rows, 3, "v2")
        assert len(calls) == 1
        assert sorted(x["source_comment_id"] for x in calls[0]["rows"]) == ["1", "2"]
        assert updated_prompt[0].headers["X-AI-Cache"] == "MISS"


def test_unstable_generated_ids_are_not_review_cached(tmp_path, monkeypatch):
    calls = []
    with make_client(tmp_path, monkeypatch, calls) as client:
        rows = reviews(2)
        rows[0]["source_comment_id"] = None
        rows[0]["batch_item_id"] = "row:1"
        run_in_batches(client, "quality-batch", rows, 3)
        calls.clear()
        # Batch grouping changes; row:1 cannot be trusted as a stable identity.
        rows = rows[::-1]
        response = run_in_batches(client, "quality-batch", rows, 3)
        assert response[0].headers["X-AI-Cache"] == "PARTIAL"
        assert len(calls) == 1
        assert calls[0]["rows"][0]["batch_item_id"] == "row:1"


def test_legacy_whole_batch_hit_populates_review_cache(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("REVIEW_CACHE_ENABLED", "0")
    monkeypatch.setenv("CACHE_NAMESPACE", "review-cache-test")
    db = tmp_path / "cache.sqlite3"
    rows = reviews(3)
    instance = app.create_app(
        db_path=db, api_key="fake-key", transport=httpx.MockTransport(fake_upstream(calls))
    )
    with TestClient(instance) as client:
        run_in_batches(client, "quality-batch", rows, 3)
        assert len(calls) == 1
    monkeypatch.setenv("REVIEW_CACHE_ENABLED", "1")
    with make_client(tmp_path, monkeypatch, calls) as client:
        original = run_in_batches(client, "quality-batch", rows, 3)
        assert original[0].headers["X-AI-Cache"] == "HIT"
        assert len(calls) == 1
        modified_order = rows[1:] + rows[:1]
        new_result = run_in_batches(client, "quality-batch", modified_order, 3)
        assert new_result[0].headers["X-AI-Cache"] == "HIT"
        assert len(calls) == 1


def test_invalid_model_response_is_not_cached(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("REVIEW_CACHE_ENABLED", "1")
    def broken(request):
        calls.append(request)
        return httpx.Response(200, json={
            "model": "deepseek-flash",
            "choices": [{"finish_reason": "stop", "message": {
                "content": '{"results": []}'
            }}],
        })
    client = TestClient(app.create_app(
        db_path=tmp_path / "cache.sqlite3", api_key="fake-key",
        transport=httpx.MockTransport(broken),
    ))
    with client:
        response = client.post(
            "/chat/completions", headers={"X-Cache-Stage": "quality-batch"},
            json=batch_body("quality-batch", reviews(2)),
        )
        assert response.status_code == 502
    with sqlite3.connect(tmp_path / "cache.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM ai_cache").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM ai_review_cache").fetchone()[0] == 0
