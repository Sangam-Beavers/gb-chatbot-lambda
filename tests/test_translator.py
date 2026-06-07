"""
Unit tests for src/translator.py.

Bedrock 클라이언트는 boto3.client 를 monkeypatch 로 mock 한다.
실제 AWS 호출은 발생하지 않는다.
"""
import json
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

import src.translator as translator_mod
from src.translator import TranslatorError, translate


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
def _make_bedrock_response(text: str, input_tokens: int = 10, output_tokens: int = 20) -> dict:
    """Bedrock converse 응답 형식의 dict 를 만들어준다."""
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": text}],
            }
        },
        "usage": {
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "totalTokens": input_tokens + output_tokens,
        },
        "stopReason": "end_turn",
    }


@pytest.fixture(autouse=True)
def _reset_client():
    """매 테스트마다 모듈 캐시된 bedrock 클라이언트를 비운다."""
    translator_mod._reset_bedrock_client_for_test()
    yield
    translator_mod._reset_bedrock_client_for_test()


@pytest.fixture
def mock_bedrock(monkeypatch):
    """boto3.client('bedrock-runtime') 를 MagicMock 으로 가린다."""
    mock_client = MagicMock()

    def _fake_boto_client(service_name, region_name=None, **kwargs):
        assert service_name == "bedrock-runtime"
        return mock_client

    monkeypatch.setattr(translator_mod.boto3, "client", _fake_boto_client)
    return mock_client


# ──────────────────────────────────────────────────────────────────────────────
# 정상 케이스
# ──────────────────────────────────────────────────────────────────────────────
def test_translate_post_ok(mock_bedrock):
    mock_bedrock.converse.return_value = _make_bedrock_response(
        json.dumps({"title": "Lương tháng", "content": "Tôi nhận lương 2 triệu won."}, ensure_ascii=False),
        input_tokens=42,
        output_tokens=58,
    )

    result = translate(
        kind="post",
        public_id="post-uuid-1",
        title="월급 이야기",
        content="월급 200만원을 받았습니다.",
        source_lang="ko",
        target_lang="vi",
    )

    assert result["translated_title"] == "Lương tháng"
    assert result["translated_content"] == "Tôi nhận lương 2 triệu won."
    assert result["target_lang"] == "vi"
    assert result["input_tokens"] == 42
    assert result["output_tokens"] == 58
    assert "haiku" in result["model_id"] or result["model_id"]  # 환경변수 미주입이면 기본값
    mock_bedrock.converse.assert_called_once()

    # system prompt 에 source/target 언어 명시 확인
    call_kwargs = mock_bedrock.converse.call_args.kwargs
    system_text = call_kwargs["system"][0]["text"]
    assert "Korean" in system_text
    assert "Vietnamese" in system_text


def test_translate_comment_ok(mock_bedrock):
    mock_bedrock.converse.return_value = _make_bedrock_response(
        json.dumps({"title": None, "content": "Cảm ơn bạn!"}, ensure_ascii=False),
    )

    result = translate(
        kind="comment",
        public_id="comment-uuid-1",
        title=None,
        content="감사합니다!",
        source_lang="ko",
        target_lang="vi",
    )

    assert result["translated_title"] is None
    assert result["translated_content"] == "Cảm ơn bạn!"
    assert result["target_lang"] == "vi"


def test_translate_post_with_code_fence_response(mock_bedrock):
    """Claude 가 ```json ... ``` 로 감싸 응답해도 안전 파싱."""
    json_body = json.dumps({"title": "Hello", "content": "World"}, ensure_ascii=False)
    fenced = f"```json\n{json_body}\n```"
    mock_bedrock.converse.return_value = _make_bedrock_response(fenced)

    result = translate(
        kind="post",
        public_id="p1",
        title="안녕",
        content="세상",
        source_lang="ko",
        target_lang="en",
    )

    assert result["translated_title"] == "Hello"
    assert result["translated_content"] == "World"


def test_translate_post_with_leading_explanation_recovers(mock_bedrock):
    """JSON 앞뒤로 설명이 붙어도 brace 범위로 복구 시도."""
    raw = (
        "여기 번역 결과 입니다:\n"
        + json.dumps({"title": "T", "content": "C"}, ensure_ascii=False)
        + "\n끝."
    )
    mock_bedrock.converse.return_value = _make_bedrock_response(raw)

    result = translate(
        kind="post",
        public_id="p1",
        title="제목",
        content="본문",
        source_lang="ko",
        target_lang="en",
    )
    assert result["translated_title"] == "T"
    assert result["translated_content"] == "C"


# ──────────────────────────────────────────────────────────────────────────────
# 같은 언어 — Bedrock 호출 0회
# ──────────────────────────────────────────────────────────────────────────────
def test_translate_same_lang_skips_bedrock(mock_bedrock):
    result = translate(
        kind="post",
        public_id="p1",
        title="제목",
        content="본문",
        source_lang="ko",
        target_lang="ko",
    )
    assert result["translated_title"] == "제목"
    assert result["translated_content"] == "본문"
    assert result["target_lang"] == "ko"
    assert result["input_tokens"] == 0
    assert result["output_tokens"] == 0
    mock_bedrock.converse.assert_not_called()


def test_translate_same_lang_comment_skips_bedrock(mock_bedrock):
    result = translate(
        kind="comment",
        public_id="c1",
        title=None,
        content="감사합니다",
        source_lang="vi",
        target_lang="vi",
    )
    assert result["translated_title"] is None
    assert result["translated_content"] == "감사합니다"
    mock_bedrock.converse.assert_not_called()


# ──────────────────────────────────────────────────────────────────────────────
# 입력 검증 실패
# ──────────────────────────────────────────────────────────────────────────────
def test_invalid_kind_raises(mock_bedrock):
    with pytest.raises(TranslatorError) as ei:
        translate(
            kind="invalid",
            public_id="p1",
            title="t",
            content="c",
            source_lang="ko",
            target_lang="en",
        )
    assert ei.value.error_code == "INVALID_INPUT"
    mock_bedrock.converse.assert_not_called()


def test_post_missing_title_raises(mock_bedrock):
    with pytest.raises(TranslatorError) as ei:
        translate(
            kind="post",
            public_id="p1",
            title=None,
            content="본문",
            source_lang="ko",
            target_lang="en",
        )
    assert ei.value.error_code == "INVALID_INPUT"
    mock_bedrock.converse.assert_not_called()


def test_comment_with_title_raises(mock_bedrock):
    with pytest.raises(TranslatorError) as ei:
        translate(
            kind="comment",
            public_id="c1",
            title="제목있음",
            content="본문",
            source_lang="ko",
            target_lang="en",
        )
    assert ei.value.error_code == "INVALID_INPUT"


def test_empty_content_raises(mock_bedrock):
    with pytest.raises(TranslatorError) as ei:
        translate(
            kind="post",
            public_id="p1",
            title="제목",
            content="",
            source_lang="ko",
            target_lang="en",
        )
    assert ei.value.error_code == "INVALID_INPUT"


# ──────────────────────────────────────────────────────────────────────────────
# 미지원 언어
# ──────────────────────────────────────────────────────────────────────────────
def test_unsupported_source_lang_raises(mock_bedrock):
    with pytest.raises(TranslatorError) as ei:
        translate(
            kind="post",
            public_id="p1",
            title="제목",
            content="본문",
            source_lang="zz",
            target_lang="vi",
        )
    assert ei.value.error_code == "UNSUPPORTED_LANGUAGE"
    mock_bedrock.converse.assert_not_called()


def test_unsupported_target_lang_raises(mock_bedrock):
    with pytest.raises(TranslatorError) as ei:
        translate(
            kind="post",
            public_id="p1",
            title="제목",
            content="본문",
            source_lang="ko",
            target_lang="xx",
        )
    assert ei.value.error_code == "UNSUPPORTED_LANGUAGE"


# ──────────────────────────────────────────────────────────────────────────────
# Bedrock 호출 실패
# ──────────────────────────────────────────────────────────────────────────────
def test_bedrock_client_error_maps_to_bedrock_error(mock_bedrock):
    mock_bedrock.converse.side_effect = ClientError(
        error_response={"Error": {"Code": "ThrottlingException", "Message": "Slow down"}},
        operation_name="Converse",
    )
    with pytest.raises(TranslatorError) as ei:
        translate(
            kind="post",
            public_id="p1",
            title="제목",
            content="본문",
            source_lang="ko",
            target_lang="vi",
        )
    assert ei.value.error_code == "BEDROCK_ERROR"


def test_bedrock_unexpected_error_maps_to_bedrock_error(mock_bedrock):
    mock_bedrock.converse.side_effect = RuntimeError("boom")
    with pytest.raises(TranslatorError) as ei:
        translate(
            kind="comment",
            public_id="c1",
            title=None,
            content="안녕",
            source_lang="ko",
            target_lang="vi",
        )
    assert ei.value.error_code == "BEDROCK_ERROR"


# ──────────────────────────────────────────────────────────────────────────────
# JSON 파싱 실패
# ──────────────────────────────────────────────────────────────────────────────
def test_non_json_response_maps_to_bedrock_error(mock_bedrock):
    mock_bedrock.converse.return_value = _make_bedrock_response(
        "이것은 그냥 평문이고 JSON 이 아닙니다."
    )
    with pytest.raises(TranslatorError) as ei:
        translate(
            kind="post",
            public_id="p1",
            title="제목",
            content="본문",
            source_lang="ko",
            target_lang="vi",
        )
    assert ei.value.error_code == "BEDROCK_ERROR"


def test_response_missing_content_key_maps_to_bedrock_error(mock_bedrock):
    mock_bedrock.converse.return_value = _make_bedrock_response(
        json.dumps({"title": "T"}, ensure_ascii=False),  # content 없음
    )
    with pytest.raises(TranslatorError) as ei:
        translate(
            kind="post",
            public_id="p1",
            title="제목",
            content="본문",
            source_lang="ko",
            target_lang="vi",
        )
    assert ei.value.error_code == "BEDROCK_ERROR"


def test_post_response_missing_title_maps_to_bedrock_error(mock_bedrock):
    """게시글 번역인데 title 이 비어있으면 BEDROCK_ERROR (응답 품질 문제)."""
    mock_bedrock.converse.return_value = _make_bedrock_response(
        json.dumps({"title": "", "content": "본문 번역"}, ensure_ascii=False),
    )
    with pytest.raises(TranslatorError) as ei:
        translate(
            kind="post",
            public_id="p1",
            title="제목",
            content="본문",
            source_lang="ko",
            target_lang="vi",
        )
    assert ei.value.error_code == "BEDROCK_ERROR"


def test_empty_response_maps_to_bedrock_error(mock_bedrock):
    mock_bedrock.converse.return_value = _make_bedrock_response("")
    with pytest.raises(TranslatorError) as ei:
        translate(
            kind="comment",
            public_id="c1",
            title=None,
            content="안녕",
            source_lang="ko",
            target_lang="vi",
        )
    assert ei.value.error_code == "BEDROCK_ERROR"


# ──────────────────────────────────────────────────────────────────────────────
# 환경변수로 모델 ID 주입
# ──────────────────────────────────────────────────────────────────────────────
def test_env_model_id_is_respected(mock_bedrock, monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "apac.anthropic.claude-haiku-test")
    mock_bedrock.converse.return_value = _make_bedrock_response(
        json.dumps({"title": "T", "content": "C"}, ensure_ascii=False),
    )

    result = translate(
        kind="post",
        public_id="p1",
        title="제목",
        content="본문",
        source_lang="ko",
        target_lang="en",
    )
    assert result["model_id"] == "apac.anthropic.claude-haiku-test"
    call_kwargs = mock_bedrock.converse.call_args.kwargs
    assert call_kwargs["modelId"] == "apac.anthropic.claude-haiku-test"
