"""
Chatbot core — Bedrock converse_stream call.

Phase 4 Step 1 — no tool use yet. Answer based on analysis_summary alone.
Step 2 will add tool use loop + MCP client calls.

References:
- AWS Bedrock Converse API: https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html
- Inference profile (APAC routing): apac.anthropic.claude-sonnet-4-20250514-v1:0
"""
import logging
import os
from typing import Generator, Optional

import boto3

from src.system_prompt import build_system_prompt

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Phase 4 사전 조사(R3) 결정: APAC routing — 한국 사용자 latency 가장 낮음 + Tool Use 지원
DEFAULT_MODEL_ID = "apac.anthropic.claude-sonnet-4-20250514-v1:0"
DEFAULT_REGION = "ap-northeast-2"


def _get_bedrock_client():
    """Bedrock Runtime client. Recreated per Lambda cold start."""
    return boto3.client(
        "bedrock-runtime",
        region_name=os.environ.get("AWS_REGION", DEFAULT_REGION),
    )


def chat_once(
    message: str,
    analysis_summary: Optional[str] = None,
    user_lang: str = "ko",
    model_id: Optional[str] = None,
) -> Generator[str, None, None]:
    """
    Stream tokens for a single user message.

    Phase 4 Step 1: no tool use. Answer based on analysis_summary context only.

    Args:
        message: user question (e.g. "이 계약서 어때?")
        analysis_summary: contract analysis summary, injected on first turn only.
                         Format defined in result-json-schema-agreement.md §6.
        user_lang: answer language code. Demo uses 'ko'.
        model_id: Bedrock inference profile ID. Defaults to env BEDROCK_MODEL_ID
                 or DEFAULT_MODEL_ID.

    Yields:
        Text tokens (one character to a few characters per yield).

    Raises:
        botocore.exceptions.ClientError on Bedrock API errors (wrong model ID,
        insufficient IAM permissions, throttling, etc.).
    """
    bedrock = _get_bedrock_client()
    chosen_model = (
        model_id
        or os.environ.get("BEDROCK_MODEL_ID")
        or DEFAULT_MODEL_ID
    )

    # Build message history
    # On first turn, inject analysis_summary as a synthetic user+assistant pair
    # so the model treats it as established context. (ai-chatbot-mcp.md §3-2)
    messages = []
    if analysis_summary:
        messages.append({
            "role": "user",
            "content": [{
                "text": (
                    f"[분석된 계약서 요약]\n"
                    f"{analysis_summary}\n\n"
                    f"위 계약서에 대해 질문하겠습니다."
                )
            }]
        })
        messages.append({
            "role": "assistant",
            "content": [{"text": "네, 확인했습니다. 궁금한 점을 물어보세요."}]
        })

    messages.append({
        "role": "user",
        "content": [{"text": message}]
    })

    logger.info(
        "Bedrock converse_stream: model=%s, has_summary=%s, message_len=%d",
        chosen_model, analysis_summary is not None, len(message),
    )

    response = bedrock.converse_stream(
        modelId=chosen_model,
        messages=messages,
        system=[{"text": build_system_prompt(user_lang)}],
        inferenceConfig={
            "maxTokens": 2048,
            "temperature": 0.3,  # 사실 기반 답변이라 보수적
        },
    )

    # Stream events:
    #   messageStart           — generation begins
    #   contentBlockDelta      — text token chunks (we yield these)
    #   contentBlockStop       — end of one content block
    #   messageStop            — stopReason: end_turn / max_tokens / stop_sequence
    #   metadata               — usage stats
    stop_reason = None
    for event in response["stream"]:
        if "contentBlockDelta" in event:
            delta = event["contentBlockDelta"]["delta"]
            if "text" in delta:
                yield delta["text"]
        elif "messageStop" in event:
            stop_reason = event["messageStop"].get("stopReason")
        elif "metadata" in event:
            usage = event["metadata"].get("usage", {})
            logger.info(
                "Bedrock done: stopReason=%s, inputTokens=%s, outputTokens=%s",
                stop_reason,
                usage.get("inputTokens"),
                usage.get("outputTokens"),
            )
