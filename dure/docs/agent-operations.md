# Agent 설정과 credential 회전 운영 절차

이 문서는 GPU/CPU 노드의 Agent 등록 전 설정, 등록 후 identity 파일, credential 회전과 재등록을
다룬다. Controller가 노드에 inbound SSH로 접속해 설정을 바꾸지 않는다. 노드 소유자 또는 권한을
위임받은 운영자가 해당 노드에서 root로 명시적으로 수행한다.

## 설정 파일의 역할

| 파일 | 사용 시점 | 소유자·권한 | 포함 가능한 값 | 주의 |
| --- | --- | --- | --- | --- |
| `/etc/dure/dure-client.env` | `dure join` 전의 Controller 주소 결정 | root 소유, 일반 사용자 쓰기 금지 | `DURE_SERVER`, `DURE_INSECURE` | 이 파일에는 node credential을 넣지 않음 |
| `/etc/dure/agent.json` | join 후 Agent polling | root 소유 `0600` | `server`, `node_id`, `credential`, `install_id`, `verify_tls`, `state_file` | credential이 있으므로 복사·백업·출력 금지 |
| `/etc/dure/server.env` | Controller service | Controller host에서만 root/서비스 관리자 보호 | DB URL, admin token | Agent host에 배포하지 않음 |

Debian package는 `/etc/dure/dure-client.env`와 `dure-agent.service`를 설치한다. source/editable
설치에서는 package template을 읽을 수 있지만 production에서는 package와 동일한 root 소유 설정·unit을
운영자가 명시적으로 준비해야 한다.

## Controller 주소와 TLS 우선순위

`dure join`은 Controller 주소를 다음 순서로 선택한다.

1. 명령행 `--server`
2. 프로세스 환경의 `DURE_SERVER`
3. `/etc/dure/dure-client.env`의 `DURE_SERVER`

TLS 검증 해제 여부는 다음 순서다.

1. 명령행 `--insecure`
2. 프로세스 환경의 `DURE_INSECURE`
3. `/etc/dure/dure-client.env`의 `DURE_INSECURE` (기본 `false`)

`DURE_INSECURE=true` 또는 `dure join --insecure`는 HTTPS 검증을 끄는 개발 전용 탈출구다.
신뢰된 production Agent에서는 사용하면 안 되며, `DURE_SERVER`는 HTTPS URL이어야 한다. 임시 HTTP
시험을 끝낸 뒤에는 `DURE_INSECURE=false`로 되돌리고 HTTPS Controller에 새로 등록한다.

등록이 완료되면 Agent는 `/etc/dure/agent.json`에 저장된 `server`와 `verify_tls`로 polling한다.
따라서 등록 후 `/etc/dure/dure-client.env`만 바꿔도 기존 Agent의 Controller나 TLS 동작은 바뀌지
않는다. Controller 주소·TLS 경계를 바꿀 때는 기존 node identity를 복사하지 말고 `sudo dure unjoin`
후 올바른 설정으로 `sudo dure join`을 수행한다.

## 등록과 일상 점검

등록 전에는 파일 내용과 권한을 root로만 확인한다. credential이 없는 client 설정은 일반 설정이며,
Agent JSON은 화면·ticket·로그에 붙여넣지 않는다.

```bash
sudo install -d -m 0750 -o root -g root /etc/dure
sudoedit /etc/dure/dure-client.env
sudo chown root:root /etc/dure/dure-client.env
sudo chmod 0644 /etc/dure/dure-client.env

sudo dure join
sudo systemctl status dure-agent --no-pager
sudo journalctl -u dure-agent -n 100 --no-pager
```

`dure join`은 node를 `pending`으로 만들고 Agent service를 enable/start한다. 중앙 운영자는 승인과
heartbeat를 확인한 뒤에만 작업을 배정한다.

```bash
dure admin nodes --pending
dure admin node approve <node-id>
dure admin nodes --online
```

`systemctl` 또는 journal 출력에는 credential을 포함한 JSON 파일이나 HTTP Authorization header를
추가해 진단하지 않는다. Agent가 Controller에 연결하지 못하면 먼저 HTTPS origin, DNS/인증서,
`DURE_INSECURE=false`, outbound 방화벽과 node 승인 상태를 확인한다. 자세한 포트 경계는
[네트워크·방화벽 운영 절차](networking.md)를 따른다.

## node credential 회전

`dure admin credential rotate <node-id>`는 현재 활성 credential을 즉시 revoke하고 새 raw credential을
한 번만 표준 출력에 반환한다. Controller는 node를 승인 상태로 유지하지만, Agent JSON을 갱신하기 전에는
기존 Agent가 heartbeat·task claim에 실패한다. 따라서 서비스 중단이 허용되는 유지보수 창에서 수행한다.

1. 중앙 운영자는 해당 node의 실행 중 deployment·task를 확인한다. 실행 중인 작업을 자동으로 drain,
   이동 또는 재시도하지 않는다.
2. 노드 소유자와 안전한 비밀 전달 경로를 준비한다. 공유 terminal, shell history, chat, ticket, CI log에
   새 credential을 남기면 안 된다.
3. 노드에서 polling을 멈춘다.

   ```bash
   sudo systemctl stop dure-agent
   ```

4. 중앙 운영자가 안전한 terminal에서 회전을 실행한다. 출력 전체는 비밀이므로 저장·전달 직후 화면과
   clipboard 기록을 정리한다.

   ```bash
   dure admin credential rotate <node-id>
   ```

5. 노드 소유자는 `sudoedit /etc/dure/agent.json`으로 **기존 JSON의 `credential` 값만** 새 값으로
   원자적으로 갱신한다. `server`, `node_id`, `install_id`, `verify_tls`, `state_file`을 다른 노드의
   값으로 바꾸거나 파일을 복사하지 않는다. root 소유 `0600`을 확인한다.

   ```bash
   sudo chown root:root /etc/dure/agent.json
   sudo chmod 0600 /etc/dure/agent.json
   sudo systemctl start dure-agent
   sudo systemctl status dure-agent --no-pager
   ```

6. 중앙 운영자는 최신 heartbeat와 task 수신 가능 상태를 확인한다. credential 값을 읽거나 API 요청에
   넣어 확인하지 않는다.

   ```bash
   dure admin nodes --online
   dure admin tasks --watch
   ```

7. 회전 중 실패하거나 새 credential이 노출됐다고 판단되면 새 값을 재사용하지 말고 다시 회전한다.
   node identity 자체가 의심되면 credential 회전 대신 revoke·`unjoin`·새 join 흐름으로 재등록한다.

## revoke·재등록과 복구 경계

`dure admin credential revoke <node-id>`는 node가 이후 작업을 받지 못하게 하는 중앙 격리 수단이다.
이는 로컬 container 정리, Agent 파일 삭제, 새 credential 발급을 자동으로 수행하지 않는다. 반대로
`sudo dure unjoin`은 해당 노드의 정확한 Dure deployment label을 확인해 중지하고 Agent를 비활성화한
뒤 로컬 credential을 제거한다. 둘은 목적과 영향이 다르므로 장애 상황에서 서로를 대체하지 않는다.

Controller 복구 또는 credential 노출 뒤의 순서는 [PostgreSQL 백업·복구·재해 복구](disaster-recovery.md)를,
일상 상태 확인과 알림은 [관측·장애 대응 운영 절차](observability.md)를 따른다.
