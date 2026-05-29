"""
Local test script — call chatbot.chat_once directly without Lambda runtime.

Use this to verify Bedrock connection + converse_stream + token streaming
before deploying to AWS Lambda.

Requirements:
    - AWS credentials configured (~/.aws/credentials, env vars, or IAM role)
    - Bedrock model access enabled on the account
    - The inference profile apac.anthropic.claude-sonnet-4-20250514-v1:0 must be active

Usage:
    cd gb-chatbot-lambda
    python -m tests.local_run

If you want to test a different scenario, edit MESSAGE / ANALYSIS_SUMMARY below.
"""
import logging
import sys

from src.chatbot import chat_once


# Set up clear logging so we can see Bedrock errors easily
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


# ──────────────────────────────────────────────────────────────────────────────
# Scenario 1 — "Is this contract OK?"  (no tool use needed)
# ──────────────────────────────────────────────────────────────────────────────
# The analysis_summary is what would normally come from MySQL document_results
# in production. Hardcoded here for local testing.
ANALYSIS_SUMMARY = (
    "위험도 HIGH. "
    "최저임금 미달(시급 9,620원 기준 미충족), 주 50시간 초과근무 조항 존재. "
    "임금: 월 2,000,000원 / 시급 9,620원. "
    "문서유형: 근로계약서."
)

MESSAGE = "이 계약서 어때?"


def main() -> int:
    print(f"\n── 시나리오 1 — 분석 요약 보고 답변 ──\n")
    print(f"분석 요약: {ANALYSIS_SUMMARY}\n")
    print(f"사용자: {MESSAGE}\n")
    print(f"챗봇: ", end="", flush=True)

    try:
        for token in chat_once(
            message=MESSAGE,
            analysis_summary=ANALYSIS_SUMMARY,
            user_lang="ko",
        ):
            print(token, end="", flush=True)
        print("\n")
        return 0
    except Exception as e:
        print(f"\n\n[에러] {type(e).__name__}: {e}\n", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
