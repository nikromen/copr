# Run Copr infra with podman kube play

Run the full COPR infrastructure locally using `podman kube play` with Kubernetes
manifests. Designed for development, testing, and as the foundation for
OpenShift deployment.

## Quick Start

```bash
cd deployment/
just up          # Start with @copr/copr-dev packages (latest main)
```

## Prerequisites

- `podman` (with aardvark-dns for inter-pod DNS resolution)
- `just` (command runner — `dnf install just`)

Enable the rootless podman socket (needed for dynamic builder provisioning):

```bash
systemctl --user enable --now podman.socket
```

## Modes

| Mode      | Command                  | RPM Source                              |
| --------- | ------------------------ | --------------------------------------- |
| `dev`     | `just up`                | `@copr/copr-dev` packages (latest main) |
| `release` | `just up-release`        | Stable Fedora RPMs only                 |
| `pr`      | `just up-pr <PR_NUMBER>` | PR packages overlaid on dev             |
| `local`   | `just up-local`          | Dev RPMs + local source mounted         |

### Local Development

```bash
just up-local                    # Start with source mounted at /opt/copr
vim ../frontend/coprs_frontend/  # Edit code
just restart frontend            # Pick up changes
```

### Testing a Pull Request

```bash
just up-pr 3127                  # Start with RPMs from PR #3127
```

## Architecture

Builders are provisioned **dynamically** by resalloc as podman containers
on the shared `copr` network. This means:

- Multiple builds can run concurrently (configurable via `pools.yaml`)
- Each builder is an isolated container
- Builders are created on-demand and destroyed after use
- The approach mirrors production deployment patterns

Manifests use [Kustomize](https://kustomize.io/) with a base + overlay
pattern. The justfile renders the selected overlay via `kustomize build`
and pipes the result to `podman kube play`.

## Commands

Run `just` to see all available commands.

## OpenShift Local (CRC MicroShift)

Run Copr on a local OpenShift cluster using CRC
with the MicroShift preset.

### Setup

```bash
crc setup
crc config set preset microshift
crc config set cpus 12
crc config set memory 24576
crc config set disk-size 80
crc start
```

### Deploy

```bash
just up-openshift-local       # Build images, push to CRC, apply manifests
just status-openshift-local   # Check pod status
just down-openshift-local     # Tear down
```

Images are built locally with `podman`, transferred into the CRC VM via
`podman save | ssh podman load`, and referenced as `localhost/copr-*` with
`imagePullPolicy: Never`. The `overlays/openshift-local/` kustomization
handles image name prefixing, pull policy, security contexts, and resalloc
pool sizing.

### Running tests

TODO: still needs some tweaks... will fill out after https://github.com/fedora-copr/copr/pull/4305

### Useful URLs

| Service | URL |
|---------|-----|
| Frontend | http://copr-frontend-copr.apps.crc.testing |
| Backend results | http://copr-backend-copr.apps.crc.testing |
| Dist-git | http://copr-distgit-copr.apps.crc.testing |
| Resalloc WebUI | http://copr-resalloc-copr.apps.crc.testing/pools |

## OpenShift Prototype (real cluster, e.g. ROSA)

A minimal overlay to get Copr running on a real OpenShift cluster (e.g.
[ROSA](https://www.redhat.com/en/technologies/cloud-computing/openshift/aws))
reachable on a public IP/hostname as fast as possible. This is deliberately
**not** production-hardened -- see
[Known gaps for real production](#known-gaps-for-real-production) below.

### Prerequisites

- An already-provisioned OpenShift cluster and an active `oc login` session
  (cluster and AWS/ROSA provisioning itself is out of scope here)
- A container registry you can push to (e.g. `quay.io/<your-org>`) and are
  logged into with `podman login`
- `COPR_REGISTRY` environment variable set to that registry
  (defaults to `quay.io/copr`)

### Deploy

```bash
export COPR_REGISTRY=quay.io/<your-org>
just up-openshift       # Build+push images, apply manifests, wait for public LB
just status-openshift   # Check pod status and the frontend Service
just down-openshift     # Tear down
```

`up-openshift` builds and pushes all images to `$COPR_REGISTRY`, applies the
`overlays/openshift/` kustomization (which layers on top of
`overlays/openshift-local/`, reusing its SCCs/RBAC/builder provisioning), and
turns the `frontend` Service into a `type: LoadBalancer` -- no Route, no TLS,
no custom domain. Once AWS assigns the load balancer a public hostname, the
recipe points `PUBLIC_COPR_HOSTNAME`/`PUBLIC_COPR_BASE_URL` at it and restarts
the frontend so Flask's `Host` header check matches.

### Known gaps for real production

This prototype intentionally skips everything below. Treat it as a checklist
of decisions/work needed before running real production traffic on it:

- **TLS and a real domain** -- no Route/cert, just plain HTTP on a
  LoadBalancer hostname
- **Secrets management** -- `base/secrets/*.yaml` cleartext dev credentials
  are reused as-is; fine for a throwaway prototype, not for real data
- **Builder security** -- reuses the shared `privileged` SCC bound to
  `copr-anyuid`; a real deployment should use a purpose-built, narrower SCC
- **Storage** -- PVCs use the cluster default storage class and dev-sized
  capacities, not sized/classed for real workloads
- **Resource sizing / HA** -- dev resource requests/limits, single replica
  everywhere, no PodDisruptionBudgets
- **Outgoing email** -- `SEND_EMAILS` stays off; frontend's `mail.py` hardcodes
  `SMTP("localhost")` with no relay configured
- **Backups** -- no backup of the Postgres database or the keygen GPG keys
- **Multi-arch builders** -- only x86_64 is exercised; aarch64 would need a
  Graviton machine pool + nodeSelector, other arches would need a QEMU/binfmt
  story that doesn't exist yet for this container image
- **CI/CD** -- image build+push is manual (`just build-push-production`), no
  automated pipeline
- **Monitoring/alerting** -- nothing beyond whatever the cluster provides by
  default
