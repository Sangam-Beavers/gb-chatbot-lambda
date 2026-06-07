"""
Local manual test for the translator Lambda.

실제 Bedrock 으로 번역을 호출해 결과를 stdout 에 찍는다(`chatbot/local_run.py` 패턴).
모킹 없음 — AWS 자격증명·Bedrock 모델 접근 권한이 필요하다.

Usage:
    cd gb-chatbot-lambda
    # AWS 자격증명 (계정 B)
    export AWS_REGION=ap-northeast-2
    # Haiku inference profile (서울 리전)
    export BEDROCK_MODEL_ID=global.anthropic.claude-haiku-4-5
    python -m tests.local_translator_run

    # 특정 시나리오만:
    python -m tests.local_translator_run --only 2
"""
import argparse
import json
import logging
import sys
import time

from src.translator import TranslatorError, translate


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


SCENARIOS = [
    {
        "id": 1,
        "name": "시나리오 ① — 게시글 ko → vi",
        "payload": {
            "kind": "post",
            "public_id": "post-demo-1",
            "title": "월급 200만원, 베트남에 보내면 얼마인가요?",
            "content": (
                "안녕하세요. 한국에서 일한지 3개월 된 베트남 사람입니다.\n"
                "월급 200만원을 받고 있는데, 베트남 동으로 보내면 얼마쯤 되나요?\n"
                "송금 수수료도 궁금합니다."
            ),
            "source_lang": "ko",
            "target_lang": "vi",
        },
    },
    {
        "id": 2,
        "name": "시나리오 ② — 댓글 ko → en",
        "payload": {
            "kind": "comment",
            "public_id": "comment-demo-1",
            "title": None,
            "content": "저도 비슷한 상황인데, 외국인근로자센터에 문의해보세요. 도움 많이 받았어요.",
            "source_lang": "ko",
            "target_lang": "en",
        },
    },
    {
        "id": 3,
        "name": "시나리오 ③ — 같은 언어(원문 그대로 반환, Bedrock 호출 0회)",
        "payload": {
            "kind": "post",
            "public_id": "post-demo-2",
            "title": "테스트 제목",
            "content": "이 게시글은 번역되지 않고 원문 그대로 나와야 합니다.",
            "source_lang": "ko",
            "target_lang": "ko",
        },
    },
    {
        "id": 4,
        "name": "시나리오 ④ — 게시글 ko → fil",
        "payload": {
            "kind": "post",
            "public_id": "post-demo-3",
            "title": "최저임금 미달 신고는 어디서 하나요?",
            "content": "사장님이 시급을 8천원만 줍니다. 최저임금보다 적은데, 어디에 신고해야 하나요?",
            "source_lang": "ko",
            "target_lang": "fil",
        },
    },
]


def run_scenario(scenario: dict) -> bool:
    print(f"\n{'═' * 70}")
    print(f"  {scenario['name']}")
    print(f"{'═' * 70}")
    payload = scenario["payload"]
    print(f"요청:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n")

    try:
        result = translate(**payload)
    except TranslatorError as exc:
        print(f"[TranslatorError] code={exc.error_code} message={exc.message}", file=sys.stderr)
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"[Unexpected] {type(exc).__name__}: {exc}", file=sys.stderr)
        return False

    print(f"응답:\n{json.dumps(result, ensure_ascii=False, indent=2)}\n")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Run translator scenarios locally.")
    parser.add_argument(
        "--only", type=int, choices=[s["id"] for s in SCENARIOS], default=None,
        help="특정 시나리오만 실행.",
    )
    parser.add_argument(
        "--delay", type=float, default=1.0,
        help="시나리오 사이 대기(초) — rate limit 회피.",
    )
    args = parser.parse_args()

    targets = (
        [s for s in SCENARIOS if s["id"] == args.only]
        if args.only else SCENARIOS
    )

    results = []
    for i, sc in enumerate(targets):
        ok = run_scenario(sc)
        results.append((sc["id"], ok))
        if i < len(targets) - 1 and args.delay > 0:
            time.sleep(args.delay)

    print(f"\n{'═' * 70}")
    print(f"  실행 결과 요약")
    print(f"{'═' * 70}")
    for sid, ok in results:
        mark = "OK " if ok else "FAIL"
        print(f"  [{mark}] 시나리오 {sid}")
    print()

    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main())
