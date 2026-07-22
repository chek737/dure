# 데이터 보존·격리·삭제 정책

이 문서는 Dure Control Plane, Agent, 모델 artifact와 운영 증적의 보존·격리·삭제 책임을 정의한다.
Dure는 database·journal·model cache·quarantine을 정해진 기간 뒤 자동 삭제하지 않는다. 실제 저장소
수명 주기·object storage deletion·systemd journal 정리는 운영자가 별도로 구성하고 검증해야 한다.

## 기본 원칙

- 감사·복구·incident 조사에 필요한 기록은 보존 기간 전 임의 삭제하지 않는다.
- credential, token, private URL, raw prompt는 evidence·backup manifest·ticket·운영 보고서에 기록하지
  않으며 별도 secrets manager의 접근·회전 정책을 따른다.
- 중앙 DB 행, Alembic revision, task·deployment·cache event를 직접 SQL로 삭제·수정해 보존 정책을
  구현하지 않는다. schema 호환성과 감사 일관성이 깨질 수 있다.
- `artifact-cache quarantine`은 삭제가 아닌 보존 이동이다. `QUARANTINED` cache와 investigation
  evidence는 자동 복원·자동 삭제하지 않는다.
- legal hold, 보안 incident, license 조사, active deployment 또는 직접 rollback 참조가 있으면 아래
  보존 기간보다 우선해 삭제를 중지한다.

## 보존 기준

아래는 공개 운영 전 최소한 문서화해야 하는 **권장 기준**이다. 조직의 계약·법률·model license가 더
긴 기간을 요구하면 더 긴 기간을 적용한다. production 시작 전에는 책임자, 저장 위치, 암호화, 접근 권한,
삭제 방법, restore drill 날짜를 각 행에 채운 운영 기록을 승인해야 한다.

| 데이터 | 권장 최소 보존 | 권위 있는 위치 | 삭제·복구 주의 |
| --- | --- | --- | --- |
| PostgreSQL 일관성 backup | 일별 35일, 월별 12개월 | 암호화된 승인 backup vault | checksum·restore drill 없이 삭제하지 않음 |
| DB audit, node·task·deployment·recommendation·cache event | 최소 12개월 또는 지원 릴리스 종료 후 1년 중 긴 기간 | PostgreSQL과 검증 backup | raw SQL purge 금지, schema-aware 절차 필요 |
| 릴리스 evidence와 provenance | package/model 지원 기간 + 1년 | version별 evidence와 release record | `FAILED`·`NOT_RUN`도 보존 |
| Controller·Agent journal | 최소 90일, incident면 조사 종료까지 | 조직이 관리하는 log 저장소 | redaction·접근 통제를 먼저 확인 |
| Agent task journal | active lease·복구 검토 종료까지, 최소 90일 권장 | node 보호 상태 경로 | 중앙 감사의 대체물이 아니며 수동 삭제 금지 |
| model cache·OCI image·STAGE | active generation·직접 rollback 참조·license 조건 종료까지 | root 보호 host storage | Dure는 자동 퇴출하지 않음 |
| quarantined cache | 조사·법무·보안 승인 종료까지 | `.dure-quarantine` 등 보존 위치 | 자동 복원·자동 삭제 금지 |

수치는 Dure 코드가 강제하지 않는다. 비용·용량만을 이유로 기간을 줄이려면 release 책임자와 보안·법무
책임자가 RPO/RTO, rollback 가능성, license 의무를 기록하고 승인한다.

## 분류별 운영 절차

### PostgreSQL과 backup

[PostgreSQL 백업·복구·재해 복구](disaster-recovery.md)의 일관된 backup·암호화·restore drill 절차를
따른다. 만료 처리는 다음 순서로 수행한다.

1. backup inventory에서 생성 시각, checksum, 암호화, migration head, restore drill 결과, legal hold를
   확인한다.
2. 최소 보존 집합과 가장 최근 성공 restore drill artifact가 남는지 확인한다.
3. 승인 backup vault의 versioned deletion 또는 lifecycle policy로 삭제한다. live DB file이나 추측한
   backup 경로를 재귀 삭제하지 않는다.
4. 삭제 artifact의 식별자·시각·승인자·근거만 audit 기록에 남긴다. dump 내용이나 DB credential은
   남기지 않는다.

### 운영 로그와 evidence

로그 수집기는 Authorization header, `/etc/dure/agent.json`, `/etc/dure/server.env`, token, private
origin URL, raw prompt를 수집·색인하지 않도록 설정한다. 의심스러운 노출이 발견되면 보존 기간을 기다리지
말고 log 접근을 격리하고 비밀을 회전한다. redaction 이전 원본을 일반 운영자가 내려받을 수 있게 두지
않는다.

version별 release evidence는 실제 실행 상태를 보존하는 기록이다. `NOT_RUN`을 삭제하고 빈 문서만
남기거나 실패 evidence를 성공 evidence로 교체하지 않는다. 실제 GPU 결과는
[릴리스 증적 기록](release-evidence/README.md)의 형식을 따른다.

### 모델 artifact·cache·격리본

모델 weight에는 license·재배포·국가·접근 제한이 적용될 수 있다. cache 삭제·이동 전에는
[모델 반입·승인 정책](model-onboarding-policy.md)의 revision·license·incident 상태를 확인한다.

- active deployment, 준비 operation, benchmark, Fleet reservation, 직접 검증된 rollback predecessor가
  참조하는 cache는 삭제하지 않는다.
- 손상·철회 의심 cache는 중앙 preview로 참조를 확인하고 명시적 격리 후 조사한다.
- `QUARANTINED` cache 삭제에는 조사 종료, reference 부재, license 검토, 자산 소유자 승인, 삭제 방식과
  결과 검증 기록이 모두 필요하다.
- shared CAS 청크와 node별 final cache의 연결을 이해하지 못한 상태에서 부분 경로를 지우지 않는다.
  Dure는 안전한 자동 garbage collection을 제공하지 않는다.

## 삭제 승인과 실행 기록

삭제는 데이터 소유자 또는 release 책임자와 storage·보안 책임자가 함께 확인한다. incident 또는 legal
hold가 있으면 법무·보안 승인도 필요하다.

```text
대상 종류와 immutable 식별자:
보존 기간·license·legal hold 확인:
현재 배포·rollback·조사 참조 검사 결과:
승인자와 시각(UTC):
storage 삭제 방법과 검증 방법:
결과(성공 / 실패 / 중단):
```

삭제 실패는 성공으로 간주하지 않는다. 대상이 남았으면 접근을 계속 제한하고 storage 상태를 조사한다.
민감 artifact의 실제 경로나 secret을 이 기록에 적지 않는다.

## node 폐기·복구와의 관계

host 반납·hardware 교체는 [GPU 노드 폐기·교체 운영 절차](node-lifecycle.md)의 revoke·unjoin·참조
검사를 먼저 수행한다. DB 복구는 [PostgreSQL 백업·복구·재해 복구](disaster-recovery.md)를 따른다.
DB backup에 node model cache가 포함되지 않으며, 복구된 DB 상태가 현재 artifact·GPU·NCCL 준비 증거가
아니라는 점을 항상 구분한다.
