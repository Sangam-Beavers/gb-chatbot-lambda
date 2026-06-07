"""
Unit tests for src/translator_handler.py.

translator.translate 를 monkeypatch 로 가린다.
Function URL 이벤트 페이로드 형태대로 핸들러를 직접 호출.
"""
import base64
import json

import pytest

import src.translator_handler as handler_mod
from src.translator import TranslatorError


def _event(body, *, is_base64=False, method="POST", path="/"):
    """Function URL v2 페이로드 모양의 이벤트를 만들어준다."""
    if isinstance(body, dict):
        body = json.dumps(body, ensure_ascii=False)
    if is_base64 and isinstance(body, str):
        body = base64.b64encode(body.encode("utf-8")).decode("ascii")
    return {
        "version": "2.0",
        "rawPath": path,
        "requestContext": {
            "http": {"method": method, "path": path},
        },
        "body": body,
        "isBase64Encoded": is_base64,
    }


# ──────────────────────────────────────────────────────────────────────────────
# 정상 케이스
# ──────────────────────────────────────────────────────────────────────────────
def test_handler_post_ok(monkeypatch):
    captured = {}

    def fake_translate(**kwargs):
        captured.update(kwargs)
        return {
            "translated_title": "Hello",
            "translated_content": "World",
            "target_lang": "en",
            "model_id": "global.anthropic.claude-haiku-4-5",
            "input_tokens": 10,
            "output_tokens": 20,
        }

    monkeypatch.setattr(handler_mod, "translate", fake_translate)

    event = _event({
        "kind": "post",
        "public_id": "p-uuid",
        "title": "안녕",
        "content": "세상",
        "source_lang": "ko",
        "target_lang": "en",
    })

    resp = handler_mod.handler(event, None)
    assert resp["statusCode"] == 200
    assert resp["headers"]["Content-Type"].startswith("application/json")

    body = json.loads(resp["body"])
    assert body["translated_title"] == "Hello"
    assert body["translated_content"] == "World"
    assert body["target_lang"] == "en"
    assert body["input_tokens"] == 10
    assert body["output_tokens"] == 20
    assert body["model_id"] == "global.anthropic.claude-haiku-4-5"

    # translate 인자가 그대로 전달됐는지
    assert captured["kind"] == "post"
    assert captured["public_id"] == "p-uuid"
    assert captured["title"] == "안녕"
    assert captured["content"] == "세상"
    assert captured["source_lang"] == "ko"
    assert captured["target_lang"] == "en"


def test_handler_comment_ok(monkeypatch):
    def fake_translate(**kwargs):
        assert kwargs["kind"] == "comment"
        assert kwargs["title"] is None
        return {
            "translated_title": None,
            "translated_content": "Cảm ơn",
            "target_lang": "vi",
            "model_id": "x",
            "input_tokens": 1,
            "output_tokens": 1,
        }

    monkeypatch.setattr(handler_mod, "translate", fake_translate)

    event = _event({
        "kind": "comment",
        "public_id": "c-uuid",
        "title": None,
        "content": "감사합니다",
        "source_lang": "ko",
        "target_lang": "vi",
    })
    resp = handler_mod.handler(event, None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["translated_title"] is None
    assert body["translated_content"] == "Cảm ơn"


def test_handler_accepts_base64_body(monkeypatch):
    def fake_translate(**kwargs):
        return {
            "translated_title": "T",
            "translated_content": "C",
            "target_lang": "en",
            "model_id": "m",
            "input_tokens": 0,
            "output_tokens": 0,
        }

    monkeypatch.setattr(handler_mod, "translate", fake_translate)
    event = _event({
        "kind": "post",
        "public_id": "p1",
        "title": "제목",
        "content": "본문",
        "source_lang": "ko",
        "target_lang": "en",
    }, is_base64=True)
    resp = handler_mod.handler(event, None)
    assert resp["statusCode"] == 200


# ──────────────────────────────────────────────────────────────────────────────
# 에러 매핑
# ──────────────────────────────────────────────────────────────────────────────
def test_handler_invalid_input_returns_400(monkeypatch):
    def fake_translate(**kwargs):
        raise TranslatorError("INVALID_INPUT", "title 누락")

    monkeypatch.setattr(handler_mod, "translate", fake_translate)
    event = _event({
        "kind": "post",
        "public_id": "p1",
        "title": None,
        "content": "본문",
        "source_lang": "ko",
        "target_lang": "en",
    })
    resp = handler_mod.handler(event, None)
    assert resp["statusCode"] == 400
    body = json.loads(resp["body"])
    assert body["error_code"] == "INVALID_INPUT"
    assert body["message"] == "title 누락"


def test_handler_unsupported_language_returns_400(monkeypatch):
    def fake_translate(**kwargs):
        raise TranslatorError("UNSUPPORTED_LANGUAGE", "지원 안함")

    monkeypatch.setattr(handler_mod, "translate", fake_translate)
    event = _event({
        "kind": "post",
        "public_id": "p1",
        "title": "t",
        "content": "c",
        "source_lang": "zz",
        "target_lang": "en",
    })
    resp = handler_mod.handler(event, None)
    assert resp["statusCode"] == 400
    body = json.loads(resp["body"])
    assert body["error_code"] == "UNSUPPORTED_LANGUAGE"


def test_handler_bedrock_error_returns_502(monkeypatch):
    def fake_translate(**kwargs):
        raise TranslatorError("BEDROCK_ERROR", "Throttled")

    monkeypatch.setattr(handler_mod, "translate", fake_translate)
    event = _event({
        "kind": "post",
        "public_id": "p1",
        "title": "t",
        "content": "c",
        "source_lang": "ko",
        "target_lang": "en",
    })
    resp = handler_mod.handler(event, None)
    assert resp["statusCode"] == 502
    body = json.loads(resp["body"])
    assert body["error_code"] == "BEDROCK_ERROR"


def test_handler_unexpected_error_maps_to_bedrock_error(monkeypatch):
    def fake_translate(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(handler_mod, "translate", fake_translate)
    event = _event({
        "kind": "post",
        "public_id": "p1",
        "title": "t",
        "content": "c",
        "source_lang": "ko",
        "target_lang": "en",
    })
    resp = handler_mod.handler(event, None)
    assert resp["statusCode"] == 502
    body = json.loads(resp["body"])
    assert body["error_code"] == "BEDROCK_ERROR"


# ──────────────────────────────────────────────────────────────────────────────
# body 파싱 예외
# ──────────────────────────────────────────────────────────────────────────────
def test_handler_empty_body_returns_400():
    event = _event(None)
    event["body"] = None
    resp = handler_mod.handler(event, None)
    assert resp["statusCode"] == 400
    body = json.loads(resp["body"])
    assert body["error_code"] == "INVALID_INPUT"


def test_handler_non_json_body_returns_400():
    event = _event("not a json {{{")
    resp = handler_mod.handler(event, None)
    assert resp["statusCode"] == 400
    body = json.loads(resp["body"])
    assert body["error_code"] == "INVALID_INPUT"


def test_handler_array_body_returns_400():
    """body 가 JSON 배열이면 INVALID_INPUT."""
    event = _event("[1,2,3]")
    resp = handler_mod.handler(event, None)
    assert resp["statusCode"] == 400
    body = json.loads(resp["body"])
    assert body["error_code"] == "INVALID_INPUT"


def test_handler_dict_body_passthrough(monkeypatch):
    """로컬 테스트 편의: body 가 이미 dict 이어도 동작."""
    monkeypatch.setattr(handler_mod, "translate", lambda **kwargs: {
        "translated_title": None,
        "translated_content": "ok",
        "target_lang": "en",
        "model_id": "m",
        "input_tokens": 0,
        "output_tokens": 0,
    })
    event = {
        "rawPath": "/",
        "requestContext": {"http": {"method": "POST"}},
        "body": {
            "kind": "comment",
            "public_id": "c1",
            "title": None,
            "content": "안녕",
            "source_lang": "ko",
            "target_lang": "en",
        },
        "isBase64Encoded": False,
    }
    resp = handler_mod.handler(event, None)
    assert resp["statusCode"] == 200


# ──────────────────────────────────────────────────────────────────────────────
# 응답 포맷 표준 검증 (Function URL 규약)
# ──────────────────────────────────────────────────────────────────────────────
def test_response_shape_is_function_url_compatible(monkeypatch):
    monkeypatch.setattr(handler_mod, "translate", lambda **kwargs: {
        "translated_title": "T",
        "translated_content": "C",
        "target_lang": "en",
        "model_id": "m",
        "input_tokens": 0,
        "output_tokens": 0,
    })
    event = _event({
        "kind": "post",
        "public_id": "p",
        "title": "t",
        "content": "c",
        "source_lang": "ko",
        "target_lang": "en",
    })
    resp = handler_mod.handler(event, None)
    # Function URL 응답 키
    assert set(resp.keys()) >= {"statusCode", "headers", "body"}
    assert isinstance(resp["statusCode"], int)
    assert isinstance(resp["body"], str)  # body 는 반드시 문자열
    assert resp["headers"]["Content-Type"].startswith("application/json")


def test_lambda_handler_alias_exists():
    """AWS Lambda 가 기본으로 찾는 lambda_handler 별칭이 살아있어야 한다."""
    assert handler_mod.lambda_handler is handler_mod.handler
