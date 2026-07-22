# 관측·장애 대응 운영 절차

이 문서는 Dure의 day-2 상태 확인, 로그 취급, 수동 장애 대응과 외부 알림 기준을 정리한다. 현재
Dure는 Prometheus metrics endpoint, Alertmanager 규칙, 중앙 log retention, 자동 failover 또는 자동
rollback을 제공하지 않는다. 아래 신호를 운영자가 사용하는 monitoring·ticket·on-call 체계에 연결해야
하며, Dure가 알림을 보냈다고 가정해서는 안 된다.

## 현재 확인 가능한 신호

| 계층 | 확인 수단 | 정상 판단 | 주의 |
| --- | --- | --- | --- |
| Controller process | `systemctl status dure-server`, `journalctl -u dure-server` | service가 실행 중이고 재시작 반복이 없음 | `dure-server.service`는 배포자가 template을 설치한 경우의 이름 |
| Controller HTTP | loopback `GET /health` | `ok=true`와 기대 version 반환 | 외부 공개 health check로 사용하지 않음 |
| PostgreSQL 연결 | Controller 시작·migration의 연결 검사, 운영 DB monitoring | DB 연결 오류·복구 반복이 없음 | DB password·URL을 로그나 alert payload에 넣지 않음 |
| Agent process | `systemctl status dure-agent`, `journalctl -u dure-agent` | service 실행·최근 heartbeat | Agent는 outbound polling만 수행 |
| 중앙 node 상태 | `dure admin nodes --online`, `dure admin nodes --pending` | 승인 node가 의도한 connectivity | pending은 장애가 아니라 승인 전 상태일 수 있음 |
| 작업·lease | `dure admin tasks --watch`, deployment/Fleet 상태 조회 | 동일 node의 leased task가 하나이며 단계가 의도대로 진행 | 실패 코드와 현재 operation·시도 번호를 함께 확인 |
| 배포·캐시 | deployment generation, preparation, artifact-cache 조회 | exact `READY`와 현재 준비 증적 | `READY`는 실제 장기 안정성·NCCL 재측정을 뜻하지 않음 |

## 일상 점검

Controller host에서 실행한다.

```bash
sudo systemctl status dure-server --no-pager
curl -fsS http://127.0.0.1:8081/health
sudo journalctl -u dure-server -n 100 --no-pager
```

관리자 workstation에서는 안전한 `--env-file` 또는 권한이 제한된 환경으로 admin credential을 주입한
뒤 다음을 확인한다.

```bash
dure admin nodes --online
dure admin nodes --pending
dure admin tasks --watch
```

GPU node에서는 다음만 확인한다. Agent JSON·server env의 원문을 journal 또는 ticket에 출력하지
않는다.

```bash
sudo systemctl status dure-agent --no-pager
sudo journalctl -u dure-agent -n 100 --no-pager
```

## 외부 알림 기준

아래 기준은 Dure가 자동으로 발행하는 규칙이 아니라, 운영자가 monitoring에 구현해야 할 최소 정책이다.
임계값은 workload lease, 유지보수 창, RPO/RTO에 맞춰 정하고 변경 이력을 남긴다.

| 심각도 | 감지 신호 | 즉시 조치 |
| --- | --- | --- |
| 긴급 | Controller health 실패, service restart 반복, admin token·node credential 노출 의심 | 관리 API 접근을 제한하고 비밀 회전·복구 절차를 시작. 원인 확인 전 task를 임의 재실행하지 않음 |
| 높음 | PostgreSQL 연결 실패, 승인 node 다수가 offline, 배포의 필수 단계 실패 | 해당 deployment/Fleet의 다음 apply를 중단하고 `nodes`, task, preparation/generation 상태를 보존·조사 |
| 중간 | 한 node의 heartbeat 손실, lease 충돌·만료, cache `CORRUPT`/`MISSING`, 반복된 closed failure code | node를 자동 재배정하지 않고 revoke·격리 여부를 판단. cache는 preview 후 명시적 준비 또는 quarantine 사용 |
| 낮음 | pending node 장기 체류, 디스크·캐시 여유 경고, 단일 benchmark 실패 | 승인·용량·지원 조합을 검토하고 다음 유지보수 창에 조치 |

다중 노드 network/NCCL 증적은 24시간의 freshness 제한을 가질 수 있지만, 이것은 24시간 연속 실행을
검증했다는 뜻이 아니다. 증적 만료·실패는 새 추천을 안전하게 거부하는 신호로 취급하며, port를 넓게
개방하거나 자동으로 다른 모델·노드·캐시 형식으로 바꾸지 않는다.

## 장애 대응 순서

1. **변경을 멈춘다.** 실패한 apply/rollback을 반복 실행하거나 DB 행·task·Alembic revision을 직접
   고치지 않는다.
2. **상태를 보존한다.** 시간, node UUID, deployment/Fleet/recommendation ID, task ID, operation 단계,
   시도 번호, HTTP 상태와 closed failure code만 기록한다.
3. **경계를 확인한다.** Controller health, PostgreSQL, Agent service, node connectivity, exact cache
   identity, digest image 준비와 사설 네트워크를 순서대로 조사한다.
4. **명시적으로 복구한다.** 필요한 경우 credential revoke/rotation, `unjoin`, 준비 재시도,
   deployment rollback을 각각의 runbook 절차로 수행한다. 자동 rollback·자동 cache 삭제·자동 failover는
   없다.
5. **사후 확인한다.** health, node heartbeat, task terminal 상태와 배포 검증을 확인하고, 실제 GPU/NCCL
   결과는 [릴리스 수용 증적 기록](release-evidence/README.md)에 `PASSED`·`FAILED`·`NOT_RUN`으로 남긴다.

## 로그 보존과 redaction

- systemd journal 보존 기간·중앙 전송·접근 권한은 Dure가 설정하지 않는다. 운영자는 host policy로
  보존 기간, 암호화, 접근 감사와 삭제 절차를 정한다.
- 로그·alert·ticket에는 `DURE_ADMIN_TOKEN`, enrollment token, node credential, `server.env`,
  `agent.json`, Authorization header, database URL/password, model token, raw prompt, private URL,
  Docker command·환경 변수·host path를 넣지 않는다.
- 지원 요청에는 redaction된 시간대, service 이름, node UUID, deployment/task ID, failure code,
  package/build version, `journalctl`의 필요한 오류 요약만 포함한다.
- 로그 보존은 PostgreSQL backup을 대체하지 않는다. DB 복구와 credential 복구는
  [PostgreSQL 백업·복구·재해 복구](disaster-recovery.md)를 따른다.

네트워크 경계는 [네트워크·방화벽 운영 절차](networking.md), Agent 설정·회전은
[Agent 설정과 credential 회전 운영 절차](agent-operations.md), HTTP 오류 처리와 인증은
[관리자·Agent API 계약](api-contract.md)을 따른다.
