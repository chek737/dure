# 관리자·Agent API 계약

이 문서는 Dure Control Plane HTTP API를 운영 자동화와 Agent 구현 관점에서 설명한다. 이는 공개 inference API가 아니다. API는 신뢰된 관리망과 승인된 Agent를 위한 제어면이며, vLLM `127.0.0.1:8000` endpoint와 구분된다.

정확한 request/response schema는 실행 중인 Controller의 OpenAPI schema와 해당 package version의 Pydantic 모델을 기준으로 한다. 이 문서는 보안 경계, 재시도, 오류 처리, 민감 정보 취급처럼 자동화가 반드시 지켜야 할 공통 계약을 고정한다.

## OpenAPI schema와 문서 경로의 노출 정책

현재 Controller는 FastAPI 기본값을 사용하므로 `/docs`, `/redoc`, `/openapi.json`을 생성한다. 이 경로들은
admin bearer 자체로 보호되지 않는 schema·문서 경로이므로, reverse proxy가 인터넷에 `443`을 열었다고
해서 함께 공개하면 안 된다.

| 환경 | `/docs`·`/redoc`·`/openapi.json` 정책 | `/health` 정책 |
| --- | --- | --- |
| 개발·격리 검증 | loopback 또는 개발자 관리망에서만 허용 | loopback에서 확인 |
| 신뢰된 운영망 | VPN·관리 IP·별도 proxy 인증을 통과한 운영자만 허용 | load balancer/운영 monitoring만 허용 |
| 인터넷 노출 reverse proxy | proxy에서 `403` 또는 `404`로 차단 | 기본 차단. 별도 public 상태 페이지가 필요하면 민감 정보 없는 별도 service를 설계 |

Controller와 Control Plane API는 공개 inference endpoint가 아니다. artifact chunk를 외부 origin으로 제공해야
하는 경우에도 `/chunks/sha256/{digest}`만 신뢰된 artifact 경계로 분리하고, schema·admin·Agent 경로를
같은 공개 route에 포함하지 않는다. reverse proxy 변경 뒤에는 관리망 밖에서 세 schema 경로가 `403` 또는
`404`인지, Controller host loopback에서만 필요한 schema가 보이는지 확인한다.

## API 표면과 인증 주체

| API 영역 | 주요 경로 | 인증 주체 | 용도·제한 |
| --- | --- | --- | --- |
| 상태 | `GET /health` | 없음 | load balancer와 loopback 상태 점검용. 민감 정보나 상세 설정을 반환하지 않음 |
| artifact chunk | `GET`/`HEAD /chunks/sha256/{digest}` | 없음 | content-addressed chunk 전달. 목록·임의 파일 탐색 API가 아니며 신뢰된 artifact origin 안에서만 제공 |
| enrollment | `POST /v1/admin/enrollments`, `POST /v1/enrollments/claim`, `POST /v1/nodes/join` | 관리자는 admin bearer, claim/join은 일회성 enrollment 또는 join 입력 | 발급된 token·credential은 비밀이며 반환 직후 안전한 저장소에만 보관 |
| Agent | `/v1/agent/*` | 해당 node의 bearer credential | heartbeat, unjoin, task claim/lease, task complete/fail, artifact manifest 조회. pending node는 heartbeat 가능하지만 task를 받지 못함 |
| 관리자 | `/v1/admin/*` | `Authorization: Bearer <DURE_ADMIN_TOKEN>` | node 승인·revocation, inventory, artifact, qualification, Fleet, deployment, task 관리 |

관리 bearer와 node bearer는 서로 대체할 수 없다. Agent는 자신에게 발급된 credential과 server-issued node UUID만 사용해야 하며, hostname이나 다른 노드 credential으로 작업을 수행해서는 안 된다. Controller는 임의 shell command, Docker argument, URL, Python code를 task payload로 받지 않는 폐쇄형 task enum만 처리한다.

## 주요 endpoint와 상태 전이

아래 표는 운영자가 자동화에서 가장 자주 다루는 endpoint의 request/response 경계를 요약한다. 정확한
필드명·enum·필수 여부는 같은 Controller version의 `/openapi.json`을 사용하며, response의 사람용
`message`를 제어 흐름으로 사용하지 않는다.

| endpoint | 요청 주체·입력 | 성공 응답의 핵심 | 상태 전이·안전 경계 |
| --- | --- | --- | --- |
| `POST /v1/admin/enrollments` | admin bearer, 만료 시간 | enrollment `id`, 일회성 `token`, `expires_at` | enrollment 생성만 수행. token은 한 번의 비밀 전달로 취급 |
| `POST /v1/enrollments/claim` | enrollment claim 입력 | `node_id`, node `credential` | claim된 node identity와 credential을 발급. 재출력·공유 금지 |
| `POST /v1/nodes/join` | Agent의 install ID·profile | `node_id`, `credential`, `pending` | 새 node는 pending으로 시작하며 task를 받지 못함 |
| `POST /v1/admin/nodes/{node_id}/approve` | admin bearer | `ok`, `node_id`, `approved` | pending → 승인 가능. heartbeat와 별개로 운영자 승인이 필요 |
| `POST /v1/admin/nodes/{node_id}/revoke` | admin bearer | `ok` | task 수신을 격리. host 정리·unjoin을 자동 수행하지 않음 |
| `POST /v1/admin/nodes/{node_id}/credential` | admin bearer | `node_id`, 새 raw `credential` | 기존 credential을 즉시 revoke. [Agent 회전 runbook](agent-operations.md)을 따라야 함 |
| `POST /v1/agent/heartbeat` | node bearer, profile·상태 | `ok`, `approved` | node 관측 상태를 갱신. pending이면 task 권한은 생기지 않음 |
| `POST /v1/agent/tasks/claim` | node bearer | `task` 또는 pending/빈 task | 승인 node만 lease를 claim. 한 node는 한 leased task만 실행 |
| `POST /v1/agent/tasks/{task_id}/heartbeat` | node bearer, 선택 progress | `lease_until` | 현재 node·task·lease가 맞을 때만 연장 |
| `POST /v1/agent/tasks/{task_id}/complete`·`fail` | node bearer, 폐쇄형 결과 | terminal task 결과 | 과거 lease나 다른 node의 결과는 현재 시도를 바꾸지 못함 |
| `POST /v1/admin/profile-qualifications/prepare` | admin bearer, exact profile·node/GPU binding | qualification, `created` | `DRAFT → QUALIFYING` 준비. 실제 다중 노드 시험을 자동 실행하지 않음 |
| `POST /v1/admin/profile-qualifications/{run_id}/evidence` | admin bearer, 구조화된 evidence | qualification, evidence, `created` | exact 증적 등록 후에만 `VALIDATED`/활성화 경로를 판단 |
| `POST /v1/admin/fleet-recommendations` | admin bearer, node 범위·objective | 불변 recommendation ID와 후보 | 읽기 전용 추천. 다운로드·컨테이너·task를 만들지 않음 |
| `POST /v1/admin/fleet-recommendations/{id}/accept` | admin bearer | fleet·reservation·generation 정보 | inventory·증적을 재검사하고 전체 예약을 원자적으로 생성. 일부 성공 없음 |
| `POST /v1/admin/fleets/{id}/prepare`·`apply` | admin bearer | Fleet runtime 상태·변경 결과 | prepare와 apply를 분리. 실패 배포를 자동 rollback·중지·예약 해제하지 않음 |
| `POST /v1/admin/deployments/{id}/prepare` | admin bearer, `request_id`, 선택 artifact digest, `apply` | preparation, task 목록, `changed` | `apply=false`은 preview. `apply=true`만 준비 task를 생성하며 동일 request ID를 재검사 |
| `POST /v1/admin/deployments/{id}/rollback` | admin bearer, 전체 `node_ids`, `apply`, `serve` | operation, task 목록, `changed` | `apply=false`은 안전성 검사. apply 뒤에도 exact cache·image 재검사 실패 시 시작 task를 만들지 않음 |
| `GET /v1/admin/tasks`·`POST /v1/admin/tasks/{id}/cancel` | admin bearer | 최신 task 목록·terminal/cancel 결과 | 목록은 최신 200개. cancel은 현재 상태·참조 게이트를 통과할 때만 변경 |

artifact, runtime/model release, placement profile, benchmark evidence와 deployment recommendation endpoint도
동일한 admin bearer 경계를 사용한다. admin API는 resource 생성·상태 전이용이며, 일반 사용자의 모델
추론 요청을 받는 API가 아니다.

## 관리자 API의 기능 묶음

`/v1/admin/*`에는 다음의 제어면 영역이 있다.

| 영역 | 예시 작업 | 변경 안전 원칙 |
| --- | --- | --- |
| enrollment·node·inventory | enrollment 생성, 노드 조회·승인·revoke, credential 재발급, inventory 조회 | pending node는 운영자 승인 전 task를 claim하지 못함 |
| artifact·cache | manifest·chunk 등록, cache 상태·격리·참조 조회 | URL·token을 task/DB 결과에 넣지 않으며, 격리는 preview와 명시적 apply를 분리 |
| runtime·model·profile·qualification | runtime/model release, 배치 프로필, benchmark/NCCL 증적 등록·조회 | 추정 결과만으로 `VALIDATED`를 만들지 않으며 exact node/GPU 증적을 결합 |
| recommendation·Fleet | 단일 recommendation, Fleet recommendation 생성·조회·수락 | recommend/accept는 host 변경 권한이 아니며, 수락 시 inventory·증적을 재검사 |
| deployment·generation | deployment 생성, prepare/apply/verify/rollback 상태 조회·진행 | OCI image digest와 exact artifact/cache identity를 요구하고 자동 rollback을 추측하지 않음 |
| task | 폐쇄형 task 생성·조회·cancel | 한 node는 한 leased task만 수행하고 task handler는 재시도 안전해야 함 |

CLI는 일반 운영에 권장되는 표면이다. 직접 API 자동화를 만들 때는 특정 package version에서 문서화된 endpoint와 schema만 사용하고, DB를 직접 수정하거나 undocumented response field·정렬 순서에 의존하지 않는다.

## 재시도·멱등성·동시성

- `GET`과 `HEAD`는 읽기 전용이지만 관측 시점에 따라 결과가 달라질 수 있다. artifact chunk는 immutable digest와 Range request를 지원하는 전달 endpoint이다.
- 모든 `POST`가 자동으로 멱등인 것은 아니다. request schema에 `request_id` 또는 immutable recommendation identity가 명시된 경우에만 그 endpoint의 문서화된 재시도 규칙을 사용한다.
- 응답을 받지 못한 변경 요청은 같은 요청을 무한 재전송하지 말고, 해당 resource·task·recommendation 상태를 조회해 결과를 먼저 확인한다.
- Fleet 수락과 deployment generation은 transaction 안에서 inventory, evidence, node/GPU 중복을 재검사한다. 충돌이 나면 일부 노드만 성공한 것으로 간주하지 않는다.
- Agent lease heartbeat·complete·fail은 task와 node UUID가 일치해야 한다. lease 만료 또는 상태 충돌 응답을 받으면 이전 task 결과를 새 task에 재사용하지 않는다.

## 목록과 pagination

현재 API는 범용 cursor pagination 계약을 제공하지 않는다. 예를 들어 task 목록은 최신순 최대 200개를 반환한다. 목록 endpoint의 반환 개수·순서·누락 없는 전체 export를 외부 호환 계약으로 가정하면 안 된다. 대량 감사·보존·분석이 필요하면 운영자가 승인한 별도 export 절차를 만들고, production DB를 ad-hoc query로 변경하지 않는다.

## HTTP 오류와 failure code

| HTTP 상태 | 의미 | 자동화 처리 |
| --- | --- | --- |
| `400` | 닫힌 요청 규칙, 상태 전이, 입력 조합이 거부됨 | 요청을 수정하거나 운영자 판단을 받음; 동일 요청 반복 금지 |
| `401` | bearer가 없거나 유효하지 않음 | 비밀 저장소·token 회전 상태를 확인; token을 로그에 출력하지 않음 |
| `404` | resource가 없거나 artifact endpoint가 안전하게 존재를 숨김 | 식별자·권한·retention을 확인; 경로 탐색을 시도하지 않음 |
| `409` | lease, inventory, evidence, deployment/rollback, cache 상태의 충돌 | 최신 상태를 조회하고 closed failure code에 맞는 복구 절차로 전환 |
| `422` | request schema 검증 실패 | OpenAPI/schema와 client version을 맞추고 unknown field를 추측하지 않음 |
| `5xx` | 예기치 않은 Controller·의존 서비스 오류 | retry budget을 제한하고 redaction된 운영 로그와 health를 확인 |

일부 운영 오류는 `code`, `message`, `details`를 가진 구조화된 body로 반환된다. 자동화는 사람이 읽는 `message`나 HTTP framework의 자유 텍스트를 파싱하지 말고, endpoint가 명시적으로 제공하는 closed `code`와 HTTP 상태를 사용한다. endpoint마다 code 집합은 다르며, 새 code를 임의 성공으로 취급하면 안 된다.

## 민감 정보와 로그

- `DURE_ADMIN_TOKEN`, enrollment token, node credential, `/etc/dure/server.env`, `/etc/dure/agent.json`, model token은 request example, shell history, CI log, task 결과, benchmark evidence에 기록하지 않는다.
- credential을 반환하는 enrollment·claim·join·재발급 응답은 비밀 전달로 취급한다. 필요한 순간에만 수신해 권한이 제한된 저장소에 넣고, API 응답 전체를 ticket이나 chat에 붙여넣지 않는다.
- artifact endpoint는 digest 기반이라도 public inference 또는 public file browser가 아니다. origin의 TLS·network 접근 경계는 [네트워크·방화벽 운영 절차](networking.md)를 따른다.
- API error의 `details`에도 비밀을 넣지 않는다. 지원 요청에는 token·header·원시 환경 파일 대신 redaction된 request ID, HTTP 상태, failure code, 시간, node UUID만 공유한다.

## 최소 상태 점검 예시

다음 health check는 비밀을 보내지 않는다. 운영자 API 호출은 admin token을 안전한 process 환경이나 비밀 주입 수단으로 전달하고, public terminal history에 실제 값을 쓰지 않는다.

```bash
curl -fsS http://127.0.0.1:8081/health
```

API 공개 경로, reverse proxy, 보안 그룹은 [네트워크·방화벽 운영 절차](networking.md)를, credential 회전과 장애 복구는 [PostgreSQL 백업·복구·재해 복구](disaster-recovery.md)를 따른다.
