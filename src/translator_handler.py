"""
Translator Lambda 진입점 — Function URL + IAM Auth, 동기 JSON 응답.

챗봇 Lambda(`handler.py`) 와 달리:
  - Response Streaming 안 함. 일반 `return` 으로 JSON 응답.
  - Tool Use 안 씀. translator.translate() 1회 호출.

배포:
  - 함수명: gb-community-translator-{dev|stage|prod}
  - 환경변수: BEDROCK_MODEL_ID, AWS_REGION (Lambda 자동 주입)
  - 인증: IAM SigV4 (Spring 백엔드가 서명해서 호출)

요청 (event["body"], JSON 문자열):
  {
    "kind": "post" | "comment",
    "public_id": "uuid",
    "title": "원문 제목 또는 null",
    "content": "원문 본문",
    "source_lang": "ko",
    "target_lang": "vi"
  }

응답 (성공, 200):
  {
    "translated_title": "...",
    "translated_content": "...",
    "target_lang": "vi",
    "model_id": "...",
    "input_tokens": 0,
    "output_tokens": 0
  }

응답 (실패, 4xx/5xx):
  {"error_code": "INVALID_INPUT" | "UNSUPPORTED_LANGUAGE" | "BEDROCK_ERROR", "message": "..."}
"""
import base64
import json
import logging
from typing import Any

from src.translator import TranslatorError, translate

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# 에러코드 → HTTP status 매핑.
# - INVALID_INPUT / UNSUPPORTED_LANGUAGE: 사용자 입력 잘못 → 400
# - BEDROCK_ERROR: 우리/AWS 측 문제 → 502
_ERROR_STATUS = {
    "INVALID_INPUT": 400,
    "UNSUPPORTED_LANGUAGE": 400,
    "BEDROCK_ERROR": 502,
}


def _json_response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": json.dumps(body, ensure_ascii=False),
    }


def _error_response(error_code: str, message: str) -> dict:
    status = _ERROR_STATUS.get(error_code, 500)
    return _json_response(status, {"error_code": error_code, "message": message})


def _parse_body(event: dict) -> dict:
    """Function URL 이벤트의 body 를 dict 로 파싱.

    - body 가 None/빈 문자열 → INVALID_INPUT
    - isBase64Encoded=True 면 base64 디코드
    - 이미 dict 면 그대로 (로컬 테스트 편의)
    """
    raw = event.get("body")
    if isinstance(raw, dict):
        return raw
    if raw is None or raw == "":
        raise TranslatorError("INVALID_INPUT", "요청 body 가 비어있습니다.")
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw).decode("utf-8")
        except Exception as exc:  # noqa: BLE001
            raise TranslatorError(
                "INVALID_INPUT",
                f"base64 디코드 실패: {type(exc).__name__}",
            ) from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TranslatorError(
            "INVALID_INPUT",
            f"요청 body 가 JSON 이 아닙니다: {exc.msg}",
        ) from exc


def handler(event: dict, context: Any = None) -> dict:
    """Lambda Function URL 진입점.

    Args:
        event: Function URL 이벤트 (v2 페이로드)
        context: Lambda 컨텍스트 (사용 안 함)

    Returns:
        Function URL 표준 응답 dict (statusCode/headers/body).
    """
    logger.info(
        "Translator invoked: path=%s method=%s",
        event.get("rawPath"),
        (event.get("requestContext") or {}).get("http", {}).get("method"),
    )

    try:
        body = _parse_body(event)
    except TranslatorError as exc:
        return _error_response(exc.error_code, exc.message)

    if not isinstance(body, dict):
        return _error_response("INVALID_INPUT", "요청 body 는 JSON 객체여야 합니다.")

    # 필수 키 (translator 가 한번 더 검증하지만, KeyError 방지 위해 get 으로 꺼냄)
    kind = body.get("kind")
    public_id = body.get("public_id")
    title = body.get("title")  # comment 면 None
    content = body.get("content")
    source_lang = body.get("source_lang")
    target_lang = body.get("target_lang")

    try:
        result = translate(
            kind=kind,
            public_id=public_id,
            title=title,
            content=content,
            source_lang=source_lang,
            target_lang=target_lang,
        )
    except TranslatorError as exc:
        logger.warning(
            "Translator error: code=%s message=%s public_id=%s",
            exc.error_code, exc.message, public_id,
        )
        return _error_response(exc.error_code, exc.message)
    except Exception as exc:  # noqa: BLE001 — 마지막 안전망
        logger.exception("Unexpected error: %s", exc)
        return _error_response(
            "BEDROCK_ERROR",
            f"예기치 못한 오류: {type(exc).__name__}",
        )

    return _json_response(200, result)


# AWS Lambda 가 기본으로 찾는 이름과의 호환을 위해 alias 도 제공.
lambda_handler = handler
