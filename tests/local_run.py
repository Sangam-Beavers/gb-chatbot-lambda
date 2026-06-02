"""
Local test script — run multiple scenarios against the chatbot.

시나리오 5개 차례로 실행:
  ① 도구 호출 없음 — "이 계약서 어때?"
  ② MCP1 환율 도구 — "월급 베트남 돈으로 얼마야?"
  ③ KB 법령 도구 — "외국인도 최저임금 미달이 불법이야?"
  ④ MCP2 커뮤니티 도구 — "비슷한 경험 한 사람 있나?"
  ⑤ MCP3 Tavily 웹 검색 (외부 회사 직접 연결) — "올해 한국 외국인 최저임금 얼마야?"

Requirements:
    - AWS credentials configured (gb-account-b 프로필 또는 default)
    - Bedrock model access enabled (Sonnet 4.6)
    - KB ID KENYUCA5DE available (or set LEGAL_KB_ID env var)
    - MCP1 환율 서버가 로컬에 떠있어야 함 (시나리오 ② 통과 위해)
        cd ../gb-mcp-servers/mcp-exchange
        source .venv/Scripts/activate
        export REDIS_HOST=10.10.1.194 REDIS_PORT=6379 REDIS_PASSWORD=sbredis1234
        python server.py    # 8000 포트
    - MCP2 커뮤니티 서버가 로컬에 떠있어야 함 (시나리오 ④ 통과 위해)
        cd ../gb-mcp-servers/mcp-community
        source .venv/Scripts/activate
        export COMMUNITY_DB_HOST=10.10.1.193 COMMUNITY_DB_USER=community_user \
               COMMUNITY_DB_PASSWORD=sbcommunity1234 COMMUNITY_DB_NAME=community_db
        # MCP1과 동시 띄울 때는 포트 충돌 방지: 8001로 띄우거나 MCP1을 끄기
        python server.py
    - Tavily API 키 환경변수 (시나리오 ⑤ 통과 위해) — 외부 서버라 로컬 서버 안 띄움
        export TAVILY_API_KEY=tvly-...    # https://app.tavily.com/home 에서 발급
        # (선택) export TAVILY_MCP_URL=https://mcp.tavily.com/mcp/

Usage:
    cd gb-chatbot-lambda
    python -m tests.local_run

    # 특정 시나리오만 실행:
    python -m tests.local_run --only 5
"""
import argparse
import logging
import sys
import time

from src.chatbot import chat_once


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


# ──────────────────────────────────────────────────────────────────────────────
# 분석 요약 — 모든 시나리오 공통 (실제로는 MySQL document_results에서 추출)
# ──────────────────────────────────────────────────────────────────────────────
ANALYSIS_SUMMARY = (
    "위험도 HIGH. "
    "최저임금 미달(시급 9,620원 기준 미충족), 주 50시간 초과근무 조항 존재. "
    "임금: 월 2,000,000원 / 시급 9,620원. "
    "문서유형: 근로계약서."
)


# ──────────────────────────────────────────────────────────────────────────────
# 시나리오 정의
# ──────────────────────────────────────────────────────────────────────────────
SCENARIOS = [
    {
        "id": 1,
        "name": "시나리오 ① — 분석 요약만으로 답변 (도구 호출 없음)",
        "message": "이 계약서 어때?",
        "expected_tools": [],
    },
    {
        "id": 2,
        "name": "시나리오 ② — MCP1 환율 도구 호출",
        "message": "월급 200만원이 베트남 돈으로 얼마야?",
        "expected_tools": ["get_exchange_rate"],
    },
    {
        "id": 3,
        "name": "시나리오 ③ — KB 법령 도구 호출",
        "message": "외국인도 최저임금 미달이 법적으로 문제가 돼?",
        "expected_tools": ["search_legal_standard"],
    },
    {
        "id": 4,
        "name": "시나리오 ④ — MCP2 커뮤니티 검색 도구 호출",
        "message": "최저임금도 못 받고 일하는 비슷한 사람 있나? 다들 어떻게 했어?",
        "expected_tools": ["search_community_posts"],
    },
    {
        "id": 5,
        "name": "시나리오 ⑤ — MCP3 외부 Tavily 웹 검색 도구 호출 (외부 회사 직접 연결)",
        "message": "올해 한국 외국인 최저임금 얼마야?",
        "expected_tools": ["search_web"],
    },
]


def run_scenario(scenario: dict) -> bool:
    """Run a single scenario and stream the chatbot reply to stdout."""
    print(f"\n{'═' * 70}")
    print(f"  {scenario['name']}")
    print(f"{'═' * 70}")
    print(f"분석 요약: {ANALYSIS_SUMMARY}\n")
    print(f"사용자: {scenario['message']}\n")
    print(f"챗봇: ", end="", flush=True)

    try:
        for token in chat_once(
            message=scenario["message"],
            analysis_summary=ANALYSIS_SUMMARY,
            user_lang="ko",
        ):
            print(token, end="", flush=True)
        print("\n")
        return True
    except Exception as e:
        print(f"\n\n[에러] {type(e).__name__}: {e}\n", file=sys.stderr)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Run chatbot scenarios locally.")
    parser.add_argument(
        "--only", type=int, choices=[1, 2, 3, 4, 5], default=None,
        help="특정 시나리오만 실행 (1~5 중 하나). 미지정 시 전체 실행.",
    )
    parser.add_argument(
        "--delay", type=float, default=1.5,
        help="시나리오 사이 대기 시간(초) — rate limit 회피용 (기본 1.5)",
    )
    args = parser.parse_args()

    scenarios_to_run = (
        [s for s in SCENARIOS if s["id"] == args.only]
        if args.only else SCENARIOS
    )

    results = []
    for i, scenario in enumerate(scenarios_to_run):
        ok = run_scenario(scenario)
        results.append((scenario["id"], ok))
        # 다음 시나리오 전 잠깐 대기 (Bedrock rate limit 여유)
        if i < len(scenarios_to_run) - 1 and args.delay > 0:
            time.sleep(args.delay)

    # 요약 출력
    print(f"\n{'═' * 70}")
    print(f"  실행 결과 요약")
    print(f"{'═' * 70}")
    for sid, ok in results:
        mark = "✅" if ok else "❌"
        print(f"  {mark} 시나리오 {sid}")
    print()

    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main())
