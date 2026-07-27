"""``EvalRunner`` — the Ray actor that executes one evaluation run.

The runner drives OpenRAG through its own HTTP API rather than through
in-process calls, for two reasons: it is the path a real user's documents take
(so the indexing timings mean something), and it is the same surface promptfoo
itself talks to, so an eval can never pass against a code path the API does not
expose.

Everything it needs is handed to it at dispatch time — partition, bearer token,
corpus directory, parsed test cases. It owns only the mechanical work: upload
and time each file, shell out to promptfoo twice, fold the outputs into
metrics, persist, and drop the throwaway partition on the way out.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import ray

#: Terminal states of an indexing task — a protocol contract with
#: ``services.workers.task_state``, not a setting.
_TERMINAL_TASK_STATES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})

#: Only the tail of promptfoo's stderr is kept for the failure message.
_ERROR_TAIL_CHARS = 2000


class EvalRunError(RuntimeError):
    """A run failed in a way worth surfacing verbatim to the admin."""


@ray.remote
class EvalRunner:
    """Serialises evaluation runs — one at a time, by construction."""

    def __init__(self) -> None:
        from core.config import load_config
        from core.utils.logging import get_logger
        from services.persistence.connection import ConnectionManager
        from services.persistence.evaluation_repo import PgEvaluationRepository

        self._logger = get_logger()
        self._config = load_config()
        self._settings = self._config.evaluation
        self._connection = ConnectionManager(self._config.resolved_rdb())
        self._connection_ready = False
        self._repo = PgEvaluationRepository(lambda: self._connection.pool)
        self._cancelled = False
        self._active_run_id: str | None = None
        self._process: asyncio.subprocess.Process | None = None

    # ── lifecycle ────────────────────────────────────────────────────

    async def _ensure_connection(self) -> None:
        if not self._connection_ready:
            await self._connection.initialize()
            self._connection_ready = True

    def _api_client(self, api_base_url: str, token: str) -> Any:
        import httpx

        return httpx.AsyncClient(
            base_url=api_base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=self._settings.http_timeout_seconds,
            follow_redirects=True,
        )

    async def is_busy(self) -> bool:
        """Liveness probe; also what ``EvaluationService`` pings before dispatch."""
        return self._active_run_id is not None

    async def cancel(self, run_id: str) -> bool:
        """Ask the in-flight run to stop at its next checkpoint."""
        if self._active_run_id != run_id:
            return False
        self._cancelled = True
        if self._process is not None and self._process.returncode is None:
            self._process.kill()
        return True

    def _check_cancelled(self) -> None:
        if self._cancelled:
            raise asyncio.CancelledError

    # ── the run ──────────────────────────────────────────────────────

    async def run(
        self,
        *,
        run_id: str,
        partition: str,
        token: str,
        api_base_url: str,
        corpus_dir: str,
        cases: list[dict[str, Any]],
    ) -> None:
        """Execute a full run, persisting its outcome.

        Never raises: every failure is recorded on the run row, because the
        caller dispatched this fire-and-forget and has nobody to catch for.
        """
        from core.models.evaluation import EvalRun, EvalRunStatus, EvalTestCase

        await self._ensure_connection()
        self._cancelled = False
        self._active_run_id = run_id
        log = self._logger.bind(run_id=run_id, partition=partition)

        test_cases = [
            EvalTestCase(
                query=case["query"],
                expected_answer=case["expected_answer"],
                expected_file_ids=tuple(case.get("expected_file_ids") or ()),
            )
            for case in cases
        ]
        run = EvalRun(id=run_id, dataset_id="", status=EvalRunStatus.QUEUED)

        try:
            async with self._api_client(api_base_url, token) as client:
                await self._repo.update_run_status(run_id, EvalRunStatus.INDEXING)
                run.indexing = await self._index_corpus(client, partition, Path(corpus_dir))
                log.info(
                    f"Indexed {run.indexing.files_total} file(s) in "
                    f"{run.indexing.wall_seconds}s ({run.indexing.files_per_minute}/min)"
                )

                self._check_cancelled()
                await self._repo.update_run_status(run_id, EvalRunStatus.EVALUATING)
                retrieval_payload, answer_payload = await self._run_promptfoo(
                    cases=test_cases,
                    partition=partition,
                    token=token,
                    api_base_url=api_base_url,
                )

            from core.evaluation import summarize

            run.retrieval, run.answer, run.cases = summarize(
                cases=test_cases,
                retrieval_payload=retrieval_payload,
                answer_payload=answer_payload,
            )
            run.status = EvalRunStatus.COMPLETED
            await self._repo.save_run_results(run)
            log.info("Evaluation run completed")

        except asyncio.CancelledError:
            run.status = EvalRunStatus.CANCELLED
            run.error = "Run cancelled."
            await self._repo.save_run_results(run)
            log.info("Evaluation run cancelled")
        except Exception as exc:  # noqa: BLE001 — recorded, not swallowed
            run.status = EvalRunStatus.FAILED
            run.error = str(exc)[:_ERROR_TAIL_CHARS]
            await self._repo.save_run_results(run)
            log.exception(f"Evaluation run failed: {exc}")
        finally:
            self._active_run_id = None
            self._process = None
            await self._drop_partition(api_base_url, token, partition)

    # ── indexing phase ───────────────────────────────────────────────

    async def _index_corpus(self, client: Any, partition: str, corpus_dir: Path) -> Any:
        """Upload every corpus file, timing each one end to end."""
        from core.evaluation import indexing_metrics, sanitize_file_id
        from core.models.evaluation import FileIndexingSample

        files = sorted(path for path in corpus_dir.iterdir() if path.is_file())
        if not files:
            raise EvalRunError("Dataset corpus is empty — nothing to index.")

        samples: list[FileIndexingSample] = []
        started = time.perf_counter()

        for path in files:
            self._check_cancelled()
            file_started = time.perf_counter()
            failed = False
            try:
                await self._index_one(client, partition, sanitize_file_id(path.name), path)
            except Exception as exc:  # noqa: BLE001 — one bad file must not void the run
                failed = True
                self._logger.warning(f"Eval corpus file '{path.name}' failed to index: {exc}")
            samples.append(
                FileIndexingSample(
                    filename=path.name,
                    size_bytes=path.stat().st_size,
                    duration_seconds=round(time.perf_counter() - file_started, 3),
                    failed=failed,
                )
            )

        metrics = indexing_metrics(samples, time.perf_counter() - started)
        if metrics.files_failed == metrics.files_total:
            raise EvalRunError("Every corpus file failed to index — check the indexer logs.")
        return metrics

    async def _index_one(self, client: Any, partition: str, file_id: str, path: Path) -> None:
        """Upload one file and wait for its indexing task to settle."""
        with path.open("rb") as handle:
            response = await client.post(
                f"/indexer/partition/{partition}/file/{quote(file_id, safe='')}",
                files={"file": (path.name, handle)},
            )
        if response.status_code >= 400:
            raise EvalRunError(f"Upload of '{path.name}' failed: {response.status_code} {response.text[:200]}")

        status_url = response.json().get("task_status_url")
        if not status_url:
            raise EvalRunError(f"Upload of '{path.name}' returned no task URL.")
        await self._await_task(client, status_url, path.name)

    async def _await_task(self, client: Any, status_url: str, label: str) -> None:
        deadline = time.monotonic() + self._settings.task_timeout_seconds
        while True:
            self._check_cancelled()
            response = await client.get(status_url)
            state = response.json().get("task_state") if response.status_code < 400 else None
            if state in _TERMINAL_TASK_STATES:
                if state != "COMPLETED":
                    raise EvalRunError(f"Indexing of '{label}' ended as {state}.")
                return
            if time.monotonic() > deadline:
                raise EvalRunError(f"Indexing of '{label}' timed out.")
            await asyncio.sleep(self._settings.task_poll_seconds)

    # ── promptfoo phase ──────────────────────────────────────────────

    async def _run_promptfoo(
        self,
        *,
        cases: list[Any],
        partition: str,
        token: str,
        api_base_url: str,
    ) -> tuple[Any, Any]:
        """Render both configs, run them, and return the parsed outputs."""
        import yaml
        from core.evaluation import build_answer_config, build_retrieval_config

        grader = self._config.llm
        shared = {
            "cases": cases,
            "api_base_url": api_base_url,
            "partition": partition,
            "token": token,
            "grader_model": grader.model,
            "grader_base_url": grader.base_url,
            "grader_api_key": grader.api_key,
        }
        configs = {
            "retrieval": build_retrieval_config(**shared, top_k=self._settings.top_k),
            "answer": build_answer_config(**shared),
        }

        outputs: dict[str, Any] = {}
        with tempfile.TemporaryDirectory(prefix="openrag-eval-") as workdir:
            root = Path(workdir)
            # promptfoo keeps a SQLite eval history under its config dir,
            # defaulting to $HOME/.promptfoo. That path is not reliably writable
            # by the container's non-root user, and the failure is silent: the
            # migration fails and the CLI exits 1 with no output on either
            # stream. A per-run directory sidesteps that and any contention
            # between deployments sharing a home.
            config_dir = root / "promptfoo-home"
            config_dir.mkdir()
            for name, config in configs.items():
                self._check_cancelled()
                config_path = root / f"{name}.yaml"
                output_path = root / f"{name}-results.json"
                config_path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
                await self._exec_promptfoo(config_path, output_path, config_dir)
                outputs[name] = json.loads(output_path.read_text(encoding="utf-8"))

        return outputs["retrieval"], outputs["answer"]

    async def _exec_promptfoo(self, config_path: Path, output_path: Path, config_dir: Path) -> None:
        binary = self._settings.promptfoo_bin
        env = {
            **os.environ,
            "PROMPTFOO_DISABLE_TELEMETRY": "1",
            "PROMPTFOO_DISABLE_UPDATE": "1",
            # Results are already persisted on the run row; the local eval
            # history database would just grow inside the container.
            "PROMPTFOO_DISABLE_SHARING": "1",
            "PROMPTFOO_CONFIG_DIR": str(config_dir),
            # WAL mode fails on some container filesystems; disabling it keeps
            # that warning out of the failure output.
            "PROMPTFOO_DISABLE_WAL_MODE": "true",
        }
        try:
            self._process = await asyncio.create_subprocess_exec(
                binary,
                "eval",
                "--config",
                str(config_path),
                "--output",
                str(output_path),
                "--no-progress-bar",
                "--no-cache",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError as exc:
            raise EvalRunError(
                f"promptfoo executable '{binary}' not found. It ships in the Ray image; "
                "set PROMPTFOO_BIN if it lives elsewhere."
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                self._process.communicate(), timeout=self._settings.promptfoo_timeout_seconds
            )
        except TimeoutError as exc:
            self._process.kill()
            raise EvalRunError("promptfoo timed out.") from exc

        returncode = self._process.returncode
        self._process = None
        self._check_cancelled()

        # promptfoo exits non-zero when assertions fail, which is a result, not
        # an error — the output file is what decides.
        if not output_path.exists():
            # Both streams: promptfoo reports config errors on stdout, and a
            # failure to open its database produces nothing on either.
            detail = "\n".join(
                part
                for part in (
                    (stdout or b"").decode("utf-8", "replace").strip(),
                    (stderr or b"").decode("utf-8", "replace").strip(),
                )
                if part
            )[-_ERROR_TAIL_CHARS:]
            raise EvalRunError(f"promptfoo produced no output (exit {returncode}): {detail or '(no output)'}")

    # ── teardown ─────────────────────────────────────────────────────

    async def _drop_partition(self, api_base_url: str, token: str, partition: str) -> None:
        """Delete the throwaway partition, logging rather than raising."""
        try:
            async with self._api_client(api_base_url, token) as client:
                response = await client.delete(f"/partition/{partition}")
                if response.status_code >= 400:
                    self._logger.warning(f"Could not drop eval partition '{partition}': {response.status_code}")
        except Exception as exc:  # noqa: BLE001 — teardown must not mask the run's outcome
            self._logger.warning(f"Could not drop eval partition '{partition}': {exc}")


def build_eval_runner(namespace: str = "openrag") -> Any:
    """Get-or-create the detached, single-instance runner actor."""
    return EvalRunner.options(  # type: ignore[attr-defined]
        name="EvalRunner",
        namespace=namespace,
        get_if_exists=True,
        lifetime="detached",
        max_concurrency=4,  # run() holds a slot; cancel()/is_busy() must still land
    ).remote()


__all__ = ["EvalRunError", "EvalRunner", "build_eval_runner"]
