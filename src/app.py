"""
FastAPI HTTP entry point — 챗봇 본체(chat_once)와 AWS Lambda 런타임 사이의 어댑터.

플로우:
    [Spring 백엔드]
        POST / application/json (chatbot.function-url + IAM SigV4)
        ↓
    [Lambda Function URL — RESPONSE_STREAM 모드]
        ↓
    [Lambda Web Adapter (extension)]
        HTTP → ASGI 변환
        ↓
    [FastAPI 이 모듈]
        ├─ JSON payload 파싱
        ├─ chat_once 호출 (chatbot.py)
        ├─ 토큰 generator → SSE 프레임 변환
        └─ StreamingResponse 반환 (chunked, text/event-stream)
        ↓
    [백엔드 SseRelayListener]
        SSE 파싱 → SseEmitter 중계 → 사용자

응답 SSE 포맷 (R1 PoC `poc/r1-stream/index.mjs` 와 동일):
    event: token
    data: <text>          ← 한 토큰씩 점진 표시
    (빈 줄)

    event: done
    data: {"session_id":"..."}   ← 종료 신호
    (빈 줄)

R2: end_turn 턴의 텍스트만 흘리고 tool_use 턴은 비노출.
    chat_once가 이미 end_turn 텍스트만 yield하므로 이 모듈은 그대로 받아서 프레이밍.
"""
import json
import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from src.chatbot import chat_once
from src.storage import cache_thread, fetch_history, load_thread, save_turn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="gb-chatbot-lambda",
    description="Global Bridge 후속 질문 챗봇 (Bedrock + MCP + KB)",
)


@app.get("/health")
def health():
    """헬스체크 — Function URL이 살아있는지 확인용. 인증 불필요한 경량 응답."""
    return {"status": "ok"}


@app.get("/history")
def history(
    user_public_id: str = "",
    document_public_id: str = "",
    limit: int = 50,
    cursor: str = None,
):
    """대화 이력 조회 — 재방문 복원용 (ai-chatbot-mcp.md §6-2). 비스트리밍 JSON.

    백엔드 전용 내부 라우트 — Spring이 인증·문서 소유자 검증을 마친 뒤 IAM SigV4로
    릴레이한다(Function URL 자체가 IAM 인증이라 인터넷 비노출). 여기서 인가를 다시 하지 않는다.

    Response: {"messages": [{"role", "content", "created_at"}], "next_cursor": str|null}
        visible=true 턴만 시간 오름차순 — 합성 요약 턴은 노출되지 않는다.
    """
    logger.info(
        "history request: doc=%s user=%s limit=%d cursor=%s",
        document_public_id, user_public_id, limit, cursor is not None,
    )
    return fetch_history(
        user_public_id=user_public_id,
        document_public_id=document_public_id,
        limit=limit,
        cursor=cursor,
    )


@app.post("/")
async def chat_endpoint(request: Request):
    """챗봇 메인 엔드포인트.

    Spring 백엔드가 POST application/json으로 호출하면 chat_once를 돌려
    토큰을 SSE 프레임으로 흘려준다. 응답 포맷은 R1 PoC와 동일.

    Request payload (ai-chatbot-mcp.md §6):
        {
          "message": "사용자 질문",
          "session_id": "<UUID>",
          "user_lang": "ko",
          "document_public_id": "...",
          "user_public_id": "...",
          "source": "production" | "development",
          "environment": "dev" | "stage" | "prod",
          "analysis_summary": "위험도 HIGH. 최저임금 미달..."
        }

    Response:
        text/event-stream (chunked).
        event:token 으로 토큰 점진 전송 → event:done 으로 종료.
        예외 시 event:error 한 번 흘리고 종료.
    """
    payload = await request.json()

    message = payload.get("message", "")
    session_id = payload.get("session_id", "")
    user_lang = payload.get("user_lang", "ko")
    analysis_summary = payload.get("analysis_summary")
    document_public_id = payload.get("document_public_id", "")
    user_public_id = payload.get("user_public_id", "")
    environment = payload.get("environment", "dev")

    logger.info(
        "chat request: session=%s doc=%s user=%s lang=%s msg_len=%d has_summary=%s",
        session_id, document_public_id, user_public_id, user_lang,
        len(message), analysis_summary is not None,
    )

    # 스레드 복원 (ai-chatbot-mcp.md §3-4/§3-5) — 스레드 정체성 = (user, document).
    # 기존 스레드가 있으면 백엔드가 보낸 analysis_summary는 무시된다(중복 주입 방지 — §6).
    messages, inject_context = load_thread(user_public_id, document_public_id, analysis_summary)

    def sse_stream():
        """chat_once의 토큰 generator를 SSE 프레임으로 변환."""
        reply_parts = []
        tools_used = []
        try:
            for token in chat_once(
                message=message,
                user_lang=user_lang,
                environment=environment,
                messages=messages,
                tools_used_out=tools_used,
            ):
                reply_parts.append(token)
                # SSE 스펙: data 안의 \n은 멀티라인 의미라 이스케이프해서 한 줄로
                escaped = token.replace("\n", "\\n")
                yield f"event: token\ndata: {escaped}\n\n"

            # 턴 종료 저장 (§3-4 2계층 — 실패해도 raise 안 함)
            # Redis: toolUse 블록 포함 messages 통째 (chat_once가 in-place로 누적해 둠), TTL 30분 갱신
            cache_thread(user_public_id, document_public_id, messages)
            # DynamoDB: 텍스트 턴만 (§7 — 첫 대화면 합성 2턴 포함 4건, 이후 2건)
            save_turn(
                user_public_id=user_public_id,
                document_public_id=document_public_id,
                session_id=session_id,
                environment=environment,
                language=user_lang,
                user_message=message,
                assistant_reply="".join(reply_parts),
                tools_used=tools_used,
                analysis_summary=analysis_summary,
                inject_context=inject_context,
            )

            done_payload = json.dumps({"session_id": session_id})
            yield f"event: done\ndata: {done_payload}\n\n"

        except Exception as e:
            # 도구 호출 실패, Bedrock 오류 등 — 사용자에게 친화 메시지 흘리고 종료
            logger.error("chat_once failed", exc_info=True)
            err_payload = json.dumps({
                "type": type(e).__name__,
                "message": "답변 생성 중 오류가 발생했습니다.",
            })
            yield f"event: error\ndata: {err_payload}\n\n"

    return StreamingResponse(
        sse_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # nginx/CloudFront 등 프록시의 응답 버퍼링 방지 (chunked 그대로 흘림)
            "X-Accel-Buffering": "no",
        },
    )
