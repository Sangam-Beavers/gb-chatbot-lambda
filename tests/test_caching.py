"""
프롬프트 캐싱 요청 형태 검증 — 외부 의존(AWS) 없이 단독 실행.

검증:
  1. PROMPT_CACHING=True 면 converse_stream 요청의 toolConfig.tools 끝에
     cachePoint 블록이 붙는다(= system+도구 prefix 캐시 지시).
  2. PROMPT_CACHING=False 면 cachePoint가 붙지 않는다.
  3. usage_out 로 메타데이터(usage)의 cacheRead/Write 토큰을 수집한다.

실제 캐시 적중(서버측)은 Bedrock e2e(tests/benchmark.py 또는 local_run)에서 확인한다.
여기서는 "요청을 올바르게 보냈는가"만 본다.

실행:
  python -m tests.test_caching
"""
import sys
import types


def _install_stubs():
    boto3_stub = types.ModuleType("boto3")
    boto3_stub.client = lambda *a, **k: None
    sys.modules.setdefault("boto3", boto3_stub)

    storage_stub = types.ModuleType("src.storage")
    storage_stub.build_context_turns = lambda summary: []
    sys.modules["src.storage"] = storage_stub

    tools_stub = types.ModuleType("src.tools")
    # 실제 도구 1개를 흉내내 cachePoint가 "도구 뒤"에 붙는지 확인
    tools_stub.TOOLS = [{"toolSpec": {"name": "dummy"}}]
    tools_stub.execute_tool = lambda name, inp: f"[stub:{name}]"
    sys.modules["src.tools"] = tools_stub


_install_stubs()
from src import chatbot  # noqa: E402


def _answer_stream():
    yield {"contentBlockStart": {"start": {}}}
    yield {"contentBlockDelta": {"delta": {"text": "캐싱 테스트 답변"}}}
    yield {"contentBlockStop": {}}
    yield {"messageStop": {"stopReason": "end_turn"}}
    yield {"metadata": {"usage": {
        "inputTokens": 120,
        "outputTokens": 30,
        "cacheReadInputTokens": 2000,
        "cacheWriteInputTokens": 0,
    }}}


class CaptureBedrock:
    """converse_stream 호출 kwargs를 저장하고 정해진 스트림을 반환."""

    def __init__(self):
        self.last_kwargs = None

    def converse_stream(self, **kwargs):
        self.last_kwargs = kwargs
        return {"stream": iter(_answer_stream())}


def _run(caching: bool):
    fake = CaptureBedrock()
    chatbot._get_bedrock_client = lambda: fake
    chatbot.PROMPT_CACHING = caching
    usage = []
    list(chatbot.chat_once(message="질문", messages=[], usage_out=usage))
    return fake.last_kwargs, usage


def test_cachepoint_present_when_enabled():
    kwargs, _ = _run(caching=True)
    tools = kwargs["toolConfig"]["tools"]
    assert tools[-1] == {"cachePoint": {"type": "default"}}, f"cachePoint 누락: {tools}"
    # 도구 뒤에 붙어야 함(도구 자체는 보존)
    assert any("toolSpec" in t for t in tools), "원래 도구가 사라짐"
    print(f"  [enabled] tools 끝에 cachePoint 확인 (총 {len(tools)}개 요소)")


def test_no_cachepoint_when_disabled():
    kwargs, _ = _run(caching=False)
    tools = kwargs["toolConfig"]["tools"]
    assert all("cachePoint" not in t for t in tools), f"꺼졌는데 cachePoint 존재: {tools}"
    print(f"  [disabled] cachePoint 없음 확인")


def test_usage_out_collects_cache_tokens():
    _, usage = _run(caching=True)
    assert len(usage) == 1, f"usage 수집 실패: {usage}"
    assert usage[0].get("cacheReadInputTokens") == 2000, "캐시 read 토큰 미수집"
    print(f"  [usage_out] cacheReadInputTokens={usage[0]['cacheReadInputTokens']} 수집 확인")


def _main():
    fails = 0
    for name in ["test_cachepoint_present_when_enabled",
                 "test_no_cachepoint_when_disabled",
                 "test_usage_out_collects_cache_tokens"]:
        try:
            globals()[name]()
            print(f"PASS  {name}")
        except AssertionError as e:
            fails += 1
            print(f"FAIL  {name}: {e}")
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"ERROR {name}: {type(e).__name__}: {e}")
        finally:
            chatbot.PROMPT_CACHING = True  # 복구
    print(f"\n{'ALL PASS' if fails == 0 else 'FAILED'} — {fails} failure(s)")
    return fails


if __name__ == "__main__":
    sys.exit(_main())
