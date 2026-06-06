"""
Chatbot core — Bedrock converse_stream + Tool Use loop.

Phase 4 Step 2: Tool Use 루프 추가. 도구(MCP1 환율 + KB 법령) 호출 가능.
LLM이 도구 호출 결정 → 우리 코드가 실행 → 결과 LLM에게 전달 → 최종 답변.

References:
- AWS Bedrock Tool Use: https://docs.aws.amazon.com/bedrock/latest/userguide/tool-use.html
- Inference profile (Sonnet 4.6): global.anthropic.claude-sonnet-4-6
"""
import json
import logging
import os
import time
from typing import Generator, Optional

import boto3

from src.storage import build_context_turns
from src.system_prompt import build_system_prompt
from src.tools import TOOLS, execute_tool

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# 2026-05-29: Sonnet 4 APAC이 Legacy 마킹 받아 Sonnet 4.6 (global) 으로 교체
DEFAULT_MODEL_ID = "global.anthropic.claude-sonnet-4-6"
DEFAULT_REGION = "ap-northeast-2"

# Tool Use 루프 최대 반복 횟수 (도구 4번까지 + 마지막 end_turn = 5)
MAX_TOOL_USE_ITERATIONS = 5

# 슬라이딩 윈도우 — Bedrock에 보내는 messages 상한(≈20턴). 합성 요약 2턴은 고정 보존.
# 정본 §8/§10 — Summary Worker 확장 전 데모 처리.
MAX_WINDOW_MESSAGES = 40

# 합성 요약 턴 식별 마커 (storage.build_context_turns와 동일 텍스트)
_CONTEXT_MARKER = "[분석된 계약서 요약]"

# 시뮬레이션 스트리밍 지연 (per character, 영상에서 점진 표시 효과)
STREAM_CHAR_DELAY = 0.005


def _get_bedrock_client():
    return boto3.client(
        "bedrock-runtime",
        region_name=os.environ.get("AWS_REGION", DEFAULT_REGION),
    )


def _is_plain_user_text(msg: dict) -> bool:
    """일반 user 텍스트 메시지인가 — toolResult(role=user지만 content가 toolResult 블록)는 제외."""
    if msg.get("role") != "user":
        return False
    content = msg.get("content") or []
    return bool(content) and "text" in content[0]


def _apply_sliding_window(messages: list) -> None:
    """messages가 상한을 넘으면 최근 턴만 남긴다 — **in-place** 수정.

    in-place인 이유: app.py가 같은 리스트 참조를 들고 있다가 턴 종료 후 Redis에 캐시하므로,
    새 리스트로 갈아끼우면 이후 누적 턴이 캐시에서 빠진다.

    규칙(정본 §8):
    - 합성 요약 2턴(맨 앞, [분석된 계약서 요약] 마커)은 고정 보존 — 분석 맥락 유실 방지.
    - 잘린 구간 시작이 toolResult/assistant면 일반 user 텍스트 메시지까지 전진해
      toolUse↔toolResult pair가 깨지지 않게 한다(깨지면 Bedrock 400).
    """
    if len(messages) <= MAX_WINDOW_MESSAGES:
        return

    pinned = []
    rest = messages
    first_text = (messages[0].get("content") or [{}])[0].get("text", "") if messages else ""
    if _CONTEXT_MARKER in first_text:
        pinned = messages[:2]
        rest = messages[2:]

    tail = rest[len(rest) - (MAX_WINDOW_MESSAGES - len(pinned)):]
    while tail and not _is_plain_user_text(tail[0]):
        tail = tail[1:]
    if not tail:  # 비정상적으로 긴 단일 턴 — 최소한 마지막 메시지는 유지
        tail = rest[-1:]

    trimmed = pinned + tail
    logger.info("Sliding window applied: %d → %d messages", len(messages), len(trimmed))
    messages[:] = trimmed


def chat_once(
    message: str,
    analysis_summary: Optional[str] = None,
    user_lang: str = "ko",
    model_id: Optional[str] = None,
    messages: Optional[list] = None,
    tools_used_out: Optional[list] = None,
) -> Generator[str, None, None]:
    """
    Stream tokens for a single user message, with optional tool use.

    Args:
        message: 사용자 질문
        analysis_summary: 분석 요약 (첫 turn에만 주입 — messages 미전달 시에만 사용)
        user_lang: 답변 언어 코드 (데모는 'ko')
        model_id: Bedrock inference profile ID (기본 Sonnet 4.6)
        messages: 복원된 스레드 (storage.load_thread 결과). 전달되면 이 위에 user 질문을
            누적하고 analysis_summary는 무시한다 — 요약은 이미 스레드 안에 있다(§3-2).
            None이면 단일턴 모드(로컬 테스트 호환): 요약 합성 2턴부터 새로 조립.
        tools_used_out: 이번 턴에 호출된 도구명을 담아 돌려줄 리스트 (저장용 메타, 선택)

    Yields:
        텍스트 토큰 (글자 단위, end_turn 턴의 텍스트만 흘림 - R2)

    Flow:
        Iteration N:
          ① converse_stream 호출 (도구 명세 포함)
          ② 응답 끝까지 수집 (text + toolUse 블록)
          ③ stopReason 확인:
             - "end_turn" → 텍스트 yield (사용자에게 흘림) → break
             - "tool_use" → 도구 실행 → 결과 messages에 추가 → 다음 iteration
    """
    bedrock = _get_bedrock_client()
    chosen_model = (
        model_id or os.environ.get("BEDROCK_MODEL_ID") or DEFAULT_MODEL_ID
    )

    # ──────────────────────────────────────────────────────────────────────────
    # messages 조립 — 복원 스레드(load_thread) 위에 누적하거나, 단일턴 모드로 새로 조립
    # ──────────────────────────────────────────────────────────────────────────
    if messages is None:
        messages = build_context_turns(analysis_summary) if analysis_summary else []

    messages.append({
        "role": "user",
        "content": [{"text": message}]
    })

    # 긴 스레드는 최근 턴만 — 합성 요약 고정 + 윈도우 (in-place, §8)
    _apply_sliding_window(messages)

    logger.info(
        "Chat start: model=%s, history_turns=%d, message_len=%d",
        chosen_model, len(messages) - 1, len(message),
    )

    # ──────────────────────────────────────────────────────────────────────────
    # Tool Use 루프
    # ──────────────────────────────────────────────────────────────────────────
    for iteration in range(MAX_TOOL_USE_ITERATIONS):
        logger.info("Tool use loop iteration %d", iteration + 1)

        response = bedrock.converse_stream(
            modelId=chosen_model,
            messages=messages,
            system=[{"text": build_system_prompt(user_lang)}],
            toolConfig={"tools": TOOLS},
            inferenceConfig={
                "maxTokens": 2048,
                "temperature": 0.3,  # 사실 기반 답변, 보수적
            },
        )

        # 스트림을 끝까지 받아서 블록 단위로 재구성
        content_blocks = []
        current_block: Optional[dict] = None
        stop_reason: Optional[str] = None

        for event in response["stream"]:
            if "contentBlockStart" in event:
                start = event["contentBlockStart"]["start"]
                if "toolUse" in start:
                    current_block = {
                        "type": "toolUse",
                        "toolUseId": start["toolUse"]["toolUseId"],
                        "name": start["toolUse"]["name"],
                        "input_json": "",
                    }
                else:
                    # 텍스트 블록 시작 (start에 type 필드 없음)
                    current_block = {"type": "text", "text": ""}

            elif "contentBlockDelta" in event:
                delta = event["contentBlockDelta"]["delta"]
                if "text" in delta:
                    if current_block is None:
                        # contentBlockStart 누락 케이스 — 텍스트 블록 신규 생성
                        current_block = {"type": "text", "text": ""}
                    if current_block.get("type") == "text":
                        current_block["text"] += delta["text"]
                elif "toolUse" in delta:
                    if current_block and current_block.get("type") == "toolUse":
                        current_block["input_json"] += delta["toolUse"].get("input", "")

            elif "contentBlockStop" in event:
                if current_block:
                    if current_block["type"] == "toolUse":
                        try:
                            current_block["input"] = (
                                json.loads(current_block["input_json"])
                                if current_block["input_json"] else {}
                            )
                        except json.JSONDecodeError:
                            logger.warning(
                                "Failed to parse tool input JSON: %r",
                                current_block["input_json"],
                            )
                            current_block["input"] = {}
                    content_blocks.append(current_block)
                    current_block = None

            elif "messageStop" in event:
                stop_reason = event["messageStop"].get("stopReason")

            elif "metadata" in event:
                usage = event["metadata"].get("usage", {})
                logger.info(
                    "Iteration %d done: stopReason=%s, in=%s, out=%s",
                    iteration + 1, stop_reason,
                    usage.get("inputTokens"), usage.get("outputTokens"),
                )

        # ──────────────────────────────────────────────────────────────────
        # assistant 메시지 재구성 → messages 누적
        # ──────────────────────────────────────────────────────────────────
        assistant_content = []
        for cb in content_blocks:
            if cb["type"] == "text" and cb["text"]:
                assistant_content.append({"text": cb["text"]})
            elif cb["type"] == "toolUse":
                assistant_content.append({
                    "toolUse": {
                        "toolUseId": cb["toolUseId"],
                        "name": cb["name"],
                        "input": cb["input"],
                    }
                })

        if assistant_content:
            messages.append({"role": "assistant", "content": assistant_content})

        # ──────────────────────────────────────────────────────────────────
        # 종료 조건 + 분기
        # ──────────────────────────────────────────────────────────────────
        if stop_reason == "end_turn":
            # R2: end_turn 턴의 텍스트만 사용자에게 흘림
            for cb in content_blocks:
                if cb["type"] == "text":
                    for char in cb["text"]:
                        yield char
                        if STREAM_CHAR_DELAY > 0:
                            time.sleep(STREAM_CHAR_DELAY)
            return

        if stop_reason == "tool_use":
            # 도구 실행 + 결과를 messages에 추가 → 다음 iteration
            tool_results = []
            for cb in content_blocks:
                if cb["type"] == "toolUse":
                    logger.info(
                        "Tool call: name=%s input=%s",
                        cb["name"], cb["input"],
                    )
                    if tools_used_out is not None:
                        tools_used_out.append(cb["name"])
                    result_text = execute_tool(cb["name"], cb["input"])
                    tool_results.append({
                        "toolResult": {
                            "toolUseId": cb["toolUseId"],
                            "content": [{"text": result_text}],
                        }
                    })

            if tool_results:
                messages.append({"role": "user", "content": tool_results})
                continue
            else:
                # tool_use라고 했는데 toolUse 블록이 없음 — 비정상
                logger.warning("stopReason=tool_use but no toolUse blocks found")
                return

        # 그 외 stop reason (max_tokens, stop_sequence 등) — 누적 텍스트만 yield
        logger.warning("Unexpected stopReason: %s", stop_reason)
        for cb in content_blocks:
            if cb["type"] == "text":
                for char in cb["text"]:
                    yield char
                    if STREAM_CHAR_DELAY > 0:
                        time.sleep(STREAM_CHAR_DELAY)
        return

    # 루프 한도 초과 — 안전 메시지
    logger.warning("Tool use loop exceeded max iterations (%d)", MAX_TOOL_USE_ITERATIONS)
    yield "\n\n[죄송합니다. 답변 생성에 시간이 너무 오래 걸려 중단되었습니다.]\n"
