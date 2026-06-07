"""
Translator core — Bedrock Claude Haiku 동기 호출로 커뮤니티 게시글·댓글을 번역.

챗봇 Lambda(`src/chatbot.py`) 와 같은 레포에 살지만 배포 단위는 분리된다.
(`gb-community-translator-{dev|stage|prod}`)

Design:
  - 모델: Haiku (BEDROCK_MODEL_ID 환경변수). 비용·속도 우선.
  - Bedrock `converse` (비스트리밍, 동기) 1회 호출. Tool Use 없음.
  - 시스템 프롬프트가 JSON 응답을 강제 → 파싱 후 dict 반환.
  - source_lang == target_lang 이면 Bedrock 호출 없이 원문 그대로 반환 (비용 절감).

References:
  - https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html
  - 챗봇과 달리 응답이 짧고 1턴이라 converse_stream 대신 converse 사용.
"""
import json
import logging
import os
import re
from typing import Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from src.translator_system_prompt import (
    SUPPORTED_LANGS,
    build_translator_system_prompt,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Haiku — 서울 리전 사용 시 BEDROCK_MODEL_ID 환경변수로 inference profile 지정.
# (apac.anthropic.claude-haiku-* 또는 global.anthropic.claude-haiku-* 계열)
DEFAULT_MODEL_ID = "global.anthropic.claude-haiku-4-5"
DEFAULT_REGION = "ap-northeast-2"

VALID_KINDS = {"post", "comment"}


# ──────────────────────────────────────────────────────────────────────────────
# 예외
# ──────────────────────────────────────────────────────────────────────────────
class TranslatorError(Exception):
    """번역 도중 발생한 비즈니스 에러. error_code 가 응답에 그대로 실린다."""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


# ──────────────────────────────────────────────────────────────────────────────
# Bedrock 클라이언트 (재사용 — 콜드스타트 후 캐시)
# ──────────────────────────────────────────────────────────────────────────────
_bedrock_client = None


def _get_bedrock_client():
    global _bedrock_client
    if _bedrock_client is None:
        _bedrock_client = boto3.client(
            "bedrock-runtime",
            region_name=os.environ.get("AWS_REGION", DEFAULT_REGION),
        )
    return _bedrock_client


def _reset_bedrock_client_for_test() -> None:
    """테스트에서 매 호출마다 boto3.client mock 이 새 인스턴스를 반환하도록 강제."""
    global _bedrock_client
    _bedrock_client = None


# ──────────────────────────────────────────────────────────────────────────────
# JSON 파싱 — Claude 응답에서 JSON 본문만 안전하게 뽑기
# ──────────────────────────────────────────────────────────────────────────────
_JSON_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*(?P<body>.*?)\s*```\s*$",
    re.DOTALL | re.IGNORECASE,
)


def _strip_code_fence(text: str) -> str:
    """```json ... ``` 코드블록 펜스를 벗긴다. 펜스가 없으면 원문."""
    m = _JSON_FENCE_RE.match(text.strip())
    if m:
        return m.group("body").strip()
    return text.strip()


def _parse_translation_json(raw: str) -> dict:
    """Claude 응답 텍스트에서 {title, content} dict 추출.

    - 코드블록 펜스 제거
    - JSON 파싱
    - title/content 키 존재 확인
    """
    if not raw or not raw.strip():
        raise TranslatorError("BEDROCK_ERROR", "Bedrock 응답이 비어있습니다.")

    body = _strip_code_fence(raw)
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        # 본문 어딘가에 JSON 객체가 박혀있을 수 있어 1회 더 시도 (느슨한 파싱).
        first_brace = body.find("{")
        last_brace = body.rfind("}")
        if first_brace != -1 and last_brace > first_brace:
            try:
                data = json.loads(body[first_brace : last_brace + 1])
            except json.JSONDecodeError as exc:
                logger.error("Failed to parse translation JSON: %r", raw)
                raise TranslatorError(
                    "BEDROCK_ERROR",
                    "Bedrock 응답을 JSON 으로 해석하지 못했습니다.",
                ) from exc
        else:
            logger.error("Failed to parse translation JSON: %r", raw)
            raise TranslatorError(
                "BEDROCK_ERROR",
                "Bedrock 응답을 JSON 으로 해석하지 못했습니다.",
            )

    if not isinstance(data, dict):
        raise TranslatorError(
            "BEDROCK_ERROR",
            "Bedrock 응답 JSON 이 객체 형태가 아닙니다.",
        )
    if "content" not in data:
        raise TranslatorError(
            "BEDROCK_ERROR",
            "Bedrock 응답 JSON 에 'content' 키가 없습니다.",
        )
    return data


# ──────────────────────────────────────────────────────────────────────────────
# 입력 검증
# ──────────────────────────────────────────────────────────────────────────────
def _validate_inputs(
    kind: str,
    public_id: Optional[str],
    title: Optional[str],
    content: Optional[str],
    source_lang: str,
    target_lang: str,
) -> None:
    if kind not in VALID_KINDS:
        raise TranslatorError(
            "INVALID_INPUT",
            f"kind 는 'post' 또는 'comment' 여야 합니다 (받은 값: {kind!r}).",
        )
    if not public_id or not isinstance(public_id, str):
        raise TranslatorError("INVALID_INPUT", "public_id 는 비어있을 수 없습니다.")
    if content is None or not isinstance(content, str) or not content.strip():
        raise TranslatorError("INVALID_INPUT", "content 는 비어있을 수 없습니다.")
    if kind == "post":
        if title is None or not isinstance(title, str) or not title.strip():
            raise TranslatorError(
                "INVALID_INPUT",
                "kind=post 일 때 title 은 비어있을 수 없습니다.",
            )
    else:  # comment
        if title is not None:
            raise TranslatorError(
                "INVALID_INPUT",
                "kind=comment 일 때 title 은 null 이어야 합니다.",
            )

    if not source_lang or not isinstance(source_lang, str):
        raise TranslatorError("INVALID_INPUT", "source_lang 가 필요합니다.")
    if not target_lang or not isinstance(target_lang, str):
        raise TranslatorError("INVALID_INPUT", "target_lang 가 필요합니다.")

    if source_lang not in SUPPORTED_LANGS:
        raise TranslatorError(
            "UNSUPPORTED_LANGUAGE",
            f"지원하지 않는 source_lang 입니다: {source_lang!r}.",
        )
    if target_lang not in SUPPORTED_LANGS:
        raise TranslatorError(
            "UNSUPPORTED_LANGUAGE",
            f"지원하지 않는 target_lang 입니다: {target_lang!r}.",
        )


# ──────────────────────────────────────────────────────────────────────────────
# 메인 진입점
# ──────────────────────────────────────────────────────────────────────────────
def translate(
    kind: str,
    public_id: str,
    title: Optional[str],
    content: str,
    source_lang: str,
    target_lang: str,
    model_id: Optional[str] = None,
) -> dict:
    """게시글/댓글 번역.

    Args:
        kind: "post" 또는 "comment"
        public_id: 대상 리소스의 public_id (로깅용)
        title: 원문 제목 (kind=post 일 때만, comment 면 None)
        content: 원문 본문
        source_lang: 원문 언어 코드 (ko/en/vi/fil ...)
        target_lang: 번역 대상 언어 코드
        model_id: Bedrock inference profile (기본은 환경변수 또는 Haiku)

    Returns:
        {
            "translated_title": Optional[str],
            "translated_content": str,
            "target_lang": str,
            "model_id": str,
            "input_tokens": int,
            "output_tokens": int,
        }

    Raises:
        TranslatorError: 입력 검증 실패, 미지원 언어, Bedrock 호출/파싱 실패.
    """
    _validate_inputs(kind, public_id, title, content, source_lang, target_lang)

    chosen_model = (
        model_id or os.environ.get("BEDROCK_MODEL_ID") or DEFAULT_MODEL_ID
    )

    # ── 같은 언어 — Bedrock 호출 없이 원문 그대로 ────────────────────────────
    if source_lang == target_lang:
        logger.info(
            "Skip translation (same lang): kind=%s public_id=%s lang=%s",
            kind, public_id, source_lang,
        )
        return {
            "translated_title": title if kind == "post" else None,
            "translated_content": content,
            "target_lang": target_lang,
            "model_id": chosen_model,
            "input_tokens": 0,
            "output_tokens": 0,
        }

    # ── Bedrock 호출 ────────────────────────────────────────────────────────
    system_prompt = build_translator_system_prompt(source_lang, target_lang, kind)
    user_payload = (
        json.dumps(
            {"title": title, "content": content},
            ensure_ascii=False,
        )
        if kind == "post"
        else json.dumps(
            {"content": content},
            ensure_ascii=False,
        )
    )

    logger.info(
        "Translate start: kind=%s public_id=%s %s→%s model=%s len(content)=%d",
        kind, public_id, source_lang, target_lang, chosen_model, len(content),
    )

    bedrock = _get_bedrock_client()
    try:
        response = bedrock.converse(
            modelId=chosen_model,
            messages=[
                {"role": "user", "content": [{"text": user_payload}]}
            ],
            system=[{"text": system_prompt}],
            inferenceConfig={
                "maxTokens": 4096,
                "temperature": 0.1,  # 번역은 사실 기반, 거의 결정적으로.
            },
        )
    except (ClientError, BotoCoreError) as exc:
        logger.error("Bedrock converse failed: %s", exc)
        raise TranslatorError(
            "BEDROCK_ERROR",
            f"Bedrock 호출에 실패했습니다: {type(exc).__name__}",
        ) from exc
    except Exception as exc:  # noqa: BLE001 — 모든 외부 예외를 한 코드로 매핑
        logger.error("Bedrock converse failed (unexpected): %s", exc)
        raise TranslatorError(
            "BEDROCK_ERROR",
            f"Bedrock 호출 중 예기치 못한 오류: {type(exc).__name__}",
        ) from exc

    # ── 응답 파싱 ────────────────────────────────────────────────────────────
    output = response.get("output", {}) or {}
    message = output.get("message", {}) or {}
    content_blocks = message.get("content", []) or []
    text_chunks = [
        blk.get("text", "")
        for blk in content_blocks
        if isinstance(blk, dict) and "text" in blk
    ]
    raw_text = "".join(text_chunks)
    parsed = _parse_translation_json(raw_text)

    usage = response.get("usage", {}) or {}
    input_tokens = int(usage.get("inputTokens", 0) or 0)
    output_tokens = int(usage.get("outputTokens", 0) or 0)

    translated_title = parsed.get("title") if kind == "post" else None
    translated_content = parsed.get("content", "")

    if not isinstance(translated_content, str) or not translated_content:
        raise TranslatorError(
            "BEDROCK_ERROR",
            "번역 결과의 content 가 비어있거나 문자열이 아닙니다.",
        )
    if kind == "post" and (
        translated_title is None
        or not isinstance(translated_title, str)
        or not translated_title.strip()
    ):
        raise TranslatorError(
            "BEDROCK_ERROR",
            "게시글 번역 결과의 title 이 비어있습니다.",
        )

    logger.info(
        "Translate done: kind=%s public_id=%s in=%d out=%d",
        kind, public_id, input_tokens, output_tokens,
    )

    return {
        "translated_title": translated_title,
        "translated_content": translated_content,
        "target_lang": target_lang,
        "model_id": chosen_model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
