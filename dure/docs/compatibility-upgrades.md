# 버전 호환성과 롤링 업그레이드

이 문서는 Controller, Agent, 배포 backend, artifact cache와 PostgreSQL migration을 안전하게
업그레이드·복구하는 운영 기준이다. 지원 범위 자체는 [지원 매트릭스](support-matrix.md)가 기준이며,
이 문서는 버전이 섞인 기간에 무엇을 멈추고 어떤 순서로 확인하는지 설명한다.

Dure는 모든 과거 버전 조합의 양방향 호환성을 보장하지 않는다. 새 기능은 아래 최소 Agent version과
현재 Controller schema를 충족할 때만 시작하며, unknown version·불완전 heartbeat·schema 불일치를
지원되는 성공 경로로 추정하지 않는다.

## 현재 최소 호환 계약

| 기능 또는 계약 | Controller·DB 조건 | 모든 대상 Agent 최소 버전 | 운영 제한 |
| --- | --- | ---: | --- |
| 기존 local plan·legacy backend 검증/rollback 증거 | 대상 Controller release의 migration head | 0.3.12 | 대상 node 전체가 조건을 충족해야 `verified_at`을 rollback 증거로 사용 |
| `VLLM_RAY_PP_V1` 다중 노드 실행 | 현재 runtime contract와 exact node·GPU·rank binding | 0.3.18 | vLLM 0.9.0 V0 Ray, `TP=1`, `PP=2/3`만 허용 |
| `STAGE` 준비·실행·검증 | stage variant·rank manifest·cache identity 지원 schema | 0.3.19 | `FULL_SNAPSHOT`과 자동 대체하지 않고 exact rank cache 필요 |
| 중앙 artifact 준비·cache lifecycle·격리 | artifact preparation/cache lifecycle migration 적용 | 0.3.20 | 대상 node는 해당 버전 이상이어야 함 |
| 현재 Fleet profile·qualification·reservation·runtime | 현재 migration head (`0015_fleet_runtime`) | 대상 기능이 요구하는 위 최소 버전과 현재 heartbeat | legacy·오래된 node를 새 Fleet에 묵시적으로 포함하지 않음 |

Agent version만 같아도 GPU driver, Docker/NVIDIA runtime, OCI image digest, model manifest, network/NCCL
증적이 호환된다는 뜻은 아니다. 배포 전에는 generation의 inventory·exact cache·image·network 게이트를
다시 통과해야 한다.

## 업그레이드 전 중단 기준

다음 상태에서는 production migration·Agent rollout·새 deployment apply를 시작하지 않는다.

- PostgreSQL backup checksum 또는 최근 restore drill이 없거나 backup의 package·schema 호환성을 알 수 없음
- `PREPARED`, `QUEUED`, `RUNNING` operation 또는 leased task의 소유·종료 방식을 확인하지 못함
- Controller health, DB migration head, Agent heartbeat version, node UUID가 불명확함
- 새 version이 요구하는 Docker/runtime·disk·network 조건을 검증하지 못함
- 같은 유지보수 창에 host-wide Docker upgrade, NVIDIA driver 변경, network policy 변경을 함께 수행하려 함

host driver는 Dure 업그레이드 대상이 아니다. Dure는 NVIDIA driver를 자동 설치·업데이트·복구하지 않는다.

## 권장 롤링 순서

1. **변경 동결과 backup**: 새 recommendation·prepare·apply·rollback을 시작하지 않고, Controller DB의
   일관된 backup, checksum, build commit, migration head, online node·task·deployment 목록을 기록한다.
   backup 절차는 [PostgreSQL 백업·복구·재해 복구](disaster-recovery.md)를 따른다.
2. **격리 검증**: 같은 package와 migration을 staging DB에서 먼저 적용하고 health·read-only 조회·기본
   migration smoke를 확인한다. production DB revision table이나 행을 수동 수정하지 않는다.
3. **Controller 우선**: Controller package와 migration을 적용하고 loopback 또는 관리망에서 health와
   schema head를 확인한다. 새 API·task는 대상 Agent 최소 version을 충족하기 전까지 만들지 않는다.
4. **Agent 소규모 batch**: workload가 없는 승인 node부터 한 대씩 package를 올리고 Agent를 재시작한다.
   `/etc/dure/agent.json` credential과 task journal을 복사·삭제하지 않는다.

   ```bash
   sudo apt update
   sudo apt install --only-upgrade dure
   sudo systemctl daemon-reload
   sudo systemctl restart dure-agent
   sudo systemctl status dure-agent --no-pager
   ```

5. **heartbeat와 기능 게이트 확인**: 중앙에서 `agent_version`, online heartbeat, node UUID, runtime
   inventory를 확인한다. 최소 version을 만족하지 않는 node가 하나라도 있으면 그 node를 포함한 새
   다중 노드 generation, `STAGE`, cache lifecycle 작업을 시작하지 않는다.
6. **나머지 batch와 재검증**: 같은 확인을 반복한다. 모든 대상 node가 준비된 뒤 새 recommendation을
   만들고 preview → 명시적 prepare/apply → verify 순서로 격리 검증한다. 과거 `PASSED` evidence를
   새 package·GPU binding에 복사하지 않는다.

업그레이드 중 기존 generation을 자동 교체·재시작·rollback하지 않는다. 서비스 전환은 별도 운영 승인과
[운영 절차](operations.md)의 generation 검증을 요구한다.

## migration 실패와 package 되돌리기

migration 실패는 재시작 반복이나 수동 SQL로 해결하지 않는다.

1. Controller 쓰기 작업과 새 Agent task claim을 중지하고 redaction된 log·package version·migration
   head를 보존한다.
2. 격리 DB에서 동일 backup과 package 조합을 재현한다. 지원되는 downgrade가 있는지 migration과 해당
   release runbook을 확인한다.
3. 새 migration이 만든 audit·cache·qualification·Fleet 데이터가 있거나 downgrade 조건이 확실하지
   않으면 downgrade를 추측하지 않는다. 마지막 검증 backup과 호환 package로 복원하는 경로를 선택한다.
4. Agent package를 내릴 때도 Controller가 그 Agent version을 지원하는지 먼저 확인한다. 새 Agent가
   만든 local journal·cache·credential을 삭제하거나 다른 node에 복사해 downgrade를 시도하지 않는다.
5. 복구 뒤 health, migration head, heartbeat, task lease, exact cache identity를 확인하고 운영자가
   명시적으로 새 deployment 준비를 승인한다.

`VLLM_RAY_PP_V1`과 `STAGE`의 과거 version 경계와 downgrade 금지 조건은
[운영 절차](operations.md)의 “업그레이드와 복구”를 함께 따른다.

## 호환성 표 갱신 규칙

Controller migration head 또는 downgrade 조건, Agent task payload·결과 schema·최소 version, OCI runtime,
vLLM, TP/PP, model/stage contract, package architecture·installer·systemd 설정, GPU·network/NCCL acceptance
조건이 바뀌면 이 문서, 지원 매트릭스, 운영 절차, 릴리스 실행 체크리스트를 같은 PR에서 갱신한다.

변경 뒤에는 unit test·migration smoke·package 검증과 필요한 GPU evidence를 분리해 기록한다. 실제
evidence가 아직 없으면 `NOT_RUN`을 보존하며 지원 claim을 앞당기지 않는다.
