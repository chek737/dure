# PostgreSQL 백업·복구·재해 복구

이 문서는 Dure Control Plane의 PostgreSQL 상태를 보호하고, 복구 연습과 migration 실패에 대응하는 운영 runbook이다. Dure는 backup을 자동 생성·암호화·외부 저장소로 전송하지 않는다. 운영자는 PostgreSQL 서비스, 비밀 관리 시스템, 보존 정책에 맞춰 이 절차를 자동화하고 복구 연습 결과를 별도로 보관해야 한다.

## 보호 대상과 제외 대상

| 대상 | 보호 이유 | 복구 방법·주의 |
| --- | --- | --- |
| PostgreSQL Control Plane DB | 노드, 승인, 작업, 배포 세대, recommendation, 증적과 audit 상태 | 일관된 DB backup에서 복원하고 migration 상태를 확인 |
| Dure package version·build commit·설정 기준 | DB schema와 실행 코드의 호환성 판단 | 동일 또는 검증된 호환 버전의 package와 release evidence를 보관 |
| `/etc/dure/server.env`의 관리자 비밀 | Controller 접근·DB 연결에 영향 | 일반 DB backup에 넣지 말고 별도 비밀 관리 시스템에서 접근 제어·회전 |
| Agent credential·`/etc/dure/agent.json` | 노드 identity와 작업 권한 | 대량 파일 복원으로 재배포하지 말고 노드별 상태를 확인하고 필요 시 credential 회전·재등록 |
| 모델 캐시·OCI image·STAGE | 용량이 큰 배포 입력 | PostgreSQL backup으로 복원되지 않는다. digest와 manifest를 다시 검증하여 명시적 prepare 수행 |
| APT archive private key | package 서명 권한 | DB와 분리된 조직 보호 저장소에서 관리하며 일반 복구 artifact에 넣지 않음 |

DB에 `READY` 또는 과거 배포 상태가 있다고 해서 모델 캐시·이미지·NCCL 경로가 현재도 사용 가능하다는 뜻은 아니다. 복구 뒤에는 현재 probe, exact cache identity, 준비 증적과 배포 검증 게이트를 다시 통과해야 한다.

## RPO·RTO와 책임

Dure는 RPO·RTO의 기본 수치를 정하지 않는다. 운영자는 서비스 중요도에 따라 아래 값을 문서화하고, backup 보존과 복구 연습 결과로 충족 여부를 확인한다.

| 항목 | 운영자가 정할 값 | 기록 예시 |
| --- | --- | --- |
| RPO | 허용 가능한 마지막 DB backup 시점 | `≤ 4시간`처럼 업무 기준으로 명시 |
| RTO | 장애 선언부터 Controller 복구 확인까지 허용 시간 | `≤ 2시간`처럼 책임자·교대 체계와 함께 명시 |
| backup 주기·보존 | full/증분 방식, 보존 기간, 외부 사본 위치 | 암호화된 off-site 사본과 삭제 검토 일정 |
| 복구 연습 주기 | 격리 환경에서 실제 restore를 하는 간격 | 날짜, 소요 시간, 사용한 artifact SHA-256 |
| 승인자 | backup 복원·credential 회전·Agent 재개 권한 | 운영 책임자와 대리자 |

## 백업 생성

1. PostgreSQL 전용 backup role과 최소 권한을 사용한다. 비밀번호를 명령행, shell history, Dure 로그에 넣지 않는다.
2. PostgreSQL의 일관된 logical backup 또는 운영 DB 서비스가 제공하는 동등한 일관성 snapshot을 사용한다. 파일 시스템을 단순 복사해 live DB를 백업하는 방식은 사용하지 않는다.
3. backup artifact는 전송·보관 모두에서 암호화하고, 접근 가능한 운영자와 복구 환경을 제한한다.
4. 생성 직후 SHA-256, 생성 시각(UTC), DB server/client version, schema migration head, source database identity를 inventory에 기록한다. checksum만으로 암호화·접근 통제를 대체하지 않는다.

예시는 로컬 PostgreSQL과 `dure` database를 가정한다. database 이름·권한·저장 경로는 각 환경의 정책에 맞게 바꾼다.

```bash
sudo -u postgres pg_dump --format=custom \
  --file /secure-backups/dure-$(date -u +%Y%m%dT%H%M%SZ).dump dure
sha256sum /secure-backups/dure-*.dump
```

`/secure-backups`는 예시일 뿐이다. 운영 환경에서는 encrypted backup vault 또는 조직이 승인한 object storage를 사용하고, checksum manifest도 같은 보존·접근 정책으로 관리한다.

## 복원 리허설

복원은 먼저 production Agent가 연결할 수 없는 격리된 PostgreSQL과 격리된 Controller에서 연습한다. production database나 실행 중인 Controller를 대상으로 `pg_restore --clean`을 실행하면 안 된다.

1. backup checksum, 암호화 복호화 권한, source version, 필요한 package/build commit을 확인한다.
2. 격리 DB instance와 빈 target database를 만들고 network path를 production Agent에서 차단한다.
3. 검증된 PostgreSQL 도구로 복원한다.

```bash
createdb dure_restore_drill
pg_restore --clean --if-exists --dbname=dure_restore_drill \
  /secure-backups/dure-<UTC>.dump
```

4. 동일하거나 호환성이 검증된 Dure package로 격리 Controller를 시작하고 migration 상태를 확인한다. migration은 `dure-server --database-url <격리 DB URL> --migrate`처럼 별도 endpoint에만 적용한다.
5. `GET /health`, schema head, node·deployment·task 수의 합리성, 민감 값이 API/로그에 나오지 않는지 확인한다. 실제 Agent task claim이나 deployment apply는 리허설에서 실행하지 않는다.
6. 소요 시간, backup checksum, 복원 package version, migration 결과, 발견한 차이와 후속 조치를 복구 증적으로 기록한다. 목표 RPO/RTO를 만족하지 못하면 production 전제 조건으로 처리한다.

## 실제 장애 복구

1. 장애 범위를 선언하고 Controller의 쓰기 작업과 Agent task claim을 중지한다. 원인 확인 전 DB 행, Alembic revision, task를 수동 삭제하거나 수정하지 않는다.
2. 마지막으로 검증된 backup, checksum, source version과 비밀 관리 시스템의 접근 권한을 확인한다.
3. 복구 대상 DB가 정확히 격리·식별됐는지 두 명 이상이 확인한 뒤 복원한다. 복원 명령은 destructive할 수 있으므로 이름이 비슷한 production/연습 DB를 혼동하지 않는다.
4. Controller를 loopback 또는 신뢰된 관리망에서 먼저 시작하고 health, migration, audit 상태를 확인한다. 외부 reverse proxy와 Agent 연결은 이 확인 뒤에만 재개한다.
5. 관리자 token, enrollment token, node credential의 노출 가능성을 평가한다. 의심되면 이전 값을 되살리기보다 비밀 관리 시스템에서 `DURE_ADMIN_TOKEN`을 회전하고 영향 받은 노드는 revoke 후 재등록한다.
6. 각 Agent의 최신 heartbeat, 승인 상태, inventory, Docker/runtime, 모델 cache·image identity를 재검사한다. 복구 직후 자동 배포·자동 롤백을 허용하지 않는다.
7. 운영자가 명시적으로 승인한 deployment만 prepare와 apply를 다시 수행한다. 이전 DB 상태는 현재 GPU·네트워크·NCCL·artifact 준비를 대신하는 증거가 아니다.

## migration 실패 또는 호환성 불일치

migration 실패 시 Controller를 계속 재시작해 우연히 통과시키거나, 이미 적용된 Alembic revision을 수동으로 되돌리면 안 된다. 다음 순서로 중단한다.

1. Controller와 쓰기 작업을 중지하고 redaction된 로그·실행 version·현재 migration head를 보관한다.
2. 실패한 package와 database backup의 호환성을 격리 환경에서 재현한다.
3. 수동 SQL 수정, revision table 수정, task/deployment/event 삭제는 하지 않는다. 지원되는 upgrade 경로가 없다면 마지막 검증 backup과 검증된 이전 package로 복원한다.
4. 수정된 migration 또는 복구 절차는 격리 DB에서 upgrade·restart·read-only 확인을 통과한 뒤에만 production 변경으로 승인한다.

## credential 복구 원칙

- `/etc/dure/server.env`, enrollment token, node credential, signing key, model token은 DB dump나 운영 보고서에 평문으로 보관하지 않는다.
- 비밀은 별도 secrets manager에서 최소 권한·감사·회전 가능한 형태로 복구한다.
- 관리 token 또는 Controller host가 노출됐을 가능성이 있으면 token을 회전하고, 기존 token으로 생성된 관리 세션을 신뢰하지 않는다.
- Agent credential 노출 가능성이 있으면 해당 노드를 revoke하고 새 enrollment/join 흐름으로 재등록한다. Agent 설정 파일을 다른 host에 복사해 identity를 복제하지 않는다.

관련 운영 순서는 [운영 절차](operations.md), 보안 경계는 [보안 모델](security.md), release-to-package 출처는 [릴리스 권한과 출처 관리](release-governance.md)를 따른다. backup·audit·evidence·cache의 보존·삭제 기준은 [데이터 보존·격리·삭제 정책](data-retention.md), package·Agent·migration rollout은 [버전 호환성과 롤링 업그레이드](compatibility-upgrades.md)를 따른다.
