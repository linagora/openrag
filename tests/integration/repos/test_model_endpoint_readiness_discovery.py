"""Integration coverage for database-authoritative readiness discovery."""

from __future__ import annotations

from uuid import uuid4

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


async def test_discovery_follows_only_defaults_and_references_used_by_partitions(postgres_store):
    from services.persistence.model_endpoint_repo import PgModelEndpointRepository

    pool = postgres_store.pool
    run_id = uuid4()
    suffix = run_id.hex
    partition_id = -(run_id.int % 2_000_000_000 + 1)
    names = {
        label: f"s3-{label}-{suffix}"
        for label in (
            "embedder",
            "chat",
            "retrieval",
            "reranker",
            "vlm",
            "stt",
            "context",
            "metadata",
            "topic",
            "unused",
            "missing",
            "indexation-preset",
            "retrieval-preset",
            "unused-preset",
            "broken-endpoint-preset",
            "deleted-preset",
            "main-partition",
            "missing-endpoint-partition",
            "missing-preset-partition",
        )
    }
    endpoint_rows = [
        (names["embedder"], "embedder", True),
        (names["chat"], "llm", False),
        (names["retrieval"], "llm", False),
        (names["reranker"], "reranker", False),
        (names["vlm"], "vlm", False),
        (names["stt"], "stt", False),
        (names["context"], "llm", False),
        (names["metadata"], "llm", False),
        (names["topic"], "llm", False),
        (names["unused"], "llm", False),
    ]
    preset_rows = [
        (
            names["indexation-preset"],
            "indexation",
            {
                "vlm": names["vlm"],
                "stt": f"  {names['stt']}  ",
                "contextualization_llm": names["context"],
                "metadata_extraction_llm": names["metadata"],
                "topic_tagging_llm": names["topic"],
            },
        ),
        (
            names["retrieval-preset"],
            "retrieval",
            {"llm": names["retrieval"], "reranker": names["reranker"]},
        ),
        (names["unused-preset"], "retrieval", {"llm": names["unused"]}),
        (names["broken-endpoint-preset"], "retrieval", {"llm": names["missing"]}),
    ]
    partition_rows = [
        (
            partition_id,
            names["main-partition"],
            "default",
            names["indexation-preset"],
            names["retrieval-preset"],
            names["chat"],
        ),
        (
            partition_id - 1,
            names["missing-endpoint-partition"],
            names["embedder"],
            names["indexation-preset"],
            names["broken-endpoint-preset"],
            None,
        ),
        (
            partition_id - 2,
            names["missing-preset-partition"],
            names["embedder"],
            names["deleted-preset"],
            names["retrieval-preset"],
            None,
        ),
    ]
    async with pool.acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        try:
            # Keep the default swap invisible and block concurrent endpoint
            # writers until the original state has been restored.
            await conn.execute("LOCK TABLE model_endpoints IN SHARE ROW EXCLUSIVE MODE")
            old_embedder_defaults = await conn.fetch(
                "SELECT name, model_type FROM model_endpoints WHERE model_type = 'embedder' AND is_default"
            )
            try:
                await conn.execute("UPDATE model_endpoints SET is_default = false WHERE model_type = 'embedder'")
                await conn.executemany(
                    """
                    INSERT INTO model_endpoints
                        (name, model_type, endpoint, model_name, batch_size, timeout, extra, is_default, vector_field)
                    VALUES ($1, $2, 'https://models.test/v1', $1, 4, 5, '{}'::jsonb, $3,
                            CASE WHEN $2::varchar = 'embedder' THEN 'vector_' || $1::varchar END)
                    """,
                    endpoint_rows,
                )
                await conn.executemany(
                    "INSERT INTO pipeline_presets (name, preset_type, config) VALUES ($1, $2, $3::jsonb)",
                    preset_rows,
                )
                await conn.executemany(
                    """
                    INSERT INTO partitions
                        (id, partition, embedder, indexation_preset, retrieval_preset, chat_llm)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    partition_rows,
                )

                result = await PgModelEndpointRepository(lambda: conn).discover_readiness_targets(
                    default_model_kinds=("embedder", "reranker", "llm", "vlm", "stt")
                )
                identities = {(target.kind, target.provider) for target in result.targets}

                assert {
                    ("embedder", names["embedder"]),
                    ("llm", names["chat"]),
                    ("llm", names["retrieval"]),
                    ("reranker", names["reranker"]),
                    ("vlm", names["vlm"]),
                    ("stt", names["stt"]),
                    ("llm", names["context"]),
                    ("llm", names["metadata"]),
                    ("llm", names["topic"]),
                    ("llm", names["missing"]),
                } <= identities
                assert ("llm", names["unused"]) not in identities
                assert ("embedder", "default") not in identities
                assert next(target for target in result.targets if target.provider == names["missing"]).config is None
                assert ("indexation_preset", names["deleted-preset"]) in {
                    (finding.kind, finding.name) for finding in result.configuration_references
                }
            finally:
                await conn.executemany(
                    "DELETE FROM partitions WHERE partition = $1",
                    [(row[1],) for row in partition_rows],
                )
                await conn.executemany(
                    "DELETE FROM pipeline_presets WHERE name = $1 AND preset_type = $2",
                    [(row[0], row[1]) for row in preset_rows],
                )
                await conn.executemany(
                    "DELETE FROM model_endpoints WHERE name = $1 AND model_type = $2",
                    [(row[0], row[1]) for row in endpoint_rows],
                )
                await conn.executemany(
                    "UPDATE model_endpoints SET is_default = true WHERE name = $1 AND model_type = $2",
                    [(row["name"], row["model_type"]) for row in old_embedder_defaults],
                )
        finally:
            # Roll back successful teardown too, including preset revision
            # trigger side effects, so the shared database is logically unchanged.
            await transaction.rollback()
