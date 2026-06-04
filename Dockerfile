# 챗봇 Lambda 컨테이너 이미지 — Lambda Web Adapter + FastAPI + Python 3.12
#
# ──────────────────────────────────────────────────────────────────────────────
# 왜 이 패턴인가
# ──────────────────────────────────────────────────────────────────────────────
# AWS Lambda Function URL의 RESPONSE_STREAM 모드(SSE 토큰 점진 전송)는
# Python 런타임에서 직접 API를 제공하지 않는다 (Node.js만 streamifyResponse 보유).
# Lambda Web Adapter(LWA)는 Lambda extension으로 동작하면서 일반 ASGI/WSGI 앱을
# Function URL의 streaming 응답에 그대로 노출한다. 그래서 FastAPI를 그대로 쓰면 됨.
#
# 결과 구조:
#   [Function URL + RESPONSE_STREAM]
#       ↓
#   [LWA extension] HTTP → uvicorn(8080) ASGI
#       ↓
#   [FastAPI src/app.py] StreamingResponse(SSE)
#       ↓
#   [chat_once() in src/chatbot.py] Bedrock + Tool Use 토큰 generator
#
# ──────────────────────────────────────────────────────────────────────────────
# 빌드 / 배포 (계정 B)
# ──────────────────────────────────────────────────────────────────────────────
#   docker build -t gb-chatbot-lambda .
#
#   # ECR 레포 1회 생성
#   aws ecr create-repository --repository-name gb-chatbot \
#       --region ap-northeast-2 --profile gb-account-b
#
#   # 로그인
#   aws ecr get-login-password --region ap-northeast-2 --profile gb-account-b \
#       | docker login --username AWS --password-stdin \
#           713729793436.dkr.ecr.ap-northeast-2.amazonaws.com
#
#   # 태그 + 푸시
#   docker tag gb-chatbot-lambda:latest \
#       713729793436.dkr.ecr.ap-northeast-2.amazonaws.com/gb-chatbot:latest
#   docker push \
#       713729793436.dkr.ecr.ap-northeast-2.amazonaws.com/gb-chatbot:latest
#
# Lambda 함수 생성 (콘솔 또는 CLI):
#   - Package type: Image, 위 ECR URI 지정
#   - Memory: 1024MB+ (Bedrock 호출 + Tool 루프 고려)
#   - Timeout: 60s+ (Tool 루프 최대 5회, KB retrieve + MCP 호출 포함)
#   - Function URL: 활성화 + Invoke mode = RESPONSE_STREAM + Auth = AWS_IAM
#   - 환경변수: BEDROCK_MODEL_ID, LEGAL_KB_ID, MCP_EXCHANGE_URL, MCP_COMMUNITY_URL
#   - IAM execution role: bedrock:InvokeModelWithResponseStream + bedrock:Retrieve
#                         + CloudWatch logs
#   - 백엔드(계정 A) IAM에 lambda:InvokeFunctionUrl on this function 추가
#
# ──────────────────────────────────────────────────────────────────────────────

FROM public.ecr.aws/docker/library/python:3.12-slim

# Lambda Web Adapter extension — Function URL을 ASGI 앱으로 어댑팅 + streaming 지원
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.8.0 \
     /lambda-adapter /opt/extensions/lambda-adapter

WORKDIR /var/task

# pip 캐시 최적화: requirements만 먼저 복사
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 애플리케이션 코드
COPY src/ ./src/

# LWA가 RESPONSE_STREAM 모드로 동작하도록 설정 (이게 있어야 chunked SSE가 흘러감)
ENV AWS_LWA_INVOKE_MODE=RESPONSE_STREAM
# uvicorn이 바인딩할 포트 — LWA의 기본값
ENV PORT=8080
# Python stdout 버퍼링 비활성화 — 실시간 로그 + 스트리밍 보장
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

# uvicorn으로 FastAPI 앱 띄움. workers 1로 두는 게 Lambda 동시성 모델에 맞음.
CMD ["python", "-m", "uvicorn", "src.app:app", \
     "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
