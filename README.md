# Dure

**Dure — Pool compute. Share intelligence.**

Dure is an early Linux CLI and node agent for building resource-aware community LLM infrastructure. It inventories a node, classifies useful roles, creates a model deployment plan, prepares model artifacts and containers, joins a Ray cluster, and verifies GPU/Ray/vLLM readiness.

This repository is an executable MVP. It is not yet a hardened public volunteer-computing platform.

## What works

- Ubuntu/Linux host, memory, disk, virtualization and network inventory
- NVIDIA GPU, VRAM, driver and compute-capability detection
- Live GPU memory/utilization and metadata-only host Ray/vLLM process detection
- Docker/NVIDIA runtime and Ray detection
- CPU-only utility-node classification
- Local Qwen2.5 AWQ model recommendations
- Three-node 24GB GPU planning for Qwen2.5-72B-AWQ
- Automatic 80-layer partitioning into `27/27/26`
- Persisted node lifecycle state
- Resumable Hugging Face CLI download staging
- Docker command execution for Ray head/workers and a vLLM API
- Host GPU, container CUDA, Ray resource, HTTP health and served-model checks
- Dure/Hugging Face/Ollama model inventory and Dure or common LLM container discovery
- Sharded-model completeness and actual-read checks, including configured shared model roots
- Deterministic central capacity plans that adapt to GPU join/leave without resizing live pipelines
- Codex-assisted central capacity diagnosis for GPU/Ray placement and CPU utility roles
- Dry-run by default; mutation requires explicit flags

## Install for development

```bash
cd /root/workspace/dure
python3 -m pip install -e '.[test]'
```

The signed APT package contains the dependency-free node CLI and Agent. Install the central
Control Plane from source with `python3 -m pip install -e '.[server]'`; its modern FastAPI and
SQLAlchemy requirements are intentionally not resolved from Ubuntu 22.04 system packages.

## Install from APT

Once the signed repository has been published, users register it once and install Dure:

```bash
curl -fsSL https://chek737.github.io/dure/install.sh | sudo sh
```

APT signing-key fingerprint:
`E1F952F8B23E7A1B884CB5A33EC5C8CAE53AFA01`.

Subsequent installs and upgrades use normal APT commands:

```bash
sudo apt install dure
sudo apt upgrade
```

See [docs/apt-distribution.md](docs/apt-distribution.md) for signing, GitHub Pages publishing, manual repository registration, and release instructions.

Additional documentation:

- [Architecture](docs/architecture.md)
- [Control-plane operations](docs/operations.md)
- [Security model](docs/security.md)
- [Development and release workflow](docs/development.md)
- [Development roadmap](docs/roadmap.md)

Then inspect the local node:

```bash
dure doctor
dure doctor --json
dure doctor --output camp-9.json
```

## Create a plan

Export a profile on each node:

```bash
dure doctor --output camp-7.json
dure doctor --output camp-8.json
dure doctor --output camp-9.json
```

Create a shared deployment plan:

```bash
dure plan \
  --profile camp-7.json \
  --profile camp-8.json \
  --profile camp-9.json \
  --model qwen2.5-72b-awq \
  --image registry.example.com/vllm@sha256:<digest> \
  --network-interface ens3 \
  --output qwen72b-plan.json
```

The generated plan assigns one Ray rank and one pipeline stage to each node. Every node must receive the exact same plan file.

## Initialize a node

Safe dry run:

```bash
dure init --plan qwen72b-plan.json
dure status
```

Apply the plan after reviewing it:

```bash
sudo dure init \
  --plan qwen72b-plan.json \
  --apply \
  --accept-model-download \
  --pull
```

Run `--serve` only on the assigned Ray head after all workers have joined:

```bash
sudo dure init \
  --plan qwen72b-plan.json \
  --apply \
  --serve
```

Verify the deployment:

```bash
dure verify --plan qwen72b-plan.json --api
```

## Safety model

Dure does not install or change an NVIDIA host driver. A mismatched or unavailable driver blocks provisioning and requires administrator action.

The CLI refuses to apply any image not pinned by OCI digest unless `--allow-unpinned-image` is supplied. Production plans should use an immutable OCI digest.

Model downloads require `--accept-model-download`; image pulls require `--pull`; replacement of an existing stopped container requires `--replace`.

Ray ports must be restricted to a trusted LAN or private overlay such as WireGuard. Do not expose the Ray GCS, dashboard, or worker ports to the public Internet.

## Lifecycle

```text
DISCOVERED → PROBING → ELIGIBLE → PLANNED
           → DOWNLOADING → STARTING → VERIFYING → READY
                                      └────────→ WAITING_FOR_PEERS
Any blocking error ────────────────────────────→ FAILED
```

State is stored under `$XDG_STATE_HOME/dure/state.json`, or `~/.local/state/dure/state.json` by default.

## Current limitations

- Docker is the only apply-mode container backend.
- The MVP assigns at most one GPU per physical node.
- Network benchmark and NCCL collective probe are not yet implemented.
- vLLM startup is implemented but needs broader image/version compatibility testing.
- Artifact hashes rely on a pinned Hugging Face revision; a signed model manifest is planned.
- No credit ledger, authentication, WireGuard automation or public-node sandbox yet.
- GPU supply changes produce an advisory replacement-generation plan; automatic traffic switching,
  gang rollback, and unattended reconciliation are not yet implemented.

These boundaries are intentional: the current milestones focus on deterministic node discovery, safe provisioning, readiness, and trusted-node control before adding a public inference gateway.

## Central node management

Dure includes an optional FastAPI/PostgreSQL control plane and an outbound-polling node agent.
Run `dure-server --migrate`, set `DURE_DATABASE_URL` and `DURE_ADMIN_TOKEN`, then start
`dure-server` behind a TLS reverse proxy.

The package carries the deployment's control-plane address in
`/etc/dure/dure-client.env`. A new machine joins without a per-node token or server argument:

```bash
sudo apt install dure
sudo dure join
```

Joining records the machine as pending, stores its credential, and starts `dure-agent`.
The agent may send heartbeats while pending but cannot receive work. Approve it centrally:

```bash
dure admin nodes --pending
dure admin node approve <node-id>
```

Central tasks are restricted to probe, verify, apply, start, stop, and restart operations;
arbitrary remote shell commands are not accepted. Central deployments require an OCI
digest-pinned image. The one-time enrollment-token endpoint remains for compatibility.

On the admin computer, refresh approved online nodes and ask the locally authenticated Codex CLI
for an advisory capacity report:

```bash
codex login status
dure admin diagnose
dure admin diagnose --nodes <node-id> <node-id> --json --output diagnosis.json
```

Build a deterministic plan when you need an auditable answer that does not depend on Codex. The
command preserves occupied or unmanaged GPUs, can reserve spare nodes, and describes the new
generation to stage when GPU supply changes:

```bash
dure admin capacity --refresh --objective balanced
dure admin capacity --objective quality --reserve-gpus 1 --json --output capacity.json
dure admin capacity --watch --refresh --interval 5 --refresh-interval 300
```

The objectives are `quality`, `balanced`, `throughput`, and `reuse-first`. GPU join or loss changes
the next capacity calculation, but Dure does not mutate a running pipeline in place. Operators
stage and verify a new deployment generation before switching traffic.

Create a central deployment directly from registered node UUIDs once the selected profiles are
fresh:

```bash
dure admin deployment create \
  --nodes <node-id> <node-id> <node-id> \
  --model qwen2.5-72b-awq \
  --image registry.example.com/vllm@sha256:<digest>
```

Set extra trusted model roots for the Agent with the colon-separated `DURE_MODEL_ROOTS`
environment variable. Every indexed safetensors shard must exist and pass a real read before the
model is reported complete.

The command sends hardware, network, installed-model, and LLM-container metadata to the configured
Codex provider. It never sends Dure credentials and does not apply the recommendation. Upgrade the
Agents before diagnosis so their `PROBE` results include the new model and workload inventory.

## Tests

```bash
python3 -m unittest discover -v
```
