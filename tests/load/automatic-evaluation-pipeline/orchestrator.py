"""Deploy one OpenRAG git version, evaluate it, then tear it down and reclaim disk.

This drives a single, self-contained evaluation run against a *specific* OpenRAG
build, so the same dataset can be scored across versions / configs and compared in
the dashboard. Each run is identified by a UUID, not a version number, so the same
version can be run multiple times with different configs.

Per-run flow (all teardown happens in a ``finally`` so a crashed run still cleans up):

    1. git checkout <version>          (in the separate "versions" clone)
    2. docker compose up --build -d     (isolated per-UUID data dirs + compose project)
    3. poll /health_check               (until the instance is ready)
    4. write reports/<uuid>/run_config.json   (version + config snapshot)
    5. upload_files.py                  (ingest the corpus from THIS eval repo)
    6. benchmark.py                     (writes reports/<uuid>/eval_*.json)
    7. docker compose down + rm -rf runs/<uuid> + prune dangling images/build cache

Storage note: the OpenRAG stack persists everything via **bind mounts**, not Docker
named volumes (see docker-compose.yaml / vdb/milvus.yaml). ``docker compose down -v``
therefore does NOT reclaim it — we point those mounts at a per-UUID directory and
``rm -rf`` it ourselves. The shared HuggingFace / vLLM model-weight caches are left
untouched (re-downloading them is slow and they are not run-specific).

Example:

    python orchestrator.py \
        --version v1.2.0 \
        --versions-repo ../openrag-versions \
        --partition eval_v120 \
        --dataset ./dataset.json \
        --auth-token "$AUTH_TOKEN" \
        --env CHUNKER=semantic_splitter --env RETRIEVER=hybrid
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from config import CONFIG

REPO_DIR = Path(__file__).resolve().parent
REPORTS_DIR = REPO_DIR / "reports"
# Per-run bind-mount data lives here; the whole subtree is rm -rf'd on teardown.
RUNS_DATA_ROOT = REPO_DIR / "runs"


def log(msg: str) -> None:
    """Timestamped line to stdout (the dashboard captures this into the run log)."""
    print(f"[orchestrator {datetime.now():%H:%M:%S}] {msg}", flush=True)


def port_free(port: int) -> bool:
    """True if nothing is currently listening on 127.0.0.1:<port>."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def find_free_ports(n: int) -> list[int]:
    """Return n distinct free TCP ports (held open together so they don't collide)."""
    socks = []
    try:
        for _ in range(n):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("", 0))
            socks.append(s)
        return [s.getsockname()[1] for s in socks]
    finally:
        for s in socks:
            s.close()


def run(cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None, check: bool = True) -> int:
    """Run a subprocess, streaming its output to ours. Returns the exit code."""
    log(f"$ {' '.join(cmd)}  (cwd={cwd})")
    proc = subprocess.run(cmd, cwd=str(cwd), env=env, check=False)
    if check and proc.returncode != 0:
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(cmd)}")
    return proc.returncode


def parse_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser (ignores blanks / # comments / ``export`` prefixes).

    Used only to recover AUTH_TOKEN from a supplied env file so the eval scripts
    authenticate with the same token the deployed container bootstraps with.
    """
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def git_checkout(versions_repo: Path, version: str) -> str:
    """Fetch + force-checkout the requested ref in the versions clone. Returns the SHA.

    Force (-f) discards any working-tree changes from a previous run (e.g. a
    config.yaml we copied in for an override), guaranteeing a clean tree so the
    checkout never fails on "local changes would be overwritten". Safe because the
    versions repo is a throwaway deploy clone, not where you edit code.
    """
    # Best-effort: refresh tags/branches from origin, but don't fail the run if origin
    # is unreachable — an already-present ref can still be checked out offline.
    if run(["git", "fetch", "--all", "--tags", "--prune"], cwd=versions_repo, check=False) != 0:
        log("git fetch failed (continuing with local refs)")
    run(["git", "checkout", "-f", version], cwd=versions_repo)
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(versions_repo), text=True
    ).strip()
    log(f"Checked out {version} -> {sha}")
    return sha


def apply_config_override(versions_repo: Path, config_path: Path) -> None:
    """Place a custom Hydra config into the build context before ``up --build``.

    A file is copied to ``<repo>/conf/config.yaml``; a directory's contents are
    copied into ``<repo>/conf/``. The instance reads config from ``conf/`` (baked
    into the image at build time), so the override takes effect on the rebuild.
    The next run's forced checkout resets it.
    """
    conf_dir = versions_repo / "conf"
    conf_dir.mkdir(parents=True, exist_ok=True)
    if config_path.is_dir():
        shutil.copytree(config_path, conf_dir, dirs_exist_ok=True)
        log(f"Applied config dir {config_path} -> {conf_dir}")
    else:
        shutil.copyfile(config_path, conf_dir / "config.yaml")
        log(f"Applied config {config_path} -> {conf_dir / 'config.yaml'}")


def compose_env(
    base_env: dict[str, str],
    data_root: Path,
    app_port: int,
    overrides: dict[str, str],
) -> dict[str, str]:
    """Build the environment for ``docker compose`` interpolation.

    Sets the bind-mount paths to a per-run directory so the run's data is isolated
    and deletable. Leaves MODEL_WEIGHTS_VOLUME / VLLM_CACHE alone so the (large,
    shared) model caches are reused, not duplicated per run.

    Only the API host port is set here; the per-run ports override (see
    ``write_ports_override``) strips every other published port, so concurrent /
    pre-existing stacks on the host don't collide. The container's internal port
    stays 8080 (APP_iPORT). ``overrides`` are the user's config knobs, last.
    """
    env = dict(base_env)
    env.update(
        {
            "DATA_VOLUME": str(data_root / "data"),
            "DB_VOLUME": str(data_root / "db"),
            "MILVUS_VOLUME_DIRECTORY": str(data_root / "volumes"),
            "APP_PORT": str(app_port),
            "APP_iPORT": "8080",
        }
    )
    env.update(overrides)
    return env


def wait_for_health(base_url: str, timeout: int, auth_token: str | None = None, interval: float = 3.0) -> None:
    """Poll <base_url>/health_check until 200 or timeout. Raises on timeout.

    Sends the AUTH_TOKEN as a bearer — some versions auth-gate /health_check and
    return 403 to unauthenticated probes (a token-less poll would never see 200).
    """
    deadline = time.time() + timeout
    url = f"{base_url}/health_check"
    headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
    log(f"Waiting for {url} (timeout {timeout}s)...")
    last_err = ""
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=5, headers=headers)
            if r.status_code == 200:
                log("Instance is healthy.")
                return
            last_err = f"status {r.status_code}"
        except httpx.RequestError as e:
            last_err = str(e)
        time.sleep(interval)
    raise TimeoutError(f"Instance not healthy after {timeout}s (last: {last_err})")


def force_rmtree(path: Path) -> None:
    """Delete a directory that may contain root-owned files.

    Postgres / Milvus write their bind-mounted data as root (or uid 999) inside the
    container, so a plain rmtree by the host user fails with Permission denied. Delete
    it via a throwaway root container that mounts the parent (the Docker daemon runs
    as root — no sudo needed), falling back to shutil for anything left behind.
    """
    if not path.exists():
        return
    try:
        subprocess.run(
            ["docker", "run", "--rm", "-v", f"{path.parent}:/host", "alpine",
             "rm", "-rf", f"/host/{path.name}"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, OSError) as e:
        log(f"container-based delete failed ({e}); trying direct rmtree")
        shutil.rmtree(path, ignore_errors=True)


def list_compose_services(versions_repo: Path, compose_flags: list[str], env: dict[str, str]) -> list[str]:
    """Return every service defined in the merged compose config for this version."""
    out = subprocess.check_output(
        ["docker", "compose", *compose_flags, "config", "--services"],
        cwd=str(versions_repo),
        env=env,
        text=True,
    )
    return [s for s in out.splitlines() if s.strip()]


def write_ports_override(path: Path, all_services: list[str], app_services: list[str]) -> None:
    """Write a compose override that publishes ONLY the API port, on a free host port.

    Different versions publish different host ports (Milvus, MinIO, etcd, reranker…),
    sometimes hardcoded with no env var to remap — a frequent source of collisions
    with other stacks on the host. The eval run only needs to reach OpenRAG's API
    from the host; everything else talks over the compose network. So we strip every
    service's published ports (``!override []``) and re-publish just ``${APP_PORT}:8080``
    on the API service(s). Version-agnostic: it doesn't matter what the base declares.
    """
    lines = ["services:"]
    for svc in all_services:
        lines.append(f"  {svc}:")
        if svc in app_services:
            lines.append('    ports: !override ["${APP_PORT}:${APP_iPORT:-8080}"]')
        else:
            lines.append("    ports: !override []")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def capture_container_logs(
    versions_repo: Path, compose_flags: list[str], project: str, env: dict[str, str], run_dir: Path
) -> None:
    """Save the deployed stack's logs into the run dir before teardown removes it.

    Without this, a run where every query fails reads as "complete" and then the
    instance (and its logs — the only place the real error lives) is destroyed.
    Captured to reports/<uuid>/container_logs.txt for post-mortem.
    """
    try:
        out = subprocess.run(
            ["docker", "compose", *compose_flags, "-p", project, "logs", "--no-color", "--timestamps"],
            cwd=str(versions_repo),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        (run_dir / "container_logs.txt").write_text((out.stdout or "") + (out.stderr or ""), encoding="utf-8")
        log(f"Captured container logs -> {run_dir / 'container_logs.txt'}")
    except (subprocess.SubprocessError, OSError) as e:
        log(f"Could not capture container logs (continuing): {e}")


def teardown(
    versions_repo: Path,
    compose_flags: list[str],
    project: str,
    env: dict[str, str],
    data_root: Path,
    prune: bool,
) -> None:
    """Stop the stack, delete the run's bind-mount data, prune dangling build artifacts.

    Best-effort: every step is guarded so one failure doesn't block the rest. Never
    touches the shared model caches. ``compose_flags`` is the prebuilt ``-f …`` list
    (base files + the per-run ports override).
    """
    log("=== Teardown ===")
    try:
        run(
            ["docker", "compose", *compose_flags, "-p", project, "down", "--remove-orphans"],
            cwd=versions_repo,
            env=env,
            check=False,
        )
    except Exception as e:  # noqa: BLE001 - teardown must not raise
        log(f"compose down failed (continuing): {e}")

    # Bind mounts aren't removed by `compose down -v`; delete the host dirs ourselves.
    # Postgres/Milvus data is root-owned, so use a container to remove it.
    if data_root.exists():
        force_rmtree(data_root)
        if data_root.exists():
            log(f"Could not fully delete {data_root} (continuing)")
        else:
            log(f"Deleted run data: {data_root}")

    if prune:
        # `up --build` retags linagoraai/openrag:latest each run, orphaning the prior
        # image as dangling layers; the build cache is the bigger hidden cost. Both are
        # safe to prune (base images / model caches are untouched).
        for cmd in (
            ["docker", "image", "prune", "-f"],
            ["docker", "builder", "prune", "-f"],
        ):
            try:
                run(cmd, cwd=versions_repo, check=False)
            except Exception as e:  # noqa: BLE001
                log(f"{' '.join(cmd)} failed (continuing): {e}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deploy one OpenRAG version, evaluate it, and tear it down."
    )
    parser.add_argument("--version", required=True, help="Git tag/branch/commit to deploy")
    parser.add_argument(
        "--versions-repo",
        required=True,
        help="Path to the separate OpenRAG clone used for checkout+deploy",
    )
    parser.add_argument("--partition", default=CONFIG.common.partition, help="Partition to ingest into / query")
    parser.add_argument("--dataset", default=CONFIG.common.dataset_path, help="Golden dataset JSON path")
    parser.add_argument(
        "--corpus-dir",
        default=None,
        help="Directory of documents to ingest (overrides the configured ./pdf_files; e.g. a small subset).",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=None,
        help="Max number of files to ingest from the corpus (default: all available).",
    )
    parser.add_argument(
        "--generate-questions",
        action="store_true",
        help="Self-contained mode: after ingest, generate the dataset from THIS run's "
        "indexed chunks (into reports/<uuid>/dataset.json) and benchmark on it. Chunk IDs "
        "match the instance, so retrieval metrics are non-zero. Ignores --dataset.",
    )
    parser.add_argument(
        "--n-questions-per-cluster",
        type=int,
        default=None,
        help="Answerable questions per cluster when --generate-questions is set (default: config).",
    )
    parser.add_argument(
        "--n-unanswerable-per-cluster",
        type=int,
        default=None,
        help="Unanswerable questions per cluster when --generate-questions is set (default: config).",
    )
    parser.add_argument("--limit", type=int, default=None, help="Limit dataset to N entries (debug)")
    parser.add_argument("--app-port", type=int, default=8080, help="Host port to expose OpenRAG on")
    parser.add_argument(
        "--auth-token",
        default=os.environ.get("AUTH_TOKEN"),
        help="AUTH_TOKEN for the eval scripts. Defaults to AUTH_TOKEN from --deploy-env, then the env var.",
    )
    parser.add_argument(
        "--deploy-env",
        default=None,
        help="Path to a .env file for the deployed instance (sets SHARED_ENV → compose env_file + /ray_mount/.env).",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to a Hydra config.yaml (or a conf dir) to deploy the instance with. Copied into the build context.",
    )
    parser.add_argument(
        "--compose-file",
        action="append",
        default=None,
        help="Compose file (relative to versions repo). Repeatable. Default: docker-compose.yaml",
    )
    parser.add_argument(
        "--service",
        action="append",
        default=None,
        help=(
            "Compose service(s) to build + start (repeatable). Default: openrag, which "
            "pulls its deps (rdb/milvus/vllm) and skips indexer-ui (an uninitialized "
            "submodule with no Dockerfile). Use openrag-cpu for a CPU deploy."
        ),
    )
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Config override applied to the deployed instance (repeatable). Captured in run_config.json.",
    )
    parser.add_argument(
        "--health-timeout", type=int, default=600, help="Seconds to wait for /health_check"
    )
    parser.add_argument(
        "--no-prune",
        action="store_true",
        help="Skip pruning dangling images / build cache on teardown.",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Skip teardown entirely (leave the instance + data up for debugging).",
    )
    args = parser.parse_args()

    versions_repo = Path(args.versions_repo).expanduser().resolve()
    if not (versions_repo / ".git").exists():
        log(f"ERROR: {versions_repo} is not a git repo (need a separate OpenRAG clone).")
        return 2

    # Optional supplied env file for the deployed instance (SHARED_ENV).
    deploy_env: Path | None = None
    deploy_env_vars: dict[str, str] = {}
    if args.deploy_env:
        deploy_env = Path(args.deploy_env).expanduser().resolve()
        if not deploy_env.is_file():
            log(f"ERROR: --deploy-env not found: {deploy_env}")
            return 2
        deploy_env_vars = parse_env_file(deploy_env)

    # Optional supplied Hydra config for the deployed instance.
    config_path: Path | None = None
    if args.config:
        config_path = Path(args.config).expanduser().resolve()
        if not config_path.exists():
            log(f"ERROR: --config not found: {config_path}")
            return 2

    # Token for the eval scripts: explicit flag > AUTH_TOKEN in the supplied env file > env var.
    # (The container itself reads AUTH_TOKEN from the env file; matching them lets
    # upload/benchmark authenticate against the instance.)
    auth_token = args.auth_token or deploy_env_vars.get("AUTH_TOKEN")
    if not auth_token:
        log("ERROR: no AUTH_TOKEN (pass --auth-token, set the env var, or include it in --deploy-env).")
        return 2

    overrides = dict(kv.split("=", 1) for kv in args.env if "=" in kv)
    compose_files = args.compose_file or ["docker-compose.yaml"]
    services = args.service or ["openrag"]

    run_id = uuid.uuid4().hex
    short = run_id[:8]
    project = f"eval-{short}"
    data_root = RUNS_DATA_ROOT / run_id
    run_dir = REPORTS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    data_root.mkdir(parents=True, exist_ok=True)

    # Per-run API host port: prefer the requested one if free, else auto-pick a free
    # one. All other services' host ports are stripped by the ports override below.
    app_port = args.app_port if port_free(args.app_port) else find_free_ports(1)[0]
    base_url = f"http://localhost:{app_port}"
    log(f"=== Run {run_id} | version={args.version} | partition={args.partition} | api_port={app_port} ===")

    # Env for compose interpolation: inherit ours, then per-run mounts + overrides.
    cenv = compose_env(dict(os.environ), data_root, app_port, overrides)
    # Point SHARED_ENV at the supplied env file (absolute) so compose uses it as the
    # service env_file and the /ray_mount/.env bind mount. A leading-slash bind mount
    # (/$SHARED_ENV → //abs/path) collapses to the same absolute path on Linux.
    if deploy_env is not None:
        cenv["SHARED_ENV"] = str(deploy_env)

    compose_flags: list[str] = []
    for f in compose_files:
        compose_flags += ["-f", f]

    exit_code = 0
    try:
        # 1. Checkout the requested build.
        git_commit = git_checkout(versions_repo, args.version)

        # 1b. Lay a custom config into the build context (if supplied) before building.
        if config_path is not None:
            apply_config_override(versions_repo, config_path)

        # 1c. Generate a per-run ports override: strip every service's published host
        # ports and re-publish only the API port on a free host port. Makes deploys
        # collision-proof regardless of what each version's compose declares.
        all_services = list_compose_services(versions_repo, compose_flags, cenv)
        override_path = run_dir / "ports-override.yaml"
        write_ports_override(override_path, all_services, services)
        compose_flags += ["-f", str(override_path)]
        log(f"Publishing only the API port {app_port} (all other host ports stripped)")

        # 2. Deploy it (isolated project + per-run data dirs). Only the named services
        # (+ their depends_on) are built/started, so indexer-ui is never touched.
        run(
            ["docker", "compose", *compose_flags, "-p", project, "up", "--build", "-d", *services],
            cwd=versions_repo,
            env=cenv,
        )

        # 3. Wait until it answers (token-authenticated — some versions gate health_check).
        wait_for_health(base_url, args.health_timeout, auth_token)

        # 4. Snapshot exactly what produced this run, next to its reports.
        run_config = {
            "run_id": run_id,
            "version": args.version,
            "git_commit": git_commit,
            "timestamp": datetime.now(UTC).isoformat(),
            "partition": args.partition,
            "dataset": str(args.dataset),
            "corpus_dir": args.corpus_dir,
            "max_files": args.max_files,
            "generate_questions": args.generate_questions,
            "n_questions_per_cluster": args.n_questions_per_cluster,
            "n_unanswerable_per_cluster": args.n_unanswerable_per_cluster,
            "base_url": base_url,
            "app_port": app_port,
            "compose_project": project,
            "compose_files": compose_files,
            "services": services,
            "env_overrides": overrides,
            "deploy_env": str(deploy_env) if deploy_env else None,
            "config": str(config_path) if config_path else None,
            "data_root": str(data_root),
            "limit": args.limit,
        }
        (run_dir / "run_config.json").write_text(json.dumps(run_config, indent=2), encoding="utf-8")
        log(f"Wrote {run_dir / 'run_config.json'}")

        # Env for the eval scripts: inherit ours, layer the supplied env file (it also
        # carries the judge/model config — MODEL/BASE_URL/API_KEY/JUDGE_* — that
        # benchmark.py needs), then force the target instance + token last.
        seval = dict(os.environ)
        seval.update(deploy_env_vars)
        seval["API_BASE_URL"] = base_url
        seval["AUTH_TOKEN"] = auth_token
        if args.corpus_dir:
            seval["UPLOAD_DIR"] = str(Path(args.corpus_dir).expanduser().resolve())

        # 5. Ingest the corpus (docs come from this eval repo's upload dir).
        log("=== Uploading + indexing corpus ===")
        upload_cmd = [sys.executable, "upload_files.py", "--partition", args.partition]
        if args.max_files is not None:
            upload_cmd += ["--max-files", str(args.max_files)]
        run(upload_cmd, cwd=REPO_DIR, env=seval)

        # 5b. Self-contained mode: generate the dataset from THIS instance's indexed
        # chunks (IDs match → retrieval metrics work). Written next to the report.
        dataset_for_benchmark = str(args.dataset)
        if args.generate_questions:
            log("=== Generating questions from ingested corpus ===")
            gen_dataset = run_dir / "dataset.json"
            gen_cmd = [
                sys.executable, "generate_questions.py",
                "--partition", args.partition,
                "--output", str(gen_dataset),
            ]
            if args.n_questions_per_cluster is not None:
                gen_cmd += ["--n-questions-per-cluster", str(args.n_questions_per_cluster)]
            if args.n_unanswerable_per_cluster is not None:
                gen_cmd += ["--n-unanswerable-per-cluster", str(args.n_unanswerable_per_cluster)]
            run(gen_cmd, cwd=REPO_DIR, env=seval)
            dataset_for_benchmark = str(gen_dataset)

        # 6. Evaluate, writing reports into reports/<run_id>/.
        log("=== Running benchmark ===")
        bench_cmd = [
            sys.executable,
            "benchmark.py",
            "--partition", args.partition,
            "--base-url", base_url,
            "--dataset", dataset_for_benchmark,
            "--output-dir", str(run_dir),
            "--run-id", run_id,
            "--version", args.version,
            "--git-commit", git_commit,
        ]
        if args.limit:
            bench_cmd += ["--limit", str(args.limit)]
        run(bench_cmd, cwd=REPO_DIR, env=seval)

        log(f"=== Run {run_id} complete -> {run_dir} ===")

    except Exception as e:  # noqa: BLE001 - report, then always tear down
        exit_code = 1
        log(f"ERROR: run failed: {e}")
        # Persist the failure alongside the run so the dashboard/user can see it.
        try:
            (run_dir / "error.txt").write_text(str(e), encoding="utf-8")
        except OSError:
            pass
    finally:
        # Capture logs while the stack still exists — the only place a per-query
        # failure's real error lives (otherwise teardown destroys it).
        capture_container_logs(versions_repo, compose_flags, project, cenv, run_dir)
        if args.keep:
            log("--keep set: leaving instance and data in place (no teardown).")
        else:
            teardown(versions_repo, compose_flags, project, cenv, data_root, prune=not args.no_prune)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
