"""
Chatbot tools — Bedrock Tool Use 대상 도구 정의 + 실행 함수.

Phase 4 Step 2에서 추가된 도구 2개:
  1. get_exchange_rate    — MCP1 환율 서버(gb-mcp-servers)에 HTTP 호출
  2. search_legal_standard — Bedrock Knowledge Base retrieve 호출 (유진 KB)

Bedrock Converse API의 toolConfig.tools 형식:
  https://docs.aws.amazon.com/bedrock/latest/userguide/tool-use.html
"""
import asyncio
import logging
import os

import boto3
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Bedrock Knowledge Base — 법령 검색 (유진이 만든 KB)
# ──────────────────────────────────────────────────────────────────────────────
DEFAULT_KB_ID = "KENYUCA5DE"  # knowledge-base-S3Vectors (유진 작업)
DEFAULT_KB_RESULTS = 5

_kb_client = None


def _get_kb_client():
    """Bedrock Agent Runtime client — KB retrieve 호출용 (singleton)."""
    global _kb_client
    if _kb_client is None:
        _kb_client = boto3.client(
            "bedrock-agent-runtime",
            region_name=os.environ.get("AWS_REGION", "ap-northeast-2"),
        )
    return _kb_client


def _search_legal_standard(query: str) -> str:
    """KB에서 법령 청크를 검색해 합쳐 반환."""
    kb_id = os.environ.get("LEGAL_KB_ID", DEFAULT_KB_ID)
    logger.info("KB retrieve: kb_id=%s query=%r", kb_id, query)

    try:
        response = _get_kb_client().retrieve(
            knowledgeBaseId=kb_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={
                "vectorSearchConfiguration": {"numberOfResults": DEFAULT_KB_RESULTS}
            },
        )
        results = response.get("retrievalResults", [])
        if not results:
            return f"'{query}' 관련 법령을 찾지 못했습니다."

        chunks = []
        for i, r in enumerate(results, 1):
            text = (r.get("content") or {}).get("text", "").strip()
            if text:
                chunks.append(f"[법령 청크 {i}]\n{text}")
        return "\n\n---\n\n".join(chunks) if chunks else "법령 결과가 비어있습니다."
    except Exception as e:
        logger.error("KB retrieve failed: %s", e)
        return f"법령 검색 중 오류가 발생했습니다: {type(e).__name__}"


# ──────────────────────────────────────────────────────────────────────────────
# MCP1 환율 서버 호출 (HTTP)
# ──────────────────────────────────────────────────────────────────────────────
DEFAULT_MCP_EXCHANGE_URL = "http://localhost:8000/mcp"


async def _call_mcp_exchange_async(amount_krw: float, target_currency: str) -> str:
    """MCP1 환율 서버의 get_exchange_rate 도구를 호출."""
    url = os.environ.get("MCP_EXCHANGE_URL", DEFAULT_MCP_EXCHANGE_URL)
    logger.info("MCP exchange call: url=%s amount=%s currency=%s",
                url, amount_krw, target_currency)

    try:
        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "get_exchange_rate",
                    arguments={
                        "amount_krw": amount_krw,
                        "target_currency": target_currency,
                    },
                )
                if result.content and len(result.content) > 0:
                    first = result.content[0]
                    if hasattr(first, "text") and first.text:
                        return first.text
                return "환율 정보를 받지 못했습니다."
    except Exception as e:
        logger.error("MCP exchange call failed: %s", e)
        return f"환율 서버 호출 중 오류가 발생했습니다: {type(e).__name__}"


def _get_exchange_rate(amount_krw: float, target_currency: str) -> str:
    """Sync wrapper for the async MCP call."""
    return asyncio.run(_call_mcp_exchange_async(amount_krw, target_currency))


# ──────────────────────────────────────────────────────────────────────────────
# Tool 정의 (Bedrock Converse API toolSpec)
# ──────────────────────────────────────────────────────────────────────────────
TOOLS = [
    {
        "toolSpec": {
            "name": "get_exchange_rate",
            "description": (
                "한국 원화(KRW)를 다른 통화로 환산합니다. "
                "외국인 사용자가 한국에서 받는 임금이나 금액을 본국 통화로 보고 싶을 때 사용하세요. "
                "예: '월급 200만원을 베트남 동(VND)으로?', '시급 9,620원이 필리핀 페소(PHP)로?'"
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "amount_krw": {
                            "type": "number",
                            "description": "환산할 원화 금액. 예: 2000000",
                        },
                        "target_currency": {
                            "type": "string",
                            "description": (
                                "목표 통화 코드 (ISO 4217). "
                                "지원: VND(베트남), PHP(필리핀), USD(미국), THB(태국), IDR(인도네시아), CNY(중국), JPY(일본)"
                            ),
                        },
                    },
                    "required": ["amount_krw", "target_currency"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "search_legal_standard",
            "description": (
                "한국 노동법령(최저임금법, 근로기준법, 외국인근로자법 등) 조문을 검색합니다. "
                "사용자가 법적 근거나 조항을 물을 때, 또는 어떤 사항이 합법/불법인지 판단하는 데 "
                "근거가 필요할 때 반드시 이 도구를 사용해 정확한 법령을 인용하세요. "
                "예: '외국인도 최저임금 적용?', '주 52시간 초과근무 합법?'"
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "검색할 법령 키워드 또는 자연어 질문. "
                                "예: '최저임금 적용 범위', '근로시간 한도', '외국인 근로자 보호'"
                            ),
                        }
                    },
                    "required": ["query"],
                }
            },
        }
    },
]


def execute_tool(tool_name: str, tool_input: dict) -> str:
    """Execute a tool call from Bedrock and return the result as text."""
    logger.info("Executing tool: name=%s input=%s", tool_name, tool_input)

    if tool_name == "get_exchange_rate":
        return _get_exchange_rate(
            amount_krw=float(tool_input.get("amount_krw", 0)),
            target_currency=str(tool_input.get("target_currency", "")),
        )

    if tool_name == "search_legal_standard":
        return _search_legal_standard(query=str(tool_input.get("query", "")))

    return f"Unknown tool: {tool_name}"
