"""
chat_once 스트리밍 동작 검증 — 외부 의존(AWS/Redis/MCP) 없이 단독 실행.

검증 목표 (응답 속도 개선 2건):
  1. 인공 토큰 지연 제거 — STREAM_CHAR_DELAY == 0.
  2. 실시간 스트리밍 — Bedrock 텍스트 델타를 받는 즉시 yield (턴 종료 전에 흘러나옴),
     도구 턴(toolUse 블록 선행)의 텍스트는 사용자에게 노출되지 않음(R2 유지).

방식:
  - boto3 / src.storage / src.tools 를 가짜 모듈로 sys.modules에 주입한 뒤 src.chatbot import.
  - chatbot._get_bedrock_client 를 스크립트된 이벤트를 내는 FakeBedrock 으로 교체.
  - 실제 AWS·네트워크 호출 없음.

실행:
  python -m pytest tests/test_streaming.py -s
  또는  python -m tests.test_streaming
"""
import sys
import time
import types


# ──────────────────────────────────────────────────────────────────────────────
# 1) 무거운/부재 의존성을 가짜 모듈로 대체 (src.chatbot import 전에 실행돼야 함)
# ──────────────────────────────────────────────────────────────────────────────
def _install_stubs():
    boto3_stub = types.ModuleType("boto3")
    boto3_stub.client = lambda *a, **k: None
    sys.modules.setdefault("boto3", boto3_stub)

    storage_stub = types.ModuleType("src.storage")
    storage_stub.build_context_turns = lambda summary: []
    sys.modules["src.storage"] = storage_stub

    tools_stub = types.ModuleType("src.tools")
    tools_stub.TOOLS = []
    tools_stub.execute_tool = lambda name, inp: f"[stub:{name}]"
    sys.modules["src.tools"] = tools_stub


_install_stubs()

from src import chatbot  # noqa: E402  (스텁 주입 후 import)


# ──────────────────────────────────────────────────────────────────────────────
# 2) FakeBedrock — converse_stream 호출마다 미리 스크립트된 이벤트 시퀀스를 반환
# ──────────────────────────────────────────────────────────────────────────────
def _text_block_events(text, chunk=12):
    yield {"contentBlockStart": {"start": {}}}
    for i in range(0, len(text), chunk):
        yield {"contentBlockDelta": {"delta": {"text": text[i:i + chunk]}}}
    yield {"contentBlockStop": {}}


def _tooluse_block_events(tu_id, name, input_json):
    yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": tu_id, "name": name}}}}
    yield {"contentBlockDelta": {"delta": {"toolUse": {"input": input_json}}}}
    yield {"contentBlockStop": {}}


def _tail(stop_reason):
    yield {"messageStop": {"stopReason": stop_reason}}
    yield {"metadata": {"usage": {"inputTokens": 10, "outputTokens": 20}}}


class FakeBedrock:
    def __init__(self, scripts):
        self._scripts = list(scripts)
        self.calls = 0

    def converse_stream(self, **kwargs):
        ev = self._scripts[self.calls]
        self.calls += 1
        return {"stream": iter(ev)}


def _patch(scripts):
    fake = FakeBedrock(scripts)
    chatbot._get_bedrock_client = lambda: fake
    return fake


# ──────────────────────────────────────────────────────────────────────────────
# 3) 테스트
# ──────────────────────────────────────────────────────────────────────────────
def test_no_delay_constant():
    assert chatbot.STREAM_CHAR_DELAY == 0.0, "인공 토큰 지연이 남아있음"


def test_realtime_streaming_no_tools():
    """도구 없는 답변: 델타 도착 즉시 여러 번 yield, 인공 지연 없음."""
    answer = "안녕하세요. 이 계약서는 위험도가 높습니다. " * 12  # 300+자
    _patch([list(_text_block_events(answer)) + list(_tail("end_turn"))])

    start = time.perf_counter()
    toks = []
    for t in chatbot.chat_once(message="이 계약서 어때?", messages=[]):
        toks.append((t, time.perf_counter() - start))
    elapsed = time.perf_counter() - start

    joined = "".join(t for t, _ in toks)
    assert joined == answer, "스트리밍 결과가 원문과 다름"
    assert len(toks) >= 2, f"실시간 분할 스트리밍이 아님: {len(toks)}회"
    assert elapsed < 0.5, f"인공 지연 의심 — {elapsed:.3f}s"
    assert toks[0][1] < 0.2, f"첫 토큰이 늦음 — {toks[0][1]:.3f}s"
    print(f"  [no_tools] yields={len(toks)} elapsed={elapsed:.4f}s first@{toks[0][1]:.4f}s")


def test_tool_then_answer():
    """도구 호출 후 최종 답변만 사용자에게 — 도구 원시결과 비노출."""
    final = "200만원은 약 3,600,000 VND 입니다."
    fake = _patch([
        list(_tooluse_block_events("tu_1", "get_exchange_rate",
                                   '{"amount_krw":2000000,"target_currency":"VND"}'))
        + list(_tail("tool_use")),
        list(_text_block_events(final)) + list(_tail("end_turn")),
    ])
    joined = "".join(chatbot.chat_once(message="월급 베트남 돈?", messages=[]))
    assert fake.calls == 2, f"도구 루프 2회 converse 아님: {fake.calls}"
    assert joined == final, f"최종 답변만 나와야: {joined!r}"
    assert "[stub:" not in joined, "도구 원시 결과 누출"
    print(f"  [tool_then_answer] converse_calls={fake.calls} answer={joined!r}")


def test_tool_turn_text_suppressed():
    """도구 턴이 toolUse로 시작하면 같은 턴의 텍스트는 노출 안 됨(R2)."""
    final = "확인 결과 최저임금 미달입니다."
    _patch([
        list(_tooluse_block_events("tu_2", "search_legal_standard", '{"query":"최저임금"}'))
        + [{"contentBlockStart": {"start": {}}},
           {"contentBlockDelta": {"delta": {"text": "도구턴 lead 텍스트(노출금지)"}}},
           {"contentBlockStop": {}}]
        + list(_tail("tool_use")),
        list(_text_block_events(final)) + list(_tail("end_turn")),
    ])
    joined = "".join(chatbot.chat_once(message="최저임금 위반?", messages=[]))
    assert joined == final, f"도구턴 텍스트가 샘: {joined!r}"
    print(f"  [tool_turn_suppressed] answer={joined!r}")


def _run_all():
    fails = 0
    for name in ["test_no_delay_constant", "test_realtime_streaming_no_tools",
                 "test_tool_then_answer", "test_tool_turn_text_suppressed"]:
        try:
            globals()[name]()
            print(f"PASS  {name}")
        except AssertionError as e:
            fails += 1
            print(f"FAIL  {name}: {e}")
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{'ALL PASS' if fails == 0 else 'FAILED'} — {fails} failure(s)")
    return fails


if __name__ == "__main__":
    sys.exit(_run_all())
