"""Doubao AI cache proxy with response validation and quality diagnostics.

Cached stages:
- quality (legacy single-item)
- quality-batch
- issue (legacy single-item)
- issue-batch
- clustering
- global-consolidation
- problem-insight
- positive-insight

Pass-through stage (no cache reads/writes and no proxy-level report validation):
- positive-overall

The original whole-request cache remains unchanged. Optionally, quality-batch and
issue-batch also cache independently by stable source_comment_id, complete review
metadata, exact surrounding prompt, model parameters, namespace and stage version.
Set REVIEW_CACHE_ENABLED=1 to opt in, or 0 to roll back without losing data.
Optional per-stage versioning can invalidate one cached stage independently.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager, closing
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import logging
import os
import re
from pathlib import Path
import sqlite3
import time
from typing import AsyncIterator, Callable

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response


logger = logging.getLogger("uvicorn.error")


CATEGORIES = {
    "AI效果问题", "功能异常", "性能问题", "交互体验", "功能需求",
    "内容安全/审核问题", "账号/登录问题", "付费/额度问题", "隐私问题",
    "正向体验", "其他",
}
FEATURES = {
    "AI对话", "智能体", "记忆/上下文", "学习/搜题", "搜索/联网信息",
    "文本创作", "图片理解", "图片生成/编辑", "视频生成", "语音/朗读",
    "录音纪要", "会员/额度", "账号/登录", "App基础体验", "推荐/内容流", "其他",
}
SENTIMENTS = {"positive", "neutral", "negative"}
ISSUE_FIELDS = {
    "category", "feature", "sentiment", "severity", "user_need", "summary", "confidence"
}
QUALITY_INVALID_REASONS = {
    "缺少具体产品信息", "无意义内容", "广告/引流", "非豆包产品反馈",
}

STAGE_VALIDATORS = (
    "quality",
    "quality-batch",
    "issue",
    "issue-batch",
    "clustering",
    "global-consolidation",
    "problem-insight",
    "positive-insight",
    "positive-overall",
)

# Positive Overall Report does not use cache reads/writes or proxy-level
# response validation. Its output is validated by the n8n workflow instead.
NO_CACHE_STAGES = frozenset({"positive-overall"})
REVIEW_CACHE_STAGES = frozenset({"quality-batch", "issue-batch"})
REVIEW_TAG_PATTERN = re.compile(r"<reviews>\s*(.*?)\s*</reviews>", re.DOTALL)


def is_review_cache_enabled() -> bool:
    # Default OFF: preserves the already-working production behavior until
    # a deliberate deployment with REVIEW_CACHE_ENABLED=1.
    return os.getenv("REVIEW_CACHE_ENABLED", "0").strip().lower() in {
        "1", "true", "yes", "on"
    }



# Optional versions for each cache stage, supplied when starting the container.
# An unset variable keeps the historical key exactly unchanged ("legacy"),
# so existing cache records remain readable after this code is deployed.
# To invalidate one stage only, set CACHE_VERSION_ISSUE=v2, for example.
STAGE_VERSION_ENV = {
    stage: f"CACHE_VERSION_{stage.upper().replace('-', '_')}"
    for stage in STAGE_VALIDATORS
}


def load_stage_cache_versions() -> dict[str, str]:
    return {
        stage: os.getenv(env_var, "").strip() or "legacy"
        for stage, env_var in STAGE_VERSION_ENV.items()
    }


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def request_hash(
    body: dict,
    namespace: str,
    stage: str,
    stage_version: str = "legacy",
) -> str:
    key_material = {
        "namespace": namespace,
        "stage": stage,
        "request": body,
    }
    # Preserve all old hashes if versioning has not been enabled for this stage.
    if stage_version != "legacy":
        key_material["cache_version"] = stage_version
    material = canonical_json(key_material)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def clean_json_content(raw: object) -> str:
    if not isinstance(raw, str):
        return ""
    clean = raw.strip()
    if clean.startswith("```"):
        lines = clean.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            clean = "\n".join(lines[1:-1]).strip()
    return clean


def response_content(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    try:
        choice = payload["choices"][0]
        if choice.get("finish_reason") != "stop":
            return ""
        return clean_json_content(choice["message"]["content"])
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""


def parse_response_json(payload: object) -> object | None:
    raw = response_content(payload)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def get_user_message(body: dict) -> str:
    messages = body.get("messages")
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            return content if isinstance(content, str) else ""
    return ""


def extract_tagged_json(text: str, tag: str) -> object | None:
    start_marker = f"<{tag}>"
    end_marker = f"</{tag}>"
    start = text.find(start_marker)
    if start < 0:
        return None
    start += len(start_marker)
    end = text.find(end_marker, start)
    if end < 0:
        return None
    raw = text[start:end].strip()
    try:
        return json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def validate_quality_row(result: object) -> bool:
    if not isinstance(result, dict) or set(result) != {"is_valid", "invalid_reason"}:
        return False

    is_valid = result["is_valid"]
    reason = result["invalid_reason"]
    if type(is_valid) is not bool:
        return False
    if is_valid:
        return reason is None
    return isinstance(reason, str) and reason in QUALITY_INVALID_REASONS


def validate_quality_response(payload: object, body: dict) -> bool:
    return validate_quality_row(parse_response_json(payload))


def extract_batch_ids(body: dict) -> list[str] | None:
    reviews = extract_tagged_json(get_user_message(body), "reviews")
    if not isinstance(reviews, list) or not reviews:
        return None

    ids: list[str] = []
    for review in reviews:
        if not isinstance(review, dict):
            return None
        batch_item_id = review.get("batch_item_id")
        content = review.get("content")
        if not isinstance(batch_item_id, str) or not batch_item_id.strip():
            return None
        if not isinstance(content, str) or not content.strip():
            return None
        ids.append(batch_item_id.strip())

    if len(ids) != len(set(ids)):
        return None
    return ids


def validate_quality_batch_response(payload: object, body: dict) -> bool:
    expected_ids = extract_batch_ids(body)
    if expected_ids is None:
        return False

    result = parse_response_json(payload)
    if not isinstance(result, dict) or set(result) != {"results"}:
        return False
    rows = result["results"]
    if not isinstance(rows, list) or len(rows) != len(expected_ids):
        return False

    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"batch_item_id", "is_valid", "invalid_reason"}:
            return False
        batch_item_id = row.get("batch_item_id")
        if not isinstance(batch_item_id, str) or batch_item_id not in expected_ids or batch_item_id in seen:
            return False
        if not validate_quality_row({
            "is_valid": row.get("is_valid"),
            "invalid_reason": row.get("invalid_reason"),
        }):
            return False
        seen.add(batch_item_id)

    return seen == set(expected_ids)


def validate_issue_item(item: object) -> bool:
    if not isinstance(item, dict) or not ISSUE_FIELDS.issubset(item):
        return False
    if not isinstance(item["category"], str) or item["category"] not in CATEGORIES:
        return False
    if not isinstance(item["feature"], str) or item["feature"] not in FEATURES:
        return False
    if not isinstance(item["sentiment"], str) or item["sentiment"] not in SENTIMENTS:
        return False

    severity = item["severity"]
    confidence = item["confidence"]
    if type(severity) is not int or not 1 <= severity <= 5:
        return False
    if type(confidence) not in (int, float) or not 0 <= confidence <= 1:
        return False
    if not isinstance(item["summary"], str) or not item["summary"].strip():
        return False
    if not isinstance(item["user_need"], str) or not item["user_need"].strip():
        return False
    return True


def validate_issue_response(payload: object, body: dict) -> bool:
    result = parse_response_json(payload)
    if not isinstance(result, dict) or set(result) != {"issues"}:
        return False
    issues = result["issues"]
    if not isinstance(issues, list) or not issues:
        return False
    return all(validate_issue_item(item) for item in issues)


def validate_issue_batch_response(payload: object, body: dict) -> bool:
    expected_ids = extract_batch_ids(body)
    if expected_ids is None:
        return False

    result = parse_response_json(payload)
    if not isinstance(result, dict) or set(result) != {"results"}:
        return False
    rows = result["results"]
    if not isinstance(rows, list) or len(rows) != len(expected_ids):
        return False

    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"batch_item_id", "issues"}:
            return False
        batch_item_id = row.get("batch_item_id")
        if not isinstance(batch_item_id, str) or batch_item_id not in expected_ids or batch_item_id in seen:
            return False
        issues = row.get("issues")
        if not isinstance(issues, list) or not issues:
            return False
        if not all(validate_issue_item(issue) for issue in issues):
            return False
        seen.add(batch_item_id)

    return seen == set(expected_ids)


def validate_clustering_response(payload: object, body: dict) -> bool:
    result = parse_response_json(payload)
    if not isinstance(result, dict) or set(result) != {"clusters"}:
        return False

    clusters = result["clusters"]
    if not isinstance(clusters, list) or not clusters:
        return False

    input_issues = extract_tagged_json(get_user_message(body), "issues")
    if not isinstance(input_issues, list):
        return False

    input_keys = [
        str(item.get("issue_key")).strip()
        for item in input_issues
        if isinstance(item, dict) and item.get("issue_key")
    ]
    if not input_keys or len(set(input_keys)) != len(input_keys):
        return False

    output_keys: list[str] = []

    for cluster in clusters:
        if not isinstance(cluster, dict):
            return False
        name = cluster.get("cluster_name")
        members = cluster.get("member_issue_keys")

        if not isinstance(name, str) or not name.strip():
            return False
        if not isinstance(members, list) or not members:
            return False
        if any(not isinstance(key, str) or not key.strip() for key in members):
            return False

        output_keys.extend(str(key).strip() for key in members)

    return (
        len(output_keys) == len(input_keys)
        and len(set(output_keys)) == len(output_keys)
        and set(output_keys) == set(input_keys)
    )


def validate_global_consolidation_response(payload: object, body: dict) -> bool:
    result = parse_response_json(payload)
    if not isinstance(result, dict) or set(result) != {"final_clusters"}:
        return False

    final_clusters = result["final_clusters"]
    if not isinstance(final_clusters, list) or not final_clusters:
        return False

    input_clusters = extract_tagged_json(get_user_message(body), "clusters")
    if not isinstance(input_clusters, list):
        return False

    input_ids = [
        str(item.get("cluster_id")).strip()
        for item in input_clusters
        if isinstance(item, dict) and item.get("cluster_id")
    ]
    if not input_ids or len(set(input_ids)) != len(input_ids):
        return False

    output_ids: list[str] = []
    final_ids: list[str] = []

    for item in final_clusters:
        if not isinstance(item, dict):
            return False

        final_id = item.get("final_cluster_id")
        final_name = item.get("final_cluster_name")
        source_ids = item.get("source_cluster_ids")

        if not isinstance(final_id, str) or not final_id.strip():
            return False
        if not isinstance(final_name, str) or not final_name.strip():
            return False
        if not isinstance(source_ids, list) or not source_ids:
            return False
        if any(not isinstance(x, str) or not x.strip() for x in source_ids):
            return False

        final_ids.append(final_id.strip())
        output_ids.extend(str(x).strip() for x in source_ids)

    return (
        len(final_ids) == len(set(final_ids))
        and len(output_ids) == len(input_ids)
        and len(set(output_ids)) == len(output_ids)
        and set(output_ids) == set(input_ids)
    )


INSIGHT_FIELDS = {
    "final_cluster_id",
    "insight_type",
    "insight_summary",
    "user_signal",
    "user_need",
    "product_implication",
    "pm_followup",
    "evidence_summary",
    "limitations",
}


def expected_final_cluster_id(body: dict) -> str:
    user = get_user_message(body)
    marker = "输入数据："
    if marker not in user:
        return ""
    raw = user.rsplit(marker, 1)[1].strip()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    if isinstance(data, dict):
        value = data.get("final_cluster_id")
        return str(value).strip() if value is not None else ""
    return ""


def validate_insight_response(payload: object, body: dict, expected_type: str) -> bool:
    result = parse_response_json(payload)
    if not isinstance(result, dict) or set(result) != INSIGHT_FIELDS:
        return False

    if result.get("insight_type") != expected_type:
        return False

    if not isinstance(result.get("final_cluster_id"), str):
        return False
    if result["final_cluster_id"].strip() != expected_final_cluster_id(body):
        return False

    string_fields = INSIGHT_FIELDS - {"final_cluster_id", "insight_type"}
    for field in string_fields:
        if not isinstance(result.get(field), str) or not result[field].strip():
            return False

    return True


def validate_problem_insight_response(payload: object, body: dict) -> bool:
    return validate_insight_response(payload, body, "problem")


def validate_positive_insight_response(payload: object, body: dict) -> bool:
    return validate_insight_response(payload, body, "positive")


def extract_positive_insights(body: dict) -> list[dict] | None:
    user = get_user_message(body)
    start_marker = "Positive Insights:\n"
    end_marker = "\n\n输出必须严格符合以下 JSON 结构："
    start = user.find(start_marker)
    if start < 0:
        return None
    start += len(start_marker)
    end = user.find(end_marker, start)
    if end < 0:
        return None
    raw = user[start:end].strip()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, list) else None


def validate_positive_overall_response(payload: object, body: dict) -> bool:
    result = parse_response_json(payload)
    if not isinstance(result, dict):
        return False

    required = {
        "overall_summary",
        "core_strengths",
        "cross_cluster_patterns",
        "product_implication",
        "representative_signals",
        "limitations",
    }
    if set(result) != required:
        return False

    if not isinstance(result["overall_summary"], str) or not result["overall_summary"].strip():
        return False
    if not isinstance(result["product_implication"], str) or not result["product_implication"].strip():
        return False
    if not isinstance(result["limitations"], str) or not result["limitations"].strip():
        return False

    core = result["core_strengths"]
    patterns = result["cross_cluster_patterns"]
    signals = result["representative_signals"]

    if not isinstance(patterns, list):
        return False
    if any(not isinstance(x, str) or not x.strip() for x in patterns):
        return False
    if not isinstance(signals, list) or len(signals) > 3:
        return False

    source = extract_positive_insights(body)
    if not source:
        return False

    max_core_strengths = min(5, len(source))
    if not isinstance(core, list) or not 1 <= len(core) <= max_core_strengths:
        return False

    allowed_ids = {
        str(item.get("final_cluster_id")).strip()
        for item in source
        if isinstance(item, dict) and item.get("final_cluster_id")
    }
    if not allowed_ids:
        return False

    representative_comments = {
        str(item.get("final_cluster_id")).strip():
            str(item.get("representative_comment") or "")
        for item in source
        if isinstance(item, dict) and item.get("final_cluster_id")
    }

    for item in core:
        if not isinstance(item, dict):
            return False
        if set(item) != {
            "strength_name",
            "summary",
            "supporting_cluster_ids",
            "user_value",
        }:
            return False
        for field in ("strength_name", "summary", "user_value"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                return False
        ids = item["supporting_cluster_ids"]
        if not isinstance(ids, list) or not ids:
            return False
        if any(str(x).strip() not in allowed_ids for x in ids):
            return False

    for signal in signals:
        if not isinstance(signal, dict):
            return False
        if set(signal) != {"final_cluster_id", "comment"}:
            return False
        cluster_id = signal.get("final_cluster_id")
        comment = signal.get("comment")
        if not isinstance(cluster_id, str) or not cluster_id.strip():
            return False
        if cluster_id.strip() not in allowed_ids:
            return False
        if not isinstance(comment, str) or not comment.strip():
            return False
        if comment != representative_comments.get(cluster_id.strip(), ""):
            return False

    return True


VALIDATORS: dict[str, Callable[[object, dict], bool]] = {
    "quality": validate_quality_response,
    "quality-batch": validate_quality_batch_response,
    "issue": validate_issue_response,
    "issue-batch": validate_issue_batch_response,
    "clustering": validate_clustering_response,
    "global-consolidation": validate_global_consolidation_response,
    "problem-insight": validate_problem_insight_response,
    "positive-insight": validate_positive_insight_response,
    "positive-overall": validate_positive_overall_response,
}


@dataclass(frozen=True)
class ReviewCacheCandidate:
    batch_item_id: str
    review: dict
    key: str | None
    review_fingerprint: str | None


@dataclass(frozen=True)
class ReviewCachePlan:
    candidates: list[ReviewCacheCandidate]
    user_message_index: int
    prompt_fingerprint: str


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def prepare_review_cache_plan(
    body: dict, namespace: str, stage: str, stage_version: str
) -> ReviewCachePlan | None:
    """Only optimize a recognizable, unambiguous <reviews> batch request.

    The fingerprint includes all original instructions and model parameters,
    but replaces the *whole* reviews array with a fixed placeholder. The per-
    review fingerprint contains all original fields except the transport-only
    batch_item_id. This ensures changing a review or prompt is a cache MISS.
    """
    if stage not in REVIEW_CACHE_STAGES:
        return None
    expected_ids = extract_batch_ids(body)
    if expected_ids is None:
        return None
    messages = body.get("messages", [])
    user_indices = [i for i, m in enumerate(messages)
                    if m.get("role") == "user"]
    if not user_indices:
        return None
    index = user_indices[-1]
    original_text = messages[index]["content"]
    matches = list(REVIEW_TAG_PATTERN.finditer(original_text))
    if len(matches) != 1:
        return None
    reviews = extract_tagged_json(original_text, "reviews")
    if not isinstance(reviews, list) or len(reviews) != len(expected_ids):
        return None

    template = deepcopy(body)
    m = matches[0]
    template["messages"][index]["content"] = (
        original_text[:m.start()] + "<reviews>__REVIEW_CACHE_V1__</reviews>"
        + original_text[m.end():]
    )
    prompt_fingerprint = sha256_json(template)
    candidates = []
    for review, item_id in zip(reviews, expected_ids):
        # Only IDs actually supplied by the review source qualify. This
        # deliberately excludes n8n-generated row:1/valid-row:1 IDs.
        source_id = review.get("source_comment_id")
        normalized_id = str(source_id).strip() if source_id is not None else ""
        cache_key = None
        review_fp = None
        if normalized_id and review.get("batch_item_id") == f"comment:{normalized_id}":
            canonical_review = {
                k: v for k, v in review.items() if k != "batch_item_id"
            }
            try:
                review_fp = sha256_json(canonical_review)
                cache_key = sha256_json({
                    "cache_type": "review-v1", "namespace": namespace,
                    "stage": stage, "stage_version": stage_version,
                    "prompt_fingerprint": prompt_fingerprint,
                    "review_fingerprint": review_fp,
                })
            except (TypeError, ValueError):
                pass
        candidates.append(ReviewCacheCandidate(item_id, review, cache_key, review_fp))
    return ReviewCachePlan(candidates, index, prompt_fingerprint)


def response_rows_from_raw(raw: str, stage: str, body: dict) -> dict[str, dict] | None:
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return response_rows(decoded, stage, body)


def response_rows(payload: object, stage: str, body: dict) -> dict[str, dict] | None:
    if stage not in REVIEW_CACHE_STAGES or not VALIDATORS[stage](payload, body):
        return None
    parsed = parse_response_json(payload)
    return {row["batch_item_id"]: row for row in parsed["results"]}


def validate_cached_review_row(stage: str, row: object) -> bool:
    if not isinstance(row, dict) or not isinstance(row.get("batch_item_id"), str):
        return False
    if stage == "quality-batch":
        return (
            set(row) == {"batch_item_id", "is_valid", "invalid_reason"}
            and validate_quality_row({
                "is_valid": row.get("is_valid"),
                "invalid_reason": row.get("invalid_reason"),
            })
        )
    if stage == "issue-batch":
        return (
            set(row) == {"batch_item_id", "issues"}
            and isinstance(row.get("issues"), list)
            and bool(row["issues"])
            and all(validate_issue_item(issue) for issue in row["issues"])
        )
    return False


def find_cached_review_rows(
    path: Path, plan: ReviewCachePlan, stage: str
) -> dict[str, dict]:
    hits: dict[str, dict] = {}
    with closing(sqlite3.connect(path, timeout=30)) as conn:
        for candidate in plan.candidates:
            if candidate.key is None:
                continue
            record = conn.execute(
                "SELECT result_json FROM ai_review_cache WHERE cache_key=?",
                (candidate.key,),
            ).fetchone()
            if not record:
                continue
            try:
                row = json.loads(record[0])
            except (ValueError, TypeError):
                continue
            if not validate_cached_review_row(stage, row):
                continue
            row = dict(row)
            # ID belongs to the CURRENT request, not the batch where it was
            # first cached. Do not leak the previous batch_item_id into n8n.
            row["batch_item_id"] = candidate.batch_item_id
            hits[candidate.batch_item_id] = row
    return hits


def body_with_missing_reviews(body: dict, plan: ReviewCachePlan, missing: list) -> dict:
    result = deepcopy(body)
    index = plan.user_message_index
    text = result["messages"][index]["content"]
    match = REVIEW_TAG_PATTERN.search(text)
    assert match is not None
    # Escape '<' as the existing n8n JSON Body does.
    serialized = canonical_json([c.review for c in missing]).replace("<", "\\u003c")
    result["messages"][index]["content"] = (
        text[:match.start(1)] + serialized + text[match.end(1):]
    )
    return result


def combined_response(upstream_payload: dict | None, model: str, rows: list[dict]) -> str:
    """Match the OpenAI chat response shape expected by both n8n parsers."""
    if upstream_payload is None:
        result = {
            "id": "review-cache-reassembled", "object": "chat.completion",
            "model": model,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": "",
            }}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
    else:
        result = deepcopy(upstream_payload)
    result["choices"][0]["message"]["content"] = canonical_json({"results": rows})
    return canonical_json(result)


def save_batch_and_reviews(
    path: Path, batch_key: str, stage: str, model: str,
    raw_response: str, plan: ReviewCachePlan | None,
    validated_rows: dict[str, dict] | None,
) -> None:
    """Atomically save the full-request and independent-review caches."""
    with closing(sqlite3.connect(path, timeout=30)) as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO ai_cache "
            "(cache_key, stage, model, response_json) VALUES (?, ?, ?, ?)",
            (batch_key, stage, model, raw_response),
        )
        if plan is not None and validated_rows is not None:
            for candidate in plan.candidates:
                if not candidate.key or candidate.batch_item_id not in validated_rows:
                    continue
                row = validated_rows[candidate.batch_item_id]
                if not validate_cached_review_row(stage, row):
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO ai_review_cache "
                    "(cache_key, stage, model, prompt_fingerprint, "
                    "review_fingerprint, result_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (candidate.key, stage, model, plan.prompt_fingerprint,
                     candidate.review_fingerprint, canonical_json(row)),
                )


def initialize_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=30)) as conn, conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_cache (
                cache_key TEXT PRIMARY KEY,
                stage TEXT NOT NULL,
                model TEXT NOT NULL,
                response_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (
                    strftime('%Y-%m-%dT%H:%M:%fZ','now')
                )
            )
        """)
        # Remove legacy Positive Overall Report cache rows. This stage is now
        # pass-through only and must never reuse or store cached responses.
        cursor = conn.execute(
            "DELETE FROM ai_cache WHERE stage = ?",
            ("positive-overall",),
        )
        if cursor.rowcount > 0:
            logger.info(
                "removed %s legacy positive-overall cache entries",
                cursor.rowcount,
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_cache_stage ON ai_cache(stage)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_review_cache (
                cache_key TEXT PRIMARY KEY,
                stage TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_fingerprint TEXT NOT NULL,
                review_fingerprint TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (
                    strftime('%Y-%m-%dT%H:%M:%fZ','now')
                )
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_review_cache_stage "
            "ON ai_review_cache(stage)"
        )


def read_cache(path: Path, key: str) -> str | None:
    with closing(sqlite3.connect(path, timeout=30)) as conn, conn:
        row = conn.execute(
            "SELECT response_json FROM ai_cache WHERE cache_key = ?",
            (key,),
        ).fetchone()
    return row[0] if row else None


def create_app(
    *,
    db_path: str | Path | None = None,
    api_key: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    database_path = Path(
        db_path or os.getenv("CACHE_DB_PATH", "/data/cache.sqlite3")
    )
    secret = (
        api_key
        if api_key is not None
        else os.getenv("DEEPSEEK_API_KEY", "")
    )
    namespace = os.getenv("CACHE_NAMESPACE", "issue-v2")
    stage_cache_versions = load_stage_cache_versions()
    review_cache_enabled = is_review_cache_enabled()
    upstream_url = os.getenv(
        "DEEPSEEK_URL",
        "https://api.deepseek.com/chat/completions",
    )

    locks: dict[str, tuple[asyncio.Lock, int]] = {}
    guard = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        initialize_db(database_path)
        async with httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(
                connect=15,
                read=240,
                write=30,
                pool=15,
            ),
            follow_redirects=False,
        ) as client:
            app.state.client = client
            yield

    app = FastAPI(
        title="Doubao AI Cache Proxy",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "stages": list(STAGE_VALIDATORS),
            "cache_bypass_stages": sorted(NO_CACHE_STAGES),
            "cache_versions": stage_cache_versions,
            "review_cache_enabled": review_cache_enabled,
            "review_cache_stages": sorted(REVIEW_CACHE_STAGES),
        }

    @app.post("/chat/completions")
    async def chat_completions(request: Request) -> Response:
        stage = request.headers.get("X-Cache-Stage", "issue").strip().lower()

        if stage not in VALIDATORS:
            raise HTTPException(
                400,
                f"Unsupported cache stage: {stage}",
            )

        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("body must be an object")

            model = body.get("model")
            messages = body.get("messages")

            if not isinstance(model, str) or not model.strip():
                raise ValueError("model is required")
            if not isinstance(messages, list) or not messages:
                raise ValueError("messages must be a non-empty array")

            if any(
                not isinstance(m, dict)
                or m.get("role") not in ("system", "user", "assistant")
                or not isinstance(m.get("content"), str)
                for m in messages
            ):
                raise ValueError("invalid messages")

            if body.get("stream", False):
                raise ValueError("streaming responses are not supported")

            response_format = body.get("response_format")
            if (
                not isinstance(response_format, dict)
                or response_format.get("type") != "json_object"
            ):
                raise ValueError("response_format.type must be json_object")

            key = request_hash(body, namespace, stage, stage_cache_versions[stage])

        except (ValueError, TypeError, AttributeError, json.JSONDecodeError) as exc:
            raise HTTPException(400, f"Invalid chat request: {exc}") from exc

        started = time.monotonic()
        tag = key[:12]
        label = stage.upper()

        logger.info("%s %s received", label, tag)
        plan = (
            prepare_review_cache_plan(body, namespace, stage, stage_cache_versions[stage])
            if review_cache_enabled and stage in REVIEW_CACHE_STAGES else None
        )

        def answer(raw: str, hit: bool) -> Response:
            return Response(
                content=raw,
                media_type="application/json",
                headers={
                    "X-AI-Cache": "HIT" if hit else "MISS",
                    "X-AI-Cache-Stage": stage,
                    "X-AI-Cache-Version": stage_cache_versions[stage],
                },
            )

        # Closure exists to keep the original single upstream-response path
        # identical when the per-review optimization is disabled.
        async def fetch_and_cache(
            upstream_body: dict,
            plan: ReviewCachePlan | None,
            cached_rows: dict[str, dict],
            missing_candidates: list[ReviewCacheCandidate],
        ) -> Response:
            if not secret:
                raise HTTPException(
                    503,
                    "DEEPSEEK_API_KEY is not configured",
                )

            logger.info(
                "%s %s cache_miss upstream_start elapsed=%.1fs",
                label,
                tag,
                time.monotonic() - started,
            )

            try:
                upstream = await app.state.client.post(
                    upstream_url,
                    json=upstream_body,
                    headers={
                        "Authorization": f"Bearer {secret}",
                        "Content-Type": "application/json",
                    },
                )
            except httpx.TimeoutException as exc:
                logger.error(
                    "%s %s upstream_timeout elapsed=%.1fs",
                    label,
                    tag,
                    time.monotonic() - started,
                )
                raise HTTPException(
                    504,
                    "DeepSeek request timed out; response not cached",
                ) from exc
            except httpx.RequestError as exc:
                logger.error(
                    "%s %s upstream_connection_failed elapsed=%.1fs",
                    label,
                    tag,
                    time.monotonic() - started,
                )
                raise HTTPException(
                    502,
                    "DeepSeek connection failed; response not cached",
                ) from exc

            logger.info(
                "%s %s upstream_status=%s elapsed=%.1fs",
                label,
                tag,
                upstream.status_code,
                time.monotonic() - started,
            )

            if upstream.status_code != 200:
                status = (
                    upstream.status_code
                    if upstream.status_code in (400, 401, 403, 408, 429, 503)
                    else 502
                )
                raise HTTPException(
                    status,
                    f"DeepSeek returned HTTP {upstream.status_code}; not cached",
                )

            if stage in NO_CACHE_STAGES:
                logger.info(
                    "%s %s cache_bypass validation_bypass elapsed=%.1fs",
                    label,
                    tag,
                    time.monotonic() - started,
                )
                return Response(
                    content=upstream.content,
                    status_code=upstream.status_code,
                    headers={
                        "Content-Type": upstream.headers.get(
                            "content-type",
                            "application/json",
                        ),
                        "X-AI-Cache": "BYPASS",
                        "X-AI-Cache-Stage": stage,
                        "X-AI-Cache-Version": stage_cache_versions[stage],
                    },
                )

            try:
                parsed_response = upstream.json()
            except ValueError as exc:
                raise HTTPException(
                    502,
                    "DeepSeek did not return JSON; not cached",
                ) from exc

            if stage == "quality-batch":
                choices = parsed_response.get("choices", []) if isinstance(parsed_response, dict) else []
                choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
                message = choice.get("message", {})
                content = message.get("content") if isinstance(message, dict) else None

                clean_content = clean_json_content(content)
                parsed_content = None
                json_error = None

                if clean_content:
                    try:
                        parsed_content = json.loads(clean_content)
                    except (TypeError, ValueError) as exc:
                        json_error = f"{type(exc).__name__}: {exc}"

                # For a partial cache HIT, DeepSeek receives only the MISS subset.
                # The running version used `body` here; full requests are unchanged,
                # but partial requests must be diagnosed against `upstream_body`.
                expected_ids = extract_batch_ids(upstream_body)
                results = parsed_content.get("results") if isinstance(parsed_content, dict) else None

                row_key_shapes = sorted({
                    ",".join(sorted(row.keys()))
                    for row in results
                    if isinstance(row, dict)
                }) if isinstance(results, list) else []

                returned_ids = [
                    row.get("batch_item_id")
                    for row in results
                    if isinstance(row, dict)
                ] if isinstance(results, list) else []

                ids_match = (
                    expected_ids is not None
                    and len(returned_ids) == len(expected_ids)
                    and all(isinstance(x, str) for x in returned_ids)
                    and set(returned_ids) == set(expected_ids)
                )

                invalid_reasons = sorted({
                    str(row.get("invalid_reason"))
                    for row in results
                    if isinstance(row, dict)
                }) if isinstance(results, list) else []

                logger.warning(
                    "QUALITY-BATCH-DIAG finish_reason=%r content_type=%s content_len=%s json_error=%r top_keys=%s expected_count=%s result_count=%s row_key_shapes=%s ids_match=%s invalid_reasons=%s usage=%s",
                    choice.get("finish_reason"),
                    type(content).__name__,
                    len(content) if isinstance(content, str) else None,
                    json_error,
                    sorted(parsed_content.keys()) if isinstance(parsed_content, dict) else type(parsed_content).__name__,
                    len(expected_ids) if expected_ids is not None else None,
                    len(results) if isinstance(results, list) else None,
                    row_key_shapes,
                    ids_match,
                    invalid_reasons,
                    parsed_response.get("usage") if isinstance(parsed_response, dict) else None,
                )

            if not VALIDATORS[stage](parsed_response, upstream_body):
                logger.error(
                    "%s %s invalid_response elapsed=%.1fs",
                    label,
                    tag,
                    time.monotonic() - started,
                )
                raise HTTPException(
                    502,
                    f"DeepSeek {stage} JSON is incomplete or invalid; not cached",
                )

            raw_response = upstream.text
            rows = None
            if plan is not None:
                upstream_rows = response_rows(parsed_response, stage, upstream_body)
                if upstream_rows is None:
                    raise HTTPException(502, "Invalid sub-batch response")
                if cached_rows:
                    merged_rows = {**cached_rows, **upstream_rows}
                    ordered = [merged_rows[c.batch_item_id] for c in plan.candidates]
                    raw_response = combined_response(parsed_response, model, ordered)
                else:
                    merged_rows = upstream_rows
                if not VALIDATORS[stage](json.loads(raw_response), body):
                    raise HTTPException(502, "Merged response failed validation; not cached")
                rows = merged_rows
            try:
                save_batch_and_reviews(
                    database_path, key, stage, model, raw_response, plan, rows
                )
            except sqlite3.Error as exc:
                logger.error(
                    "%s %s cache_write_failed",
                    label,
                    tag,
                )
                raise HTTPException(
                    503,
                    "Cache database write failed; no success returned",
                ) from exc

            logger.info(
                "%s %s saved elapsed=%.1fs",
                label,
                tag,
                time.monotonic() - started,
            )

            if cached_rows:
                logger.info(
                    "%s %s partial_cache_hit reused=%d analyzed=%d elapsed=%.1fs",
                    label, tag, len(cached_rows), len(missing_candidates),
                    time.monotonic() - started,
                )
                return Response(
                    content=raw_response, media_type="application/json",
                    headers={
                        "X-AI-Cache": "PARTIAL",
                        "X-AI-Cache-Stage": stage,
                        "X-AI-Cache-Version": stage_cache_versions[stage],
                    },
                )
            return answer(raw_response, hit=False)


        try:
            previous = (
                None
                if stage in NO_CACHE_STAGES
                else read_cache(database_path, key)
            )
            if previous is not None:
                if plan is not None:
                    validated = response_rows_from_raw(previous, stage, body)
                    if validated is not None:
                        save_batch_and_reviews(
                            database_path, key, stage, model, previous, plan, validated
                        )
                logger.info(
                    "%s %s cache_hit elapsed=%.1fs",
                    label,
                    tag,
                    time.monotonic() - started,
                )
                return answer(previous, hit=True)
        except sqlite3.Error as exc:
            logger.error("%s %s cache_read_failed", label, tag)
            raise HTTPException(503, "Cache database read failed") from exc

        async with one_request_per_key(key, locks, guard):
            try:
                previous = (
                    None
                    if stage in NO_CACHE_STAGES
                    else read_cache(database_path, key)
                )
                if previous is not None:
                    if plan is not None:
                        validated = response_rows_from_raw(previous, stage, body)
                        if validated is not None:
                            save_batch_and_reviews(
                                database_path, key, stage, model, previous, plan, validated
                            )
                    logger.info(
                        "%s %s cache_hit_after_wait elapsed=%.1fs",
                        label,
                        tag,
                        time.monotonic() - started,
                    )
                    return answer(previous, hit=True)
            except sqlite3.Error as exc:
                logger.error(
                    "%s %s cache_read_failed_after_wait",
                    label,
                    tag,
                )
                raise HTTPException(
                    503,
                    "Cache database read failed",
                ) from exc

            cached_rows: dict[str, dict] = {}
            missing_candidates: list[ReviewCacheCandidate] = []
            upstream_body = body
            # All row locks are held until merged results are committed. This
            # avoids duplicate DeepSeek calls for overlapping concurrent batches.
            async with AsyncExitStack() as row_locks:
                if plan is not None:
                    for row_key in sorted({c.key for c in plan.candidates if c.key}):
                        await row_locks.enter_async_context(
                            one_request_per_key("review:" + row_key, locks, guard)
                        )
                    try:
                        cached_rows = find_cached_review_rows(database_path, plan, stage)
                    except sqlite3.Error as exc:
                        raise HTTPException(503, "Review cache read failed") from exc
                    missing_candidates = [
                        c for c in plan.candidates if c.batch_item_id not in cached_rows
                    ]
                    logger.info(
                        "%s %s review_cache hits=%d misses=%d elapsed=%.1fs",
                        label, tag, len(cached_rows), len(missing_candidates),
                        time.monotonic() - started,
                    )
                    if not missing_candidates:
                        ordered = [cached_rows[c.batch_item_id] for c in plan.candidates]
                        assembled = combined_response(None, model, ordered)
                        if not VALIDATORS[stage](json.loads(assembled), body):
                            raise HTTPException(502, "Reassembled cached batch failed validation")
                        try:
                            save_batch_and_reviews(
                                database_path, key, stage, model, assembled,
                                plan, cached_rows,
                            )
                        except sqlite3.Error as exc:
                            raise HTTPException(503, "Review cache write failed") from exc
                        logger.info(
                            "%s %s cache_hit_reviews elapsed=%.1fs",
                            label, tag, time.monotonic() - started,
                        )
                        return answer(assembled, hit=True)
                    if cached_rows:
                        upstream_body = body_with_missing_reviews(
                            body, plan, missing_candidates
                        )

                return await fetch_and_cache(
                    upstream_body, plan, cached_rows, missing_candidates
                )


    return app


@asynccontextmanager
async def one_request_per_key(
    key: str,
    locks: dict[str, tuple[asyncio.Lock, int]],
    guard: asyncio.Lock,
) -> AsyncIterator[None]:
    async with guard:
        if key in locks:
            lock, users = locks[key]
            locks[key] = (lock, users + 1)
        else:
            lock = asyncio.Lock()
            locks[key] = (lock, 1)
    try:
        async with lock:
            yield
    finally:
        async with guard:
            _, users = locks[key]
            if users == 1:
                del locks[key]
            else:
                locks[key] = (lock, users - 1)


app = create_app()
