# 중앙 제어면 운영 절차

## 중앙 서버

중앙 호스트에는 중앙 제어면 추가 의존성을 설치합니다. APT 패키지는 이동 가능한 노드 CLI/에이전트용이며 서버 의존성이나 서버 systemd unit을 설치하지 않습니다.

```bash
python3 -m pip install -e '.[server]'
```

secret은 저장소 밖에 둡니다.

```dotenv
DURE_DATABASE_URL=postgresql+psycopg://dure:password@127.0.0.1/dure
DURE_ADMIN_TOKEN=<random-secret>
```

새 버전 시작 전 migration을 적용합니다.

```bash
set -a
source /etc/dure/server.env
set +a
dure-server --migrate
systemctl restart dure-server
```

패키지의 개발/LAN service는 `0.0.0.0:8081`에서 listen합니다. 운영 환경에서는 application을 loopback에 bind하고 TLS reverse proxy를 통해 HTTPS 443만 노출해야 합니다. PostgreSQL과 Ray 포트는 공개하지 않습니다.

```bash
curl -fsS http://127.0.0.1:8081/health
```

## 노드 등록과 승인

```bash
sudo apt install dure
sudo dure join
```

join은 profile을 수집하고 root 전용 `/etc/dure/agent.json`을 쓰며 `dure-agent`를 활성화합니다. 결과 node UUID는 pending 상태입니다. 중앙에서 확인·승인한 뒤 필요하면 profile을 갱신합니다.

```bash
dure admin nodes --pending
dure admin node show <node-id>
dure admin node approve <node-id>
dure admin probe --nodes <node-id>
```

hostname, GPU inventory, network 주소, 운영자 소유권을 검토한 뒤에만 승인합니다. pending 노드는 heartbeat는 가능하지만 task 생성과 claim 양쪽에서 거부됩니다.

## GPU 노드 unjoin

현재 GPU 노드 한 대에서 직접 Dure를 해제합니다.

```bash
sudo dure unjoin
```

이 명령은 state에 기록된 deployment ID와 정확히 일치하는 Dure 컨테이너를 중지하고,
`dure-agent`를 비활성화한 뒤 중앙 credential과 로컬 credential을 폐기합니다. 모델 cache,
NVIDIA driver, Dure label이 없는 컨테이너는 삭제하지 않습니다.

중앙 관리자는 단일 GPU 또는 승인된 전체 GPU pool을 비동기로 해제할 수 있습니다.

```bash
dure admin unjoin --node <node-id>
dure admin unjoin --all
dure admin tasks --watch
```

`--all`은 저장된 profile에 GPU가 있는 승인 노드만 선택하며 CPU utility 노드는 제외합니다.
오프라인 노드의 작업은 queued 상태로 남아 해당 agent가 다시 연결되면 수행됩니다. 각 노드는
로컬 정리에 성공한 뒤에만 `UNJOINED`로 전환되고 credential이 폐기됩니다. 실패한 노드는
승인과 credential을 유지하므로 원인을 해결한 뒤 재시도할 수 있습니다.

단일 worker unjoin은 실행 중인 pipeline을 자동으로 n-1로 재구성하지 않습니다. 남은 GPU로
replacement deployment를 생성하고 검증해야 합니다. 다시 참가하는 노드는 같은 node UUID로
pending 등록되며 중앙 승인을 다시 받아야 합니다.

## Codex 기반 용량 진단

Codex는 관리자 컴퓨터에만 설치·로그인합니다.

```bash
codex --version
codex login status
```

에이전트를 먼저 갱신·재시작해 `PROBE` 결과에 설치 모델과 LLM 작업 부하가 포함되게 한 뒤 진단합니다.

```bash
dure admin diagnose
dure admin diagnose --nodes <node-a> <node-b> --output diagnosis.json
```

기본값은 모든 승인된 온라인 노드에 `PROBE` 작업을 보내고 최대 180초 대기한 뒤, 인벤토리를 로컬 Codex에 전달하는 것입니다. `--no-refresh`, `--timeout`, `--codex-timeout`, `--model`, `--json`으로 동작을 조절할 수 있습니다.

이 보고서는 참고용입니다. 배포 구성을 만들거나 적용하지 않습니다.

- 오프라인 또는 오래된 프로필은 즉시 배포 가능하다고 취급하지 않습니다.
- 다중 노드 Ray 추천은 RTT/대역폭, 방화벽, NCCL 검증 뒤에만 적용합니다.
- 불완전한 모델 디렉터리는 재사용 가능한 아티팩트로 취급하지 않습니다.
- Dure 이외의 LLM 컨테이너는 이름·이미지·상태만 관찰하며 자동 중지하지 않습니다.
- CPU 전용 노드는 utility 역할만 추천합니다.

인벤토리에는 하드웨어, 네트워크 주소, 런타임, 모델 경로·이름, 컨테이너 이미지·상태 메타데이터가 포함될 수 있습니다. 관리자·노드 전달자 자격 증명, 컨테이너 환경 변수·명령, 모델 토큰, 프롬프트 데이터는 제외합니다.

신뢰할 수 없거나 분실된 노드는 credential을 폐기합니다.

```bash
dure admin credential revoke <node-id>
```

credential rotate는 새 secret을 반환하므로 해당 노드의 Agent 설정을 즉시 갱신해야 합니다.

## 현재 배포 구성 운영

다이제스트로 고정한 배포 구성을 만들고 노드별 작업을 보냅니다.

```bash
dure admin deployment create \
  --profile node-a.json --profile node-b.json --profile node-c.json \
  --model qwen2.5-72b-awq \
  --image registry.example/vllm@sha256:<digest> \
  --accept-model-download --pull

dure admin apply <deployment-id> --nodes <node-a> <node-b> <node-c>
dure admin tasks --watch
```

새 GPU를 추가할 때는 승인된 전체 pool을 다시 조사하고 replacement deployment를 생성합니다.

```bash
sudo dure join
dure admin node approve <new-node-id>

dure admin deployment create \
  --all-online --refresh \
  --model qwen2.5-72b-awq \
  --image registry.example/vllm@sha256:<digest>
```

출력된 plan의 stage 수와 assignment를 확인한 뒤 새 deployment에 `apply`, `start`, `verify`를
수행합니다. 실패하면 기존 deployment를 계속 사용합니다. 설치나 승인만으로 실행 중인
컨테이너를 자동 교체하지는 않습니다.

현재 vLLM API는 Ray head에서만 listen합니다. worker와 head 검증을 분리합니다.

```bash
# 모든 배정 노드의 GPU/Ray 검증
dure admin verify <deployment-id> --nodes <node-a> <node-b> <node-c>

# Ray head에서만 HTTP API 검증
dure admin verify <deployment-id> --nodes <ray-head-node-id> --api
```

`start`, `stop`, `restart`는 동일한 deployment ID와 명시적 node 목록을 요구합니다. bulk 요청은 노드마다 독립 task를 만들므로 부분 실패를 확인해야 하며 all-or-nothing으로 가정해서는 안 됩니다.

## 계획된 모델 추천과 단계적 전환

정책 기반 `recommend`, 모델 레지스트리, 세대별 단계적 전환은 아직 구현되지 않았습니다. 구현된 뒤의 운영 원칙은 다음과 같습니다.

1. 최신 `PROBE`로 인벤토리를 갱신합니다.
2. 추천의 후보, 탈락 사유, 모델 리비전, 이미지 다이제스트, 네트워크 사전 조건을 검토합니다.
3. 운영자가 후보를 승인해 배포 세대를 만듭니다.
4. 명시적 apply와 verify를 거쳐서만 활성화합니다.
5. 실패 시 이전에 검증된 세대로 복구합니다.

자동 추천은 자동 다운로드, 이미지 내려받기, 적용, 기존 컨테이너 중지를 의미하지 않습니다. 동일 GPU를 공유하는 파이프라인은 블루/그린 방식이 불가능할 수 있으므로, 실제 무중단 여부를 과장하지 않고 재생성과 복구 절차를 문서화해야 합니다.

## 업그레이드와 복구

controller에서는 PostgreSQL을 백업하고, 패키지를 업그레이드하고, migration 뒤 server를 재시작합니다. Agent는 작은 batch로 업그레이드합니다.

```bash
sudo apt update
sudo apt install --only-upgrade dure
sudo systemctl daemon-reload
sudo systemctl restart dure-agent
```

Agent는 재시작 뒤에도 credential과 완료 task journal을 재사용합니다. 만료된 task lease는 재전달될 수 있으므로 handler는 멱등적이어야 합니다. 활성 deployment 중에는 `/var/lib/dure/agent-tasks.json`을 삭제하지 않습니다.

```bash
systemctl status dure-server dure-agent
journalctl -u dure-server -u dure-agent --since -1h
dure admin nodes --json
dure admin tasks
```
