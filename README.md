# gb-chatbot-lambda

> Global Bridge 챗봇 Lambda. 외국인 노동자가 업로드한 계약서를 AI가 분석한 후, 사용자의 후속 질문에 답하는 챗봇.
> **+ 커뮤니티 게시글·댓글 다국어 번역 Lambda** (같은 레포, 별도 배포 단위).

---

## 아키텍처 한 줄

```
[Spring 백엔드] ──IAM SigV4──→ [챗봇 Lambda] ──→ [Bedrock(Claude 4.6 Sonnet)]
                                     │
                                     ├─→ [Bedrock KB (법령)]              ← 우리 도메인 (분석과 KB 공유)
                                     ├─→ [MCP1 환율 (K8s)]                ← gb-mcp-servers (같은 회사 다른 팀)
                                     ├─→ [MCP2 커뮤니티 (K8s)]            ← gb-mcp-servers (같은 회사 다른 팀)
                                     └─→ [MCP3 Tavily Remote MCP] ⭐      ← 외부 회사 직결 (mcp.tavily.com)

[Spring 백엔드] ──IAM SigV4──→ [번역 Lambda] ──→ [Bedrock(Claude Haiku)]   ← 동기 1회 호출, 스트리밍 X
```

- **호출**: AWS Lambda Function URL + Response Streaming + IAM 인증
- **모델**: `global.anthropic.claude-sonnet-4-6` (최신 Sonnet 4.6)
- **흐름**: 사용자 질문 → Tool Use 루프(도구 4종 중 선택 호출) → 최종 답변 토큰 스트림
- **MCP3 특이점**: 자체 어댑터 서버 없이 Tavily 가 운영하는 공식 Remote MCP Server (`https://mcp.tavily.com/mcp/`) 에
  Python `mcp` SDK 의 Streamable HTTP 클라이언트로 직접 붙는다. MCP 본래 가치(외부 회사 시스템을 표준 인터페이스로 통합) 정면 사례.

---

## 폴더 구조

```
gb-chatbot-lambda/
├── src/
│   ├── handler.py                     # 챗봇 Lambda 진입점 (Function URL + Response Streaming)
│   ├── chatbot.py                     # 챗봇 — Bedrock converse_stream 핵심 로직
│   ├── system_prompt.py               # 챗봇 시스템 프롬프트
│   ├── tools.py                       # (Phase 4 2단계) MCP 클라이언트 + Tool Use 도구 정의
│   ├── translator_handler.py          # 번역 Lambda 진입점 (Function URL, 동기 JSON)
│   ├── translator.py                  # 번역 — Bedrock converse (Haiku) 동기 호출
│   ├── translator_system_prompt.py    # 번역 시스템 프롬프트
│   └── __init__.py
├── tests/
│   ├── local_run.py                   # 챗봇 로컬 시나리오 실행
│   ├── local_translator_run.py        # 번역 Lambda 로컬 실행
│   ├── test_translator.py             # 번역 코어 단위 테스트 (Bedrock mock)
│   ├── test_translator_handler.py     # 번역 Lambda 핸들러 테스트
│   └── __init__.py
├── requirements.txt
├── .env.example
└── README.md
```

## 두 Lambda 의 분리

| 구분 | 챗봇 Lambda | 번역 Lambda |
| --- | --- | --- |
| 함수명 | `gb-chatbot-{dev|stage|prod}` | `gb-community-translator-{dev|stage|prod}` |
| 진입점 | `src.handler` | `src.translator_handler.handler` |
| 모델 | Sonnet 4.6 (`global.anthropic.claude-sonnet-4-6`) | Haiku (`BEDROCK_MODEL_ID` 환경변수 주입) |
| 호출 방식 | Function URL + IAM + Response Streaming (SSE) | Function URL + IAM + 동기 JSON |
| Tool Use | 4개 도구 (환율/법령/커뮤니티/Tavily) | 없음 (1회 InvokeModel) |
| 사용처 | 분석 결과 후속 질문 챗봇 | 커뮤니티 게시글·댓글 번역 (#161) |

배포는 Serverless Framework 등으로 각각 분리. 코드만 같은 레포에 둔다(공유 의존성/IAM 패턴 재사용).

---

## 단계별 작업 (Phase 4)

| 단계 | 내용 | 검증 시나리오 |
| --- | --- | --- |
| **1단계** | 챗봇 뼈대 — Bedrock `converse_stream` 호출 | ① "이 계약서 어때?" (도구 호출 없음) |
| 2단계 | Tool Use 루프 + MCP1 환율 도구 연결 | ② "월급 베트남 돈으로?" |
| 3단계 | MCP2 커뮤니티 + 법령 KB 추가 | ③ "외국인도 최저임금 미달이 불법?", ④ "비슷한 경험 한 사람?" |
| 4단계 | AWS Lambda Function URL 배포 + EventBridge 워밍업 | 실제 백엔드에서 호출 (P1 통합 검증) |
| **5단계** | **외부 Tavily Remote MCP 직결** (search_web) | ⑤ "올해 한국 외국인 최저임금 얼마야?" |

---

## 로컬 실행 (1단계 — 시나리오 1)

```bash
# 가상환경
python -m venv .venv
source .venv/Scripts/activate    # Windows Git Bash
pip install -r requirements.txt

# AWS 자격증명 (계정 B)
export AWS_REGION=ap-northeast-2
# 또는 aws configure로 ~/.aws/credentials 설정

# 로컬 테스트 실행
python -m tests.local_run
```

기대 출력:
```
사용자: 이 계약서 어때?

챗봇: 이 계약서에는 몇 가지 주의해야 할 점이 있습니다. 첫째, 위험도가 HIGH로 평가되었어요...
```

### 시나리오 ⑤ 단독 검증 (외부 Tavily MCP)
```bash
# Tavily API 키 발급 (https://app.tavily.com/home, Researcher Plan 월 1,000 search 무료)
export TAVILY_API_KEY=tvly-...

python -m tests.local_run --only 5
```

---

## 도구 4종 + 통합 패턴

| # | 도구 (Bedrock 뷰) | 패턴 | 데이터 소스 | 비고 |
| --- | --- | --- | --- | --- |
| 1 | `search_legal_standard` | Bedrock KB `retrieve` | S3 Vectors (법령 벡터) | 분석 파이프라인과 KB 공유 |
| 2 | `get_exchange_rate` | 자체 MCP1 (FastMCP + Redis) | 송금팀 Redis `rate:KRW-*` | 같은 회사 다른 팀 |
| 3 | `search_community_posts` | 자체 MCP2 (FastMCP + MySQL) | 커뮤팀 `community_db.posts/comments` | 같은 회사 다른 팀 |
| 4 | `search_web` | **외부 Tavily Remote MCP 직결** ⭐ | Tavily 검색 엔진 | `mcp.tavily.com`, 내부 실명 `tavily_search` |

> **MCP3 명명 매핑**: Bedrock toolSpec 이름 규칙은 `[a-zA-Z][a-zA-Z0-9_]*` 만 허용. 따라서
> 외부 표시는 `search_web` 으로 등록하고, `execute_tool` 에서 Tavily 의 실제 도구명 `tavily_search` (underscore)
> 로 매핑한다. Tavily docs 페이지엔 `tavily-search` (hyphen) 로 적혀있으나 실제 `list_tools` 응답은
> underscore — 실측 기준으로 박음.

---

## 의존성 + IAM 권한

### Python 패키지
- `boto3` — AWS SDK (Bedrock + Lambda 호출)
- `mcp` — Anthropic MCP Python SDK (자체 MCP1/2 + 외부 Tavily MCP 모두 클라이언트로 호출)

### IAM 권한 (Lambda 실행 롤)
- `bedrock:InvokeModelWithResponseStream`
- `bedrock:Converse` / `bedrock:ConverseStream`
- `bedrock:Retrieve` (3단계, 법령 KB)
- `lambda:InvokeFunctionUrl` (백엔드 측 권한 — 백엔드 롤에 부여)
- `dynamodb:Query` / `dynamodb:PutItem` / `dynamodb:BatchWriteItem` — `chat_sessions` 테이블 ARN 한정 (대화기록, 아래 참고)

---

## 대화기록 영속화 — DynamoDB `chat_sessions`

> 정본: `gb-backend/docs/document-analysis/ai-chatbot-mcp.md` §3-4/§3-5/§7. 코드: `src/storage.py`.

- 스레드 정체성 = `(user_public_id, document_public_id)`. session_id는 로깅용 메타.
- 환경변수 **`DYNAMODB_TABLE_NAME`** 미설정 시 영속화가 조용히 꺼진다(로컬 `tests/local_run.py`는 그대로 동작).
- 첫 턴에 주입한 합성 요약 2턴도 `visible=false`로 저장 — 복구 시 요약 유실 방지.

### 테이블 생성 (계정 B, 1회)

```bash
aws dynamodb create-table \
  --table-name chat_sessions \
  --attribute-definitions AttributeName=PK,AttributeType=S AttributeName=SK,AttributeType=S \
  --key-schema AttributeName=PK,KeyType=HASH AttributeName=SK,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST \
  --region ap-northeast-2

# TTL 활성화 (90일 자동 삭제 — PII 보관기간)
aws dynamodb update-time-to-live \
  --table-name chat_sessions \
  --time-to-live-specification "Enabled=true, AttributeName=ttl" \
  --region ap-northeast-2
```

### 단위 테스트

```bash
python -m tests.test_storage    # DynamoDB Mock — AWS 자격증명 불필요
```

### Lambda Configuration 환경변수
- `AWS_REGION` — `ap-northeast-2`
- `BEDROCK_MODEL_ID` — `global.anthropic.claude-sonnet-4-6`
- `LEGAL_KB_ID` — `KENYUCA5DE`
- `MCP_EXCHANGE_URL` — 자체 MCP1 (환경별 dev/stage/prod)
- `MCP_COMMUNITY_URL` — 자체 MCP2 (환경별 dev/stage/prod)
- **`TAVILY_API_KEY`** — 외부 Tavily MCP 인증. 운영기엔 Secrets Manager 에서 주입.

---

## 번역 Lambda (gb-community-translator)

게시글·댓글 다국어 번역 전용 Lambda. 같은 레포에 코드만 함께 둔다.

```
[Spring 백엔드] ──IAM SigV4──→ POST / (Function URL) ──→ Bedrock Claude Haiku ──→ JSON 응답
```

### API 계약 (Spring 백엔드 ↔ 번역 Lambda)

요청:
```json
{
  "kind": "post",
  "public_id": "9d4c1a98-...",
  "title": "원문 제목 또는 null(댓글)",
  "content": "원문 본문",
  "source_lang": "ko",
  "target_lang": "vi"
}
```

응답 (200):
```json
{
  "translated_title": "...",
  "translated_content": "...",
  "target_lang": "vi",
  "model_id": "global.anthropic.claude-haiku-4-5",
  "input_tokens": 123,
  "output_tokens": 456
}
```

에러 (4xx/5xx):
```json
{"error_code": "INVALID_INPUT | UNSUPPORTED_LANGUAGE | BEDROCK_ERROR", "message": "..."}
```

### 로컬 테스트

```bash
# 가상환경 활성화 후
python -m tests.local_translator_run                     # 기본 시나리오
python -m tests.local_translator_run --only 2            # 댓글 번역만

# 단위 테스트 (Bedrock 호출 mock)
python -m pytest tests/test_translator.py tests/test_translator_handler.py -v
```

`source_lang == target_lang` 인 경우 Bedrock 호출 없이 원문을 그대로 반환한다(비용 절감).
지원 언어: `ko`, `en`, `vi`, `fil` (+ `th`, `id`, `tl` 폴백).

---

## 관련 문서 (gb-backend)

- `docs/document-analysis/ai-chatbot-mcp.md` — 챗봇 + MCP 정본
- `docs/document-analysis/result-json-schema-agreement.md` — 분석 결과 스키마 v1.1
- `AI-WORK-SPLIT.md` — AI 파트 작업 분담

---

*Sangam-Beavers / Global Bridge*
