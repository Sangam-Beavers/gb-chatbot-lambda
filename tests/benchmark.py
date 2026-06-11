"""
응답속도 A/B 벤치마크 — 실제 Bedrock 호출로 개선 효과를 정량 측정한다.

두 가지 비교 모드:
  --mode streaming : 개선 전(STREAM_LIVE=False + 인공지연 0.005) vs 후(실시간+지연0)
                     → 첫 응답 체감 시간(TTFB)·총시간 비교. (#15 스트리밍 효과 실측)
  --mode caching   : 프롬프트 캐싱 OFF vs ON → 입력 토큰·캐시 히트율·비용. (#17 효과)

지표 (질문 1건당):
  - TTFB      : 첫 사용자 노출 토큰까지 시간(초)
  - total     : 전체 응답 완료까지 시간(초)
  - in_tokens : 비캐시 입력 토큰 합
  - cache_read/write, hit_rate

요구사항: local_run.py 와 동일 (AWS gb-account-b 자격증명 + Bedrock/KB 권한).
  export AWS_PROFILE=gb-account-b
  python -m tests.benchmark --mode streaming --runs 3
  python -m tests.benchmark --mode caching   --runs 3
  python -m tests.benchmark --mode streaming --no-tools
"""
import argparse
import sys
import time

from src import chatbot

ANALYSIS_SUMMARY = (
    "위험도 HIGH. 최저임금 미달(시급 9,620원 기준 미충족), 주 50시간 초과근무 조항 존재. "
    "임금: 월 2,000,000원 / 시급 9,620원. 문서유형: 근로계약서."
)
MSG_TOOLS = "외국인도 최저임금 미달이 법적으로 문제가 돼?"
MSG_SIMPLE = "이 계약서 어때?"


def run_once(message: str) -> dict:
    """chat_once 1회 실행하며 TTFB/total/토큰 측정."""
    usage = []
    start = time.perf_counter()
    ttfb = None
    for _tok in chatbot.chat_once(
        message=message,
        analysis_summary=ANALYSIS_SUMMARY,
        user_lang="ko",
        usage_out=usage,
    ):
        if ttfb is None:
            ttfb = time.perf_counter() - start
    total = time.perf_counter() - start

    in_tok = sum(u.get("inputTokens", 0) or 0 for u in usage)
    cr = sum(u.get("cacheReadInputTokens", 0) or 0 for u in usage)
    cw = sum(u.get("cacheWriteInputTokens", 0) or 0 for u in usage)
    denom = in_tok + cr + cw
    return {
        "ttfb": ttfb or total, "total": total, "calls": len(usage),
        "in": in_tok, "cache_read": cr, "cache_write": cw,
        "hit_rate": (cr / denom) if denom else 0.0,
    }


def bench(label: str, message: str, setup, runs: int) -> list:
    """setup(): 플래그 세팅 콜백. runs회 실행."""
    setup()
    rows = []
    for i in range(runs):
        r = run_once(message)
        rows.append(r)
        print(
            f"  [{label}] run {i+1}/{runs}: "
            f"TTFB={r['ttfb']:.2f}s total={r['total']:.2f}s calls={r['calls']} "
            f"in={r['in']} cache_read={r['cache_read']} hit={r['hit_rate']*100:.0f}%"
        )
        time.sleep(1.0)
    return rows


def avg(rows, key):
    return sum(r[key] for r in rows) / len(rows) if rows else 0.0


def pct(a, b):  # a 대비 b의 감소율
    return (a - b) / a * 100 if a else 0.0


def _save_flags():
    return (chatbot.STREAM_LIVE, chatbot.STREAM_CHAR_DELAY, chatbot.PROMPT_CACHING)


def _restore_flags(saved):
    chatbot.STREAM_LIVE, chatbot.STREAM_CHAR_DELAY, chatbot.PROMPT_CACHING = saved


def mode_streaming(message, runs):
    print("[1] 개선 전 (버퍼링: STREAM_LIVE=False, 인공지연 5ms/자, 캐싱 OFF)")

    def old():
        chatbot.STREAM_LIVE = False
        chatbot.STREAM_CHAR_DELAY = 0.005
        chatbot.PROMPT_CACHING = False
    before = bench("개선 전", message, old, runs)

    print("\n[2] 개선 후 (실시간: STREAM_LIVE=True, 지연 0, 캐싱 OFF)")

    def new():
        chatbot.STREAM_LIVE = True
        chatbot.STREAM_CHAR_DELAY = 0.0
        chatbot.PROMPT_CACHING = False
    after = bench("개선 후", message, new, runs)

    print(f"\n{'='*60}\n  요약 (평균) — 스트리밍 효과\n{'='*60}")
    print(f"{'지표':<12}{'개선 전':>12}{'개선 후':>12}{'단축':>10}")
    print(f"{'TTFB(s)':<12}{avg(before,'ttfb'):>12.2f}{avg(after,'ttfb'):>12.2f}{pct(avg(before,'ttfb'),avg(after,'ttfb')):>9.0f}%")
    print(f"{'total(s)':<12}{avg(before,'total'):>12.2f}{avg(after,'total'):>12.2f}{pct(avg(before,'total'),avg(after,'total')):>9.0f}%")
    print("\n핵심: 개선 전엔 전체 생성이 끝나야 첫 글자가 떠 TTFB가 total에 가깝다.")


def mode_caching(message, runs):
    print("[1] 프롬프트 캐싱 OFF (기준선, 실시간 스트리밍은 ON 유지)")

    def off():
        chatbot.STREAM_LIVE = True
        chatbot.STREAM_CHAR_DELAY = 0.0
        chatbot.PROMPT_CACHING = False
    off_rows = bench("캐싱 OFF", message, off, runs)

    print("\n[2] 프롬프트 캐싱 ON")

    def on():
        chatbot.STREAM_LIVE = True
        chatbot.STREAM_CHAR_DELAY = 0.0
        chatbot.PROMPT_CACHING = True
    on_rows = bench("캐싱 ON ", message, on, runs)

    print(f"\n{'='*60}\n  요약 (평균) — 캐싱 효과\n{'='*60}")
    print(f"{'지표':<12}{'캐싱 OFF':>12}{'캐싱 ON':>12}{'개선':>10}")
    print(f"{'total(s)':<12}{avg(off_rows,'total'):>12.2f}{avg(on_rows,'total'):>12.2f}{pct(avg(off_rows,'total'),avg(on_rows,'total')):>9.0f}%")
    print(f"{'비캐시 입력':<12}{avg(off_rows,'in'):>12.0f}{avg(on_rows,'in'):>12.0f}{pct(avg(off_rows,'in'),avg(on_rows,'in')):>9.0f}%")
    print(f"{'cache_read':<12}{avg(off_rows,'cache_read'):>12.0f}{avg(on_rows,'cache_read'):>12.0f}")
    print(f"{'hit_rate':<12}{avg(off_rows,'hit_rate')*100:>11.0f}%{avg(on_rows,'hit_rate')*100:>11.0f}%")
    print("\n핵심: 캐싱은 입력 토큰·비용 절감(속도 변화는 작음).")


def main():
    ap = argparse.ArgumentParser(description="챗봇 응답속도 A/B 벤치마크")
    ap.add_argument("--mode", choices=["streaming", "caching"], default="caching")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--no-tools", action="store_true", help="도구 없는 단순 질문 사용")
    args = ap.parse_args()

    message = MSG_SIMPLE if args.no_tools else MSG_TOOLS
    kind = "단순(도구 없음)" if args.no_tools else "도구 호출"
    print(f"\n{'='*60}\n  벤치마크 [{args.mode}]: {kind} 질문 / 설정별 {args.runs}회\n{'='*60}")
    print(f"질문: {message}\n")

    saved = _save_flags()
    try:
        if args.mode == "streaming":
            mode_streaming(message, args.runs)
        else:
            mode_caching(message, args.runs)
    finally:
        _restore_flags(saved)
    return 0


if __name__ == "__main__":
    sys.exit(main())
