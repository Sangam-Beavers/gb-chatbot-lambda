"""
Chatbot tools — Bedrock Tool Use 대상 도구 정의 + 실행 함수.

도구 4개:
  1. get_exchange_rate       — MCP1 환율 서버(gb-mcp-servers/mcp-exchange) HTTP 호출 (같은 회사 다른 팀)
  2. search_legal_standard   — Bedrock Knowledge Base retrieve (유진 KB, 우리 도메인)
  3. search_community_posts  — MCP2 커뮤니티 서버(gb-mcp-servers/mcp-community) HTTP 호출 (같은 회사 다른 팀)
  4. search_web              — MCP3 Tavily Remote MCP Server (외부 회사 직접 연결) — 실시간 웹 검색

특이사항:
  - search_web 은 Tavily 가 운영하는 외부 공식 MCP 서버(https://mcp.tavily.com/mcp/) 에
    챗봇이 클라이언트로 직접 붙는 구조. 자체 어댑터 서버를 거치지 않음.
  - 발표 narrative: MCP 본래 가치(외부 회사 시스템을 표준 인터페이스로 통합)의 정면 사례.

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
# MCP2 커뮤니티 검색 서버 호출 (HTTP)
# ──────────────────────────────────────────────────────────────────────────────
DEFAULT_MCP_COMMUNITY_URL = "http://localhost:8001/mcp"


async def _call_mcp_community_async(query: str, limit: int) -> str:
    """MCP2 커뮤니티 서버의 search_community_posts 도구를 호출."""
    url = os.environ.get("MCP_COMMUNITY_URL", DEFAULT_MCP_COMMUNITY_URL)
    logger.info("MCP community call: url=%s query=%r limit=%d", url, query, limit)

    try:
        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "search_community_posts",
                    arguments={"query": query, "limit": limit},
                )
                if result.content and len(result.content) > 0:
                    first = result.content[0]
                    if hasattr(first, "text") and first.text:
                        return first.text
                return "커뮤니티 검색 결과를 받지 못했습니다."
    except Exception as e:
        logger.error("MCP community call failed: %s", e)
        return f"커뮤니티 검색 서버 호출 중 오류가 발생했습니다: {type(e).__name__}"


def _search_community_posts(query: str, limit: int = 3) -> str:
    """Sync wrapper for the async MCP call."""
    return asyncio.run(_call_mcp_community_async(query, limit))


# ──────────────────────────────────────────────────────────────────────────────
# MCP3 Tavily Remote MCP Server 호출 (외부 회사 직접 연결)
# ──────────────────────────────────────────────────────────────────────────────
# Tavily 가 운영하는 공식 Remote MCP Server. 챗봇 Lambda 가 클라이언트로 직접 붙는다.
# 자체 어댑터 서버를 거치지 않음 — MCP 본래 가치(외부 시스템 표준 통합)의 정면 사례.
#
# Transport : Streamable HTTP (Lambda 환경에 적합, stdio 불필요)
# 인증      : Authorization: Bearer <TAVILY_API_KEY>  (URL 쿼리 노출 회피)
# 노출 도구 : tavily_search, tavily_extract, tavily_crawl, tavily_map, tavily_research
#             — 우리는 그중 tavily_search 만 사용 (시연 시나리오 ⑤ 충분).
#             — Bedrock toolSpec 이름은 [a-zA-Z][a-zA-Z0-9_]* 만 허용하므로
#               외부 표시는 search_web 으로 두고 내부에서 tavily_search 로 매핑.
#             — 주의: Tavily docs 페이지엔 'tavily-search' (hyphen) 로 적혀있으나
#               실제 list_tools 응답은 'tavily_search' (underscore). 실측 기준 박음.
# 무료 quota: Researcher Plan 월 1,000 search (시연용 충분).
# Docs      : https://docs.tavily.com/documentation/mcp
DEFAULT_TAVILY_MCP_URL = "https://mcp.tavily.com/mcp/"


async def _call_tavily_search_async(query: str, max_results: int) -> str:
    """외부 Tavily Remote MCP Server의 tavily-search 도구를 호출."""
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        logger.warning("TAVILY_API_KEY 환경변수가 비어있음 — 웹 검색 호출 스킵")
        return "Tavily API 키가 설정되지 않아 웹 검색을 수행할 수 없습니다."

    url = os.environ.get("TAVILY_MCP_URL", DEFAULT_TAVILY_MCP_URL)
    headers = {"Authorization": f"Bearer {api_key}"}
    logger.info("Tavily MCP call: url=%s query=%r max_results=%d", url, query, max_results)

    try:
        async with streamablehttp_client(url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "tavily_search",
                    arguments={
                        "query": query,
                        "max_results": max_results,
                    },
                )

                # Tavily MCP는 도구 실행 실패 시 isError=True + 에러 메시지 텍스트로 응답.
                # 디버깅이 필요하면 logger.debug로 (운영 정상 호출에서 검색 결과 본문이
                # CloudWatch에 그대로 남는 걸 피하기 위해 INFO에서는 메타정보만).
                logger.info(
                    "Tavily response: isError=%s, content_count=%d",
                    getattr(result, "isError", None),
                    len(result.content) if result.content else 0,
                )

                # 모든 text 블록을 하나로 합쳐서 반환 (Tavily가 여러 블록으로 줄 수 있음)
                texts = []
                for item in (result.content or []):
                    t = getattr(item, "text", None)
                    if t:
                        texts.append(t)
                if texts:
                    return "\n\n".join(texts)
                return "웹 검색 결과를 받지 못했습니다."
    except Exception as e:
        logger.error("Tavily MCP call failed: %s", e)
        return f"웹 검색 중 오류가 발생했습니다: {type(e).__name__}"


def _search_web(query: str, max_results: int = 5) -> str:
    """Sync wrapper for the async Tavily Remote MCP call."""
    return asyncio.run(_call_tavily_search_async(query, max_results))


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
    {
        "toolSpec": {
            "name": "search_community_posts",
            "description": (
                "외국인 노동자 커뮤니티에서 사용자 질문과 비슷한 경험을 가진 다른 사용자의 "
                "게시글과 댓글을 검색합니다. 게시글 제목/본문/댓글 전부에서 키워드를 매칭하며, "
                "관련 글의 카테고리·본문 일부·주요 댓글이 함께 반환됩니다. "
                "공감과 실전 조언을 제공해야 하는 상황(임금 체불·미달, 비자·체류, 계약서, "
                "초과근무, 외국인근로자 권리 등)에서 사용하세요. "
                "예: '비슷한 경험 한 사람 있나?', '다른 사람들은 어떻게 했어?', "
                "'이런 상황 겪어본 사람?'"
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "⚠️ 반드시 짧은 핵심 키워드 1~2단어로만 입력. "
                                "긴 자연어 문장이나 조사·어미가 붙은 표현은 매칭률이 떨어집니다. "
                                "여러 단어를 넣으면 공백 기준으로 OR 매칭됩니다.\n"
                                "\n"
                                "✅ 좋은 예: '최저임금', '임금체불', '주52시간', '계약서', "
                                "'E-9 비자', '사업장 변경'\n"
                                "❌ 나쁜 예: '최저임금 미달 임금 적게 받음', "
                                "'최저임금 못 받았어요', '임금이 체불됐는데 어떻게 신고하나요'"
                            ),
                        },
                        "limit": {
                            "type": "number",
                            "description": (
                                "반환할 게시글 수 (기본 3, 최대 10). "
                                "글 1개당 본문 일부 + 댓글 최대 5개가 함께 포함됩니다."
                            ),
                        },
                    },
                    "required": ["query"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "search_web",
            "description": (
                "최신 외부 웹 정보를 실시간으로 검색합니다. 모델이 학습 시점에 모르거나 "
                "시기성이 강한 정보(올해/최근의 최저임금·정책·고시, 공공기관 공지, 비자 "
                "정책 변경, 외국인 고용 관련 최신 가이드 등) 를 사용자가 물을 때 사용하세요. "
                "내부적으로는 Tavily 가 운영하는 외부 공식 MCP 서버를 통해 웹 검색 결과를 "
                "받아옵니다. 법령 조항(정적·확정)은 search_legal_standard, 커뮤니티 글은 "
                "search_community_posts, 단순 환산은 get_exchange_rate 가 더 적절합니다.\n"
                "\n"
                "예: '올해 한국 외국인 최저임금 얼마야?', "
                "'2026년 외국인 고용허가제 변경사항?', "
                "'산업안전공단 신고 방법?', '최근 E-9 비자 정책 업데이트?'"
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "웹에서 검색할 자연어 질의. 한국어 가능. "
                                "필요하면 연도/지역 같은 한정자를 포함해 정확도를 높이세요.\n"
                                "예: '2026년 한국 최저임금', '한국 외국인 근로자 고용허가제'"
                            ),
                        },
                        "max_results": {
                            "type": "number",
                            "description": (
                                "반환할 결과 수 (기본 5, 권장 3~7). "
                                "큰 값은 응답 시간을 늘립니다."
                            ),
                        },
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

    if tool_name == "search_community_posts":
        return _search_community_posts(
            query=str(tool_input.get("query", "")),
            limit=int(tool_input.get("limit", 3)),
        )

    if tool_name == "search_web":
        return _search_web(
            query=str(tool_input.get("query", "")),
            max_results=int(tool_input.get("max_results", 5)),
        )

    return f"Unknown tool: {tool_name}"
