"""
대화 스레드 영속화 — DynamoDB `chat_sessions` (정본: ai-chatbot-mcp.md §3-4/§3-5/§7).

스레드 정체성 = (user_public_id, document_public_id). session_id는 로깅용 메타다.

키 설계 (§7):
    PK: USER#{user_public_id}
    SK: DOC#{document_public_id}#TS#{epoch_ms:013d}#SEQ#{0|1}
        └ SEQ: 같은 턴의 user(0) → assistant(1). 동일 ms 충돌에도 복원 순서 결정적.

저장 규칙:
    - content는 최종 텍스트만 (toolUse/toolResult 블록 저장 안 함 — §3-4 비대칭)
    - 첫 턴에 주입한 합성 요약 2턴도 visible=False로 저장 (§3-2 — 복구 시 요약 유실 방지)
    - TTL 90일 (PII 보관기간 관리)
    - 저장 실패는 대화를 깨지 않는다 — 로그만 남기고 계속 (이번 턴 기록만 유실)

비활성 조건: 환경변수 DYNAMODB_TABLE_NAME 미설정 또는 user/document id 부재(로컬 테스트 등)
→ 모든 함수가 조용히 no-op (단일턴 동작은 그대로).
"""
import base64
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Attr, Key

try:
    import redis as _redis_module
except ImportError:  # redis 미설치 환경(구버전 배포 등) — 캐시만 비활성, DynamoDB 단독 동작
    _redis_module = None

logger = logging.getLogger(__name__)

TTL_DAYS = 90
REDIS_TTL_SECONDS = 1800  # 30분 — "대화 제한"이 아니라 캐시 유효기간(§3-4)
DEFAULT_REGION = "ap-northeast-2"

_table = None
_redis_client = None


def _get_table():
    """DynamoDB Table 핸들 (lazy, 콜드스타트 1회). 테이블명 미설정이면 None → 영속화 비활성."""
    global _table
    table_name = os.environ.get("DYNAMODB_TABLE_NAME")
    if not table_name:
        return None
    if _table is None:
        _table = boto3.resource(
            "dynamodb",
            region_name=os.environ.get("AWS_REGION", DEFAULT_REGION),
        ).Table(table_name)
    return _table


def _get_redis():
    """Redis 클라이언트 (lazy). REDIS_HOST 미설정/패키지 부재면 None → 캐시 비활성(DynamoDB 단독)."""
    global _redis_client
    host = os.environ.get("REDIS_HOST")
    if _redis_module is None or not host:
        return None
    if _redis_client is None:
        _redis_client = _redis_module.Redis(
            host=host,
            port=int(os.environ.get("REDIS_PORT", "6379")),
            password=os.environ.get("REDIS_PASSWORD") or None,
            socket_timeout=2,
            socket_connect_timeout=2,
            decode_responses=True,
        )
    return _redis_client


def _thread_key(user_public_id: str, document_public_id: str) -> str:
    """Redis 키 — 스레드 정체성 기준(§3-5). session_id 키면 재방문 시 항상 miss라 무의미."""
    return f"chat:thread:{user_public_id}:{document_public_id}"


def cache_thread(user_public_id: str, document_public_id: str, messages: list) -> None:
    """진행 중 스레드를 Redis에 통째 캐시(TTL 30분 갱신 — §3-4).

    Redis에는 toolUse/toolResult 블록 포함 Bedrock messages 배열 전체가 들어간다
    (컨텍스트 충실도). 실패는 캐시 미스로 동작할 뿐이라 raise하지 않는다.
    """
    r = _get_redis()
    if r is None or not user_public_id or not document_public_id:
        return
    try:
        r.setex(
            _thread_key(user_public_id, document_public_id),
            REDIS_TTL_SECONDS,
            json.dumps(messages, ensure_ascii=False),
        )
    except Exception:
        logger.warning("Redis cache write failed — DynamoDB only", exc_info=True)


def build_context_turns(analysis_summary: str) -> list:
    """첫 대화에 주입하는 합성 요약 2턴 (Bedrock messages 형식).

    주입(chatbot)·저장(save_turn)·복구 검증이 같은 텍스트를 쓰도록 한 곳에 둔다.
    """
    return [
        {
            "role": "user",
            "content": [{
                "text": (
                    f"[분석된 계약서 요약]\n"
                    f"{analysis_summary}\n\n"
                    f"위 계약서에 대해 질문하겠습니다."
                )
            }],
        },
        {
            "role": "assistant",
            "content": [{"text": "네, 확인했습니다. 궁금한 점을 물어보세요."}],
        },
    ]


def load_thread(
    user_public_id: str,
    document_public_id: str,
    analysis_summary: Optional[str] = None,
) -> tuple:
    """스레드 복원. Returns (messages, inject_context).

    - DynamoDB에 기록 있음 → 텍스트 턴 재구성(visible 무관 전체 — 합성 요약 턴 포함이라
      요약 재주입 불필요). inject_context=False — 백엔드가 보낸 analysis_summary는 무시
      (재방문 중복 주입 방지, "첫 턴" 판정 권한은 여기 — §6).
    - 기록 없음(진짜 첫 대화) → 요약 있으면 합성 2턴 생성, inject_context=True
      (save_turn이 합성 턴도 저장하도록).
    - 영속화 비활성/조회 실패 → 합성 주입만으로 진행하되 inject_context=False
      (실패 시 기존 스레드 존재 가능성이 있어 합성 턴 중복 저장을 피한다).
    """
    if not user_public_id or not document_public_id:
        messages = build_context_turns(analysis_summary) if analysis_summary else []
        return messages, False

    # 1) Redis hit — toolUse 블록 포함 통째 (§3-4)
    r = _get_redis()
    if r is not None:
        try:
            raw = r.get(_thread_key(user_public_id, document_public_id))
            if raw:
                logger.info("Thread restored from Redis cache")
                return json.loads(raw), False
        except Exception:
            logger.warning("Redis cache read failed — falling back to DynamoDB", exc_info=True)

    # 2) Redis miss → DynamoDB 복구
    table = _get_table()
    if table is None:
        messages = build_context_turns(analysis_summary) if analysis_summary else []
        return messages, False

    try:
        items = []
        kwargs = {
            "KeyConditionExpression": (
                Key("PK").eq(f"USER#{user_public_id}")
                & Key("SK").begins_with(f"DOC#{document_public_id}#")
            ),
            "ScanIndexForward": True,
        }
        while True:
            resp = table.query(**kwargs)
            items.extend(resp.get("Items", []))
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key
    except Exception:
        logger.error("DynamoDB thread load failed — continuing without history", exc_info=True)
        messages = build_context_turns(analysis_summary) if analysis_summary else []
        return messages, False

    if items:
        logger.info("Thread restored from DynamoDB: %d turns", len(items))
        messages = [
            {"role": it["role"], "content": [{"text": it["content"]}]}
            for it in items
        ]
        cache_thread(user_public_id, document_public_id, messages)  # Redis 재적재(§3-4)
        return messages, False

    if analysis_summary:
        return build_context_turns(analysis_summary), True
    return [], True


def save_turn(
    *,
    user_public_id: str,
    document_public_id: str,
    session_id: str,
    environment: str,
    language: str,
    user_message: str,
    assistant_reply: str,
    tools_used: Optional[list] = None,
    analysis_summary: Optional[str] = None,
    inject_context: bool = False,
) -> None:
    """턴 종료 저장 (BatchWrite — §7).

    첫 대화면 합성 2턴(visible=False) + user + assistant = 4건, 이후 턴은 2건.
    합성 턴은 실제 턴보다 1ms 앞 타임스탬프로 복원 순서를 보장한다.
    실패해도 raise하지 않는다 — 답변은 이미 사용자에게 갔으므로 기록 유실만 로그.
    """
    table = _get_table()
    if table is None or not user_public_id or not document_public_id:
        return

    now_ms = int(time.time() * 1000)
    ttl = now_ms // 1000 + TTL_DAYS * 86400
    pk = f"USER#{user_public_id}"

    def _item(ts_ms, seq, role, content, visible, tools=None):
        it = {
            "PK": pk,
            "SK": f"DOC#{document_public_id}#TS#{ts_ms:013d}#SEQ#{seq}",
            "session_id": session_id,
            "message_uuid": str(uuid.uuid4()),
            "role": role,
            "content": content,
            "visible": visible,
            "language": language,
            "environment": environment,
            "created_at": _iso_utc(ts_ms),
            "ttl": ttl,
        }
        if tools:
            it["tools_used"] = tools
        return it

    items = []
    if inject_context and analysis_summary:
        context = build_context_turns(analysis_summary)
        items.append(_item(now_ms - 1, 0, "user", context[0]["content"][0]["text"], False))
        items.append(_item(now_ms - 1, 1, "assistant", context[1]["content"][0]["text"], False))
    items.append(_item(now_ms, 0, "user", user_message, True))
    items.append(_item(now_ms, 1, "assistant", assistant_reply, True, tools_used or None))

    try:
        with table.batch_writer() as batch:
            for it in items:
                batch.put_item(Item=it)
        logger.info(
            "Turn saved: doc=%s items=%d (context=%s)",
            document_public_id, len(items), inject_context and analysis_summary is not None,
        )
    except Exception:
        logger.error(
            "DynamoDB turn save failed — conversation continues, this turn not persisted",
            exc_info=True,
        )


def fetch_history(
    user_public_id: str,
    document_public_id: str,
    limit: int = 50,
    cursor: Optional[str] = None,
) -> dict:
    """이력 조회 — 재방문 복원용 (§6-2). 사용자 노출 턴(visible=True)만 시간 오름차순 반환.

    Returns: {"messages": [{"role", "content", "created_at"}], "next_cursor": str|None}

    - cursor = base64(JSON LastEvaluatedKey). 잘못된 cursor는 무시하고 처음부터(로그만).
    - Limit은 DynamoDB 특성상 필터(visible) 적용 전 스캔 건수 기준이라, 첫 페이지는
      합성 2턴만큼 적게 나올 수 있다(다음 페이지 cursor로 이어짐 — 데모 스케일 무해).
    """
    table = _get_table()
    if table is None or not user_public_id or not document_public_id:
        return {"messages": [], "next_cursor": None}

    kwargs = {
        "KeyConditionExpression": (
            Key("PK").eq(f"USER#{user_public_id}")
            & Key("SK").begins_with(f"DOC#{document_public_id}#")
        ),
        "FilterExpression": Attr("visible").eq(True),
        "Limit": max(1, min(int(limit), 100)),
        "ScanIndexForward": True,
    }
    start_key = _decode_cursor(cursor)
    if start_key:
        kwargs["ExclusiveStartKey"] = start_key

    resp = table.query(**kwargs)
    messages = [
        {
            "role": it["role"],
            "content": it["content"],
            "created_at": it.get("created_at"),
        }
        for it in resp.get("Items", [])
    ]
    last_key = resp.get("LastEvaluatedKey")
    return {
        "messages": messages,
        "next_cursor": _encode_cursor(last_key) if last_key else None,
    }


def _encode_cursor(last_evaluated_key: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(last_evaluated_key).encode()).decode()


def _decode_cursor(cursor: Optional[str]) -> Optional[dict]:
    if not cursor:
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
    except Exception:
        logger.warning("Invalid history cursor ignored: %r", cursor)
        return None


def _iso_utc(epoch_ms: int) -> str:
    """epoch ms → ISO 8601 UTC Z (conventions.md 시각 포맷)."""
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
