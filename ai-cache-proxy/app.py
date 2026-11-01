"""Doubao AI cache proxy.

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

The cache key includes namespace + stage + complete request body.
Optional per-stage versioning can invalidate one cached stage independently.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, closing
import hashlib
import json
import logging
import os
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


def read_cache(path: Path, key: str) -> str | None:
    with closing(sqlite3.connect(path, timeout=30)) as conn, conn:
        row = conn.execute(
            "SELECT response_json FROM ai_cache WHERE cache_key = ?",
            (key,),
        ).fetchone()
    return row[0] if row else None


def save_cache(
    path: Path,
    key: str,
    stage: str,
    model: str,
    response_text: str,
) -> None:
    with closing(sqlite3.connect(path, timeout=30)) as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO ai_cache "
            "(cache_key, stage, model, response_json) "
            "VALUES (?, ?, ?, ?)",
            (key, stage, model, response_text),
        )


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

        try:
            previous = (
                None
                if stage in NO_CACHE_STAGES
                else read_cache(database_path, key)
            )
            if previous is not None:
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
                    json=body,
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

            if not VALIDATORS[stage](parsed_response, body):
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

            try:
                save_cache(
                    database_path,
                    key,
                    stage,
                    model,
                    upstream.text,
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

            return answer(upstream.text, hit=False)

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
