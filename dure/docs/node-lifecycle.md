# GPU 노드 폐기·교체 운영 절차

이 문서는 GPU/CPU 노드를 격리, 영구 폐기, hardware 교체 또는 재등록할 때의 안전한 순서를 정한다.
Controller는 노드에 inbound SSH로 접속하지 않으며, 로컬 변경은 노드 소유자가 해당 host에서 root로
명시적으로 수행한다.

`dure admin credential revoke`와 `sudo dure unjoin`은 서로 다른 작업이다. 전자는 중앙에서 task
수신 권한을 차단하고, 후자는 해당 노드에서 정확한 Dure label의 container를 정리한 뒤 Agent credential을
제거한다. 한쪽만 수행한 상태를 “완전 폐기”로 기록하면 안 된다.

## 수명 주기와 책임

| 상태 | 목적 | 허용 행동 |
| --- | --- | --- |
| 운영 중 | 승인·online node가 정상 task 처리 | 명시적 배포·준비·검증 |
| 격리 | credential 노출, 비정상 probe, 교체 준비 | 중앙 revoke, 조사, 새 작업 중지 |
| 해제 중 | container·Agent·credential 안전 정리 | `unjoin` 결과와 중앙 상태 대조 |
| 폐기됨 | 더 이상 Dure node가 아님 | 조직의 자산·데이터 폐기 절차 수행 |
| 교체 node | 새 hardware identity | bootstrap·join·승인·qualification을 새로 수행 |

하나의 노드를 폐기해도 Dure는 다중 노드 배포의 빈 자리를 자동으로 채우지 않는다. Fleet reservation,
recommendation, deployment generation, rollback 경로를 운영자가 별도로 확인한다.

## 1. 폐기 전 계획과 중앙 격리

1. 대상 node UUID, hostname, install identity, 현재 deployment·generation·task·cache reference를
   기록한다. hostname만으로 다른 node와 동일하다고 가정하지 않는다.
2. 실행 중 Dure deployment와 leased task를 확인한다. task를 강제로 없애거나 다른 node로 이동시키지
   않는다. 서비스 중단·새 Fleet recommendation·명시적 rollback은 운영 책임자가 결정한다.

   ```bash
   dure admin nodes --online
   dure admin tasks
   dure admin artifact-cache list
   ```

3. credential 노출, host 침해 또는 즉시 격리가 필요하면 중앙에서 먼저 revoke한다.

   ```bash
   dure admin credential revoke <node-id>
   ```

   revoke는 Agent file 삭제, container 중지, model cache 삭제를 수행하지 않는다. 보안 incident에서는
   journal·중앙 audit·evidence를 보존하고 [관측·장애 대응 운영 절차](observability.md)를 따른다.
4. node가 현재 generation 또는 직접 검증된 rollback predecessor에 필요하면, 자동 reservation 해제나
   다른 GPU 대체를 하지 않는다. 새 deployment generation을 준비할지 서비스 중단을 유지할지 명시적으로
   결정한다.

## 2. 로컬 unjoin과 확인

노드 소유자는 중앙 격리·운영 결정 뒤 대상 host에서 수행한다.

```bash
sudo systemctl status dure-agent --no-pager
sudo dure unjoin
sudo systemctl status dure-agent --no-pager
sudo journalctl -u dure-agent -n 100 --no-pager
```

`unjoin`은 exact deployment·generation·node label을 가진 Dure container만 다룬다. label이 누락되거나
다르면 임의 Docker container를 중지·제거하지 않는다. 정상 완료 뒤 Agent는 비활성화되고 local credential은
제거되며 재등록에 쓸 수 있는 안전한 `install_id`만 남을 수 있다.

중앙 운영자는 `unjoin` 결과와 node 상태를 다시 확인한다. 네트워크 단절로 local 결과를 즉시 수집하지
못한 경우, 추측으로 node를 재사용 가능 상태로 바꾸지 않고 원격 조사·자산 회수 절차를 기록한다.

## 3. cache 보존·격리·삭제

model weight, `STAGE`, OCI image와 `/var/lib/dure` 상태는 PostgreSQL backup으로 복원되지 않는다.
`artifact-cache quarantine`은 자동 삭제가 아닌 보존 이동이며, 현재 참조가 있으면 거부될 수 있다.

1. 다른 deployment·rollback·조사에 필요한 exact cache인지 중앙 참조와 node 상태로 확인한다.
2. 손상 또는 철회 의심 cache는 preview를 실행하고 승인 후 정확한 cache 하나만 격리한다.

   ```bash
   dure admin artifact-cache quarantine <cache-id>
   dure admin artifact-cache quarantine <cache-id> --apply
   ```

3. 재사용 가능한 cache는 model license·보존 정책·새 host 보안 등급을 만족할 때만 별도 승인 절차로
   이동한다. raw copy나 Agent JSON 복사는 node identity를 복제할 수 있으므로 사용하지 않는다.
4. host 반납·재배치 전 cache·quarantine·journal·configuration의 보존 또는 삭제 결정을
   [데이터 보존·격리·삭제 정책](data-retention.md)에 기록한다. Dure는 자동 cache 퇴출·재귀 삭제를 하지
   않으므로 임의 `rm -rf`로 정리하지 않는다.

## 4. package 제거와 자산 폐기

완전 폐기가 승인된 경우에만 package 제거를 고려한다. `dure` package 제거는 Docker Engine, NVIDIA
driver, 다른 container workload를 제거하는 명령이 아니며, 대상 host의 package manager 정책을 먼저
확인한다.

```bash
sudo apt purge dure
```

명령 뒤에는 `/etc/dure`, `/var/lib/dure`, systemd unit, Docker container·volume, archive·journal의
잔존 여부를 읽기 전용으로 확인하고, 보존 대상과 삭제 대상을 자산 폐기 기록에 분리한다. 어떤 경로를
삭제할지 확정하기 전에는 광범위한 재귀 삭제 명령을 실행하지 않는다. hardware 반납 또는 보안 incident의
disk 소거는 조직의 암호화 키 폐기·보안 삭제·자산 관리 절차를 사용하며 Dure CLI 범위 밖이다.

## 5. hardware 교체와 재등록

교체 host는 기존 node의 복제본이 아니라 새 node다. 기존 `/etc/dure/agent.json`, credential, task
journal, Docker 설정, GPU UUID를 새 host에 복사하지 않는다.

1. 새 host의 OS, NVIDIA driver, network zone, Docker/NVIDIA runtime을 준비한다. Dure는 NVIDIA
   driver를 설치·업데이트·변경하지 않는다.
2. 아직 등록되지 않고 Agent가 비활성인 host에서 preview를 확인한 뒤 필요할 때만 bootstrap을 적용한다.

   ```bash
   sudo dure bootstrap
   sudo dure bootstrap --apply
   sudo dure doctor
   ```

3. Controller 주소·TLS를 검토하고 `sudo dure join`을 수행한다. 새 node UUID와 credential이 생기며,
   중앙 운영자가 승인·heartbeat를 확인하기 전에는 task를 받지 못한다.
4. 새 GPU·disk·runtime·network evidence를 수집한다. 기존 profile의 `VALIDATED` 또는 기존 node의
   `PASSED` evidence를 새 GPU UUID·node binding에 복사하지 않는다.
5. 필요하면 새 Fleet recommendation을 만들고 운영자가 수락·prepare·apply한다. 기존 deployment에
   node를 묵시적으로 끼워 넣지 않는다.

Agent 설정과 credential 회전은 [Agent 설정과 credential 회전 운영 절차](agent-operations.md), DB·credential
사고 복구는 [PostgreSQL 백업·복구·재해 복구](disaster-recovery.md)를 따른다.
