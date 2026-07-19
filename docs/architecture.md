# Dure Architecture

## System overview

Dure has three cooperating surfaces:

```text
Operator CLI ──HTTPS──> Control Plane ──PostgreSQL
                         ▲
                         │ outbound heartbeat/task polling
                         │
                    dure-agent
                         │
                 Docker / Ray / vLLM
```

- The local CLI probes hardware and can plan or apply deployments without central management.
- The control plane stores node profiles, observed and desired state, deployments, tasks,
  credentials, and audit events.
- The root-owned Agent joins a controller, sends heartbeat state, claims approved tasks, and invokes
  only predefined Python operations.
- Ray is an internal implementation detail of a trusted pod, not an enrollment or security layer.

## Node lifecycle

Package installation provides `/etc/dure/dure-client.env`, containing the deployment's controller
address. `sudo dure join` sends an installation ID and `NodeProfile` to `POST /v1/nodes/join`.
The server creates a node-specific credential and records the node as pending.

Pending nodes can authenticate and heartbeat, but task claim returns no work. An operator promotes
the node with `dure admin node approve <node-id>`. Revocation disables the node and revokes its
active credentials.

Local deployment state remains:

```text
DISCOVERED → PROBING → ELIGIBLE → PLANNED → DOWNLOADING
           → STARTING → VERIFYING → READY
                                └→ WAITING_FOR_PEERS
Any blocking failure ────────────→ FAILED
```

The server stores desired task state separately from the Agent's observed lifecycle state.

## Task protocol

Supported task types are `PROBE`, `VERIFY`, `APPLY_DEPLOYMENT`, `START_DEPLOYMENT`,
`STOP_DEPLOYMENT`, and `RESTART_DEPLOYMENT`.

The Agent polls over HTTPS, leases one task for five minutes, and renews the lease while executing.
Completed task IDs and outcomes are retained locally so a retried delivery reports the prior result
instead of repeating a mutation. PostgreSQL row locks serialize claims for a node.

Plans use server-issued node UUIDs. The controller can normalize a legacy hostname assignment only
when it resolves to exactly one approved node. Central images must be pinned by OCI digest.

## Trust boundaries

- The public management boundary is HTTPS; database and Ray ports remain private.
- Admin bearer credentials and node credentials have different authority.
- Tokenless join grants only pending heartbeat access, never execution authority.
- The Agent runs as root because it manages Docker and `/var/lib/dure`; its task language is closed
  to prevent the controller from becoming a general remote shell.
- The operator of a GPU host can observe local workloads. Community nodes are not suitable for
  secrets or sensitive prompts without a stronger confidential-computing boundary.

See [operations.md](operations.md) for deployment procedures and [security.md](security.md) for the
threat model and hardening backlog.
