# gb-chatbot-lambda

> Global Bridge 챗봇 Lambda. 외국인 노동자가 업로드한 계약서를 AI가 분석한 후, 사용자의 후속 질문에 답하는 챗봇.

---

## 아키텍처 한 줄

```
[Spring 백엔드] ──IAM SigV4──→ [이 Lambda] ──→ [Bedrock(Claude 4 Sonnet)]
                                     │
                                     ├─→ [MCP1 환율 (K8s)]    ← gb-mcp-servers
                                     ├─→ [MCP2 커뮤니티 (K8s)] ← gb-mcp-servers
                                     └─→ [Bedrock KB (법령)]   ← 분석 파이프라인과 공유
```

- **호출**: AWS Lambda Function URL + Response Streaming + IAM 인증
- **모델**: `global.anthropic.claude-sonnet-4-6` (최신 Sonnet 4.6)
- **흐름**: 사용자 질문 → Tool Use 루프(도구 호출 판단) → 최종 답변 토큰 스트림

---

## 폴더 구조

```
gb-chatbot-lambda/
├── src/
│   ├── handler.py          # Lambda 진입점 (Function URL + Response Streaming)
│   ├── chatbot.py          # Bedrock converse_stream 핵심 로직
│   ├── system_prompt.py    # 시스템 프롬프트 (역할 정의)
│   ├── tools.py            # (Phase 4 2단계) MCP 클라이언트 + Tool Use 도구 정의
│   └── __init__.py
├── tests/
│   ├── local_run.py        # 로컬에서 chatbot.py 직접 호출 (Lambda 우회)
│   └── __init__.py
├── requirements.txt
├── .env.example
└── README.md
```

---

## 단계별 작업 (Phase 4)

| 단계 | 내용 | 검증 시나리오 |
| --- | --- | --- |
| **1단계** | 챗봇 뼈대 — Bedrock `converse_stream` 호출 | "이 계약서 어때?" (도구 호출 없음) |
| 2단계 | Tool Use 루프 + MCP1 환율 도구 연결 | "월급 베트남 돈으로?" |
| 3단계 | MCP2 커뮤니티 + 법령 KB 추가 | "비슷한 경험 한 사람?", "외국인도 최저임금 미달이 불법?" |
| 4단계 | AWS Lambda Function URL 배포 + EventBridge 워밍업 | 실제 백엔드에서 호출 |

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

---

## 의존성 + IAM 권한

### Python 패키지
- `boto3` — AWS SDK (Bedrock + Lambda 호출)
- `redis` (2단계 이후) — MCP 도구 응답 캐시용 (옵션)

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

---

## 관련 문서 (gb-backend)

- `docs/document-analysis/ai-chatbot-mcp.md` — 챗봇 + MCP 정본
- `docs/document-analysis/result-json-schema-agreement.md` — 분석 결과 스키마 v1.1
- `AI-WORK-SPLIT.md` — AI 파트 작업 분담

---

*Sangam-Beavers / Global Bridge*
