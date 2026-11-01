import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

import app


def response_for(content, model="deepseek-flash"):
    return {
        "id": "mock",
        "model": model,
        "choices": [{
            "index": 0,
            "finish_reason": "stop",
            "message": {
                "role": "assistant",
                "content": json.dumps(content, ensure_ascii=False),
            },
        }],
        "usage": {"total_tokens": 10},
    }


def client_for(db, response_payload, calls):
    def fake_upstream(request):
        calls.append(request)
        return httpx.Response(200, json=response_payload)

    transport = httpx.MockTransport(fake_upstream)
    instance = app.create_app(
        db_path=db,
        api_key="fake-key",
        transport=transport,
    )
    return TestClient(instance)


QUALITY_BODY = {
    "model": "deepseek-flash",
    "messages": [
        {"role": "system", "content": "quality"},
        {"role": "user", "content": "<review>很好用</review>"},
    ],
    "response_format": {"type": "json_object"},
}

ISSUE_BODY = {
    "model": "deepseek-flash",
    "messages": [
        {"role": "system", "content": "issue"},
        {"role": "user", "content": "<review>文件打不开</review>"},
    ],
    "response_format": {"type": "json_object"},
}

QUALITY_BATCH_REVIEWS = [
    {"batch_item_id": "comment:1001", "source_comment_id": "1001", "content": "写作业挺方便"},
    {"batch_item_id": "comment:1002", "source_comment_id": "1002", "content": "文件打不开"},
]
ISSUE_BATCH_REVIEWS = [
    {"batch_item_id": "comment:1001", "source_comment_id": "1001", "content": "写作业挺方便"},
    {"batch_item_id": "comment:1002", "source_comment_id": "1002", "content": "文件打不开"},
]

def batch_body(stage_name, reviews):
    return {
        "model": "deepseek-flash",
        "messages": [
            {"role": "system", "content": stage_name},
            {
                "role": "user",
                "content": "请处理以下评论：\n<reviews>\n"
                + json.dumps(reviews, ensure_ascii=False).replace("<", "\\u003c")
                + "\n</reviews>",
            },
        ],
        "response_format": {"type": "json_object"},
    }

QUALITY_BATCH_BODY = batch_body("quality-batch", QUALITY_BATCH_REVIEWS)
ISSUE_BATCH_BODY = batch_body("issue-batch", ISSUE_BATCH_REVIEWS)

CLUSTER_BODY = {
    "model": "deepseek-flash",
    "messages": [
        {"role": "system", "content": "cluster"},
        {
            "role": "user",
            "content": (
                "请聚类\n"
                "<issues>\n"
                + json.dumps(
                    [
                        {"issue_key": "A__1", "summary": "a"},
                        {"issue_key": "B__1", "summary": "b"},
                    ],
                    ensure_ascii=False,
                )
                + "\n</issues>"
            ),
        },
    ],
    "response_format": {"type": "json_object"},
}

GLOBAL_BODY = {
    "model": "deepseek-flash",
    "messages": [
        {"role": "system", "content": "global"},
        {
            "role": "user",
            "content": (
                "<clusters>\n"
                + json.dumps(
                    [
                        {"cluster_id": "G001-C001", "cluster_name": "A"},
                        {"cluster_id": "G002-C001", "cluster_name": "B"},
                    ],
                    ensure_ascii=False,
                )
                + "\n</clusters>"
            ),
        },
    ],
    "response_format": {"type": "json_object"},
}

PROBLEM_BODY = {
    "model": "deepseek-flash",
    "messages": [
        {"role": "system", "content": "problem"},
        {
            "role": "user",
            "content": (
                "输入数据：\n\n"
                + json.dumps(
                    {"final_cluster_id": "FC001", "problem_name": "问题"},
                    ensure_ascii=False,
                )
            ),
        },
    ],
    "response_format": {"type": "json_object"},
}

POSITIVE_BODY = {
    "model": "deepseek-flash",
    "messages": [
        {"role": "system", "content": "positive"},
        {
            "role": "user",
            "content": (
                "输入数据：\n\n"
                + json.dumps(
                    {"final_cluster_id": "FC009", "experience_name": "优点"},
                    ensure_ascii=False,
                )
            ),
        },
    ],
    "response_format": {"type": "json_object"},
}

OVERALL_BODY = {
    "model": "deepseek-flash",
    "messages": [
        {"role": "system", "content": "overall"},
        {
            "role": "user",
            "content": (
                "Positive Insights:\n"
                + json.dumps(
                    [{
                        "final_cluster_id": "FC009",
                        "representative_comment": "真的很好用",
                    }],
                    ensure_ascii=False,
                )
                + "\n\n输出必须严格符合以下 JSON 结构：\n"
            ),
        },
    ],
    "response_format": {"type": "json_object"},
}


STAGE_CASES = [
    (
        "quality",
        QUALITY_BODY,
        {"is_valid": True, "invalid_reason": None},
    ),
    (
        "issue",
        ISSUE_BODY,
        {
            "issues": [{
                "category": "功能异常",
                "feature": "App基础体验",
                "sentiment": "negative",
                "severity": 4,
                "user_need": "正常打开文件",
                "summary": "文件打不开",
                "confidence": 0.95,
            }]
        },
    ),
    (
        "quality-batch",
        QUALITY_BATCH_BODY,
        {
            "results": [
                {"batch_item_id": "comment:1001", "is_valid": True, "invalid_reason": None},
                {"batch_item_id": "comment:1002", "is_valid": False, "invalid_reason": "缺少具体产品信息"},
            ]
        },
    ),
    (
        "issue-batch",
        ISSUE_BATCH_BODY,
        {
            "results": [
                {"batch_item_id": "comment:1001", "issues": [{
                    "category": "正向体验", "feature": "学习/搜题", "sentiment": "positive",
                    "severity": 1, "user_need": "获得清晰的学习解释", "summary": "学习辅导体验方便", "confidence": 0.95
                }]},
                {"batch_item_id": "comment:1002", "issues": [{
                    "category": "功能异常", "feature": "App基础体验", "sentiment": "negative",
                    "severity": 4, "user_need": "正常打开文件", "summary": "文件无法打开", "confidence": 0.92
                }]},
            ]
        },
    ),
    (
        "clustering",
        CLUSTER_BODY,
        {
            "clusters": [{
                "cluster_id": "ignored",
                "cluster_name": "文件打不开",
                "member_issue_keys": ["A__1", "B__1"],
            }]
        },
    ),
    (
        "global-consolidation",
        GLOBAL_BODY,
        {
            "final_clusters": [{
                "final_cluster_id": "FC001",
                "final_cluster_name": "文件问题",
                "source_cluster_ids": ["G001-C001", "G002-C001"],
            }]
        },
    ),
    (
        "problem-insight",
        PROBLEM_BODY,
        {
            "final_cluster_id": "FC001",
            "insight_type": "problem",
            "insight_summary": "问题总结",
            "user_signal": "用户反馈问题",
            "user_need": "用户希望正常使用",
            "product_implication": "可能影响体验",
            "pm_followup": "核查复现条件",
            "evidence_summary": "当前样本证据",
            "limitations": "样本有限",
        },
    ),
    (
        "positive-insight",
        POSITIVE_BODY,
        {
            "final_cluster_id": "FC009",
            "insight_type": "positive",
            "insight_summary": "体验很好",
            "user_signal": "用户认可",
            "user_need": "获得方便体验",
            "product_implication": "可作为正向信号",
            "pm_followup": "核查具体场景",
            "evidence_summary": "当前样本证据",
            "limitations": "样本有限",
        },
    ),
]


@pytest.mark.parametrize("stage,body,payload", STAGE_CASES)
def test_every_stage_miss_then_hit(tmp_path, stage, body, payload):
    calls = []
    with client_for(tmp_path / f"{stage}.db", response_for(payload), calls) as client:
        first = client.post(
            "/chat/completions",
            headers={"X-Cache-Stage": stage},
            json=body,
        )
        second = client.post(
            "/chat/completions",
            headers={"X-Cache-Stage": stage},
            json=body,
        )

    assert first.status_code == 200, first.text
    assert first.headers["X-AI-Cache"] == "MISS"
    assert second.status_code == 200, second.text
    assert second.headers["X-AI-Cache"] == "HIT"
    assert first.text == second.text
    assert len(calls) == 1


def test_positive_overall_bypasses_cache_and_proxy_validation(tmp_path):
    calls = []
    db = tmp_path / "positive-overall.db"
    # Intentionally not a valid report schema: the proxy must pass this
    # response through so the n8n Parse & Validate node can report the error.
    upstream_payload = response_for({"not_a_report": True})

    with client_for(db, upstream_payload, calls) as client:
        first = client.post(
            "/chat/completions",
            headers={"X-Cache-Stage": "positive-overall"},
            json=OVERALL_BODY,
        )
        second = client.post(
            "/chat/completions",
            headers={"X-Cache-Stage": "positive-overall"},
            json=OVERALL_BODY,
        )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.headers["X-AI-Cache"] == "BYPASS"
    assert second.headers["X-AI-Cache"] == "BYPASS"
    assert len(calls) == 2

    with sqlite3.connect(db) as conn:
        count = conn.execute(
            "SELECT count(*) FROM ai_cache WHERE stage = ?",
            ("positive-overall",),
        ).fetchone()[0]
    assert count == 0


def test_problem_retry_does_not_reuse_bad_cluster_id(tmp_path):
    calls = []
    good = {
        "final_cluster_id": "FC001",
        "insight_type": "problem",
        "insight_summary": "问题总结",
        "user_signal": "用户反馈问题",
        "user_need": "用户希望正常使用",
        "product_implication": "可能影响体验",
        "pm_followup": "核查复现条件",
        "evidence_summary": "当前样本证据",
        "limitations": "样本有限",
    }
    bad = dict(good)
    bad["final_cluster_id"] = "WRONG"

    responses = [bad, good]

    def fake(request):
        calls.append(request)
        return httpx.Response(200, json=response_for(responses[len(calls) - 1]))

    instance = app.create_app(
        db_path=tmp_path / "retry.db",
        api_key="fake-key",
        transport=httpx.MockTransport(fake),
    )

    with TestClient(instance) as client:
        first = client.post(
            "/chat/completions",
            headers={"X-Cache-Stage": "problem-insight"},
            json=PROBLEM_BODY,
        )

        second = client.post(
            "/chat/completions",
            headers={"X-Cache-Stage": "problem-insight"},
            json=PROBLEM_BODY,
        )

    assert first.status_code == 502
    assert second.status_code == 200
    assert second.headers["X-AI-Cache"] == "MISS"
    assert len(calls) == 2


def test_health_lists_all_stages(tmp_path):
    with client_for(tmp_path / "health.db", response_for({"is_valid": True, "invalid_reason": None}), []) as client:
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["stages"] == list(app.STAGE_VALIDATORS)


def test_persistence(tmp_path):
    calls = []
    db = tmp_path / "persist.db"
    with client_for(db, response_for({"is_valid": True, "invalid_reason": None}), calls) as client:
        r = client.post("/chat/completions", headers={"X-Cache-Stage": "quality"}, json=QUALITY_BODY)
        assert r.headers["X-AI-Cache"] == "MISS"

    with client_for(db, response_for({"is_valid": True, "invalid_reason": None}), []) as client:
        r = client.post("/chat/completions", headers={"X-Cache-Stage": "quality"}, json=QUALITY_BODY)
        assert r.status_code == 200
        assert r.headers["X-AI-Cache"] == "HIT"

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM ai_cache").fetchone()[0] == 1


def test_quality_batch_rejects_missing_or_duplicate_review_ids(tmp_path):
    calls = []
    invalid_payload = response_for({
        "results": [
            {"batch_item_id": "comment:1001", "is_valid": True, "invalid_reason": None},
            {"batch_item_id": "comment:1001", "is_valid": False, "invalid_reason": "缺少具体产品信息"},
        ]
    })
    with client_for(tmp_path / "quality-batch-invalid.db", invalid_payload, calls) as client:
        response = client.post(
            "/chat/completions",
            headers={"X-Cache-Stage": "quality-batch"},
            json=QUALITY_BATCH_BODY,
        )
    assert response.status_code == 502
    assert len(calls) == 1
    with sqlite3.connect(tmp_path / "quality-batch-invalid.db") as conn:
        assert conn.execute("SELECT count(*) FROM ai_cache").fetchone()[0] == 0


def test_issue_batch_rejects_missing_review_id(tmp_path):
    calls = []
    invalid_payload = response_for({
        "results": [{
            "batch_item_id": "comment:1001",
            "issues": [{
                "category": "功能异常", "feature": "App基础体验", "sentiment": "negative",
                "severity": 3, "user_need": "正常使用", "summary": "功能无法使用", "confidence": 0.9
            }]
        }]
    })
    with client_for(tmp_path / "issue-batch-invalid.db", invalid_payload, calls) as client:
        response = client.post(
            "/chat/completions",
            headers={"X-Cache-Stage": "issue-batch"},
            json=ISSUE_BATCH_BODY,
        )
    assert response.status_code == 502
    with sqlite3.connect(tmp_path / "issue-batch-invalid.db") as conn:
        assert conn.execute("SELECT count(*) FROM ai_cache").fetchone()[0] == 0
