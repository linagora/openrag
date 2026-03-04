"""
Workspace Search Benchmark: ARRAY_CONTAINS vs PG resolution + file_id IN batching.

Compares two approaches for filtering search results by workspace membership:
  A) Milvus ARRAY field with ARRAY_CONTAINS + INVERTED index
  B) PostgreSQL resolution → file_id in [...] with batching for >1000 files

Usage:
  cd benchmark && docker compose up -d
  pip install -r requirements.txt
  python benchmark.py                    # print to stdout
  python benchmark.py -o results.md      # also write to file
"""

import argparse
import time
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
from pymilvus import (
    Collection,
    DataType,
    Function,
    FunctionType,
    MilvusClient,
    AnnSearchRequest,
    RRFRanker,
    connections,
)
from sqlalchemy import create_engine, text
from tabulate import tabulate

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MILVUS_URI = "http://localhost:19530"
PG_URL = "postgresql://bench:bench@localhost:5433/bench"

COLLECTION = "bench_collection"
PARTITION_NAME = "bench"
VECTOR_DIM = 1024
FILES = 5000
CHUNKS_PER_FILE = 20
INSERT_BATCH = 1000

WARMUP_RUNS = 5
MEASURED_RUNS = 20
TOP_K = 10
SEARCH_EF = 64

BATCH_SIZE = 1000  # file_id batching threshold for Approach B

# Search params matching OpenRAG production config
DENSE_SEARCH_PARAMS = {
    "metric_type": "COSINE",
    "params": {"ef": SEARCH_EF, "radius": 0.0, "range_filter": 1.0},
}
DENSE_ANN_PARAMS = {
    "metric_type": "COSINE",
    "params": {"ef": SEARCH_EF, "radius": 0.0, "range_filter": 1.0},
}
SPARSE_ANN_PARAMS = {
    "metric_type": "BM25",
    "params": {"drop_ratio_build": 0.2},
}

# Scenario definitions: (scenario_name, workspace_id, num_files)
SCENARIOS = [
    ("S1 (1 file)", "ws_single", 1),
    ("S2 (5000 files)", "ws_5000", 5000),
    ("S3 (2500 files)", "ws_2500_a", 2500),
    ("S4 (1000 files)", "ws_1000_a", 1000),
]

# Word pool for generating random text (BM25 needs real-ish tokens)
WORDS = (
    "the of and to a in is it that was for on are with as his they be at one "
    "have this from by hot but some what there we can out other were all your "
    "when up use word how said each she which do their time if will way about "
    "many then them would write like so these her long make thing see him two "
    "has look more day could go come did my sound no most number who over know "
    "water than call first people may down side been now find head stand own "
    "page should country found answer school grow study still learn plant cover "
    "food sun four thought let keep eye never last door between city tree cross "
    "farm hard start might story saw far sea draw left late run while press "
    "close night real life few stop open seem together next white children begin "
    "got walk example ease paper often always music those both mark book letter "
    "until mile river car feet care second group carry took rain eat room friend "
    "began idea fish mountain north once base hear horse cut sure watch color "
    "face wood main enough plain girl usual young ready above ever red list "
    "though feel talk bird soon body dog family direct pose leave song measure"
).split()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def random_text(rng: np.random.Generator, n_words: int = 40) -> str:
    """Generate random text from word pool."""
    indices = rng.integers(0, len(WORDS), size=n_words)
    return " ".join(WORDS[i] for i in indices)


def random_vectors(rng: np.random.Generator, n: int) -> list[list[float]]:
    """Generate n random unit vectors of VECTOR_DIM dimensions."""
    vecs = rng.standard_normal((n, VECTOR_DIM)).astype(np.float32)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    vecs = vecs / norms
    return vecs.tolist()


def timed(fn):
    """Execute fn() and return (elapsed_seconds, result)."""
    start = time.perf_counter()
    result = fn()
    elapsed = time.perf_counter() - start
    return elapsed, result


def stats_ms(times: list[float]) -> dict:
    """Compute stats from a list of times in seconds, return in milliseconds."""
    ms = [t * 1000 for t in times]
    return {
        "median": round(statistics.median(ms), 2),
        "mean": round(statistics.mean(ms), 2),
        "p95": round(float(np.percentile(ms, 95)), 2),
        "min": round(min(ms), 2),
        "max": round(max(ms), 2),
    }


# ---------------------------------------------------------------------------
# File ↔ workspace assignment
# ---------------------------------------------------------------------------
def build_workspace_assignments() -> dict[str, list[str]]:
    """
    Returns {workspace_id: [file_id, ...]} mapping.
    File IDs are f"file_{i:05d}" for i in 0..4999.
    """
    all_files = [f"file_{i:05d}" for i in range(FILES)]
    assignments = {}

    # S1: 1 file
    assignments["ws_single"] = [all_files[0]]

    # S2: all 5000
    assignments["ws_5000"] = list(all_files)

    # S3: two non-overlapping 2500-file workspaces
    assignments["ws_2500_a"] = all_files[:2500]
    assignments["ws_2500_b"] = all_files[2500:]

    # S4: five non-overlapping 1000-file workspaces
    for j in range(5):
        assignments[f"ws_1000_{chr(ord('a') + j)}"] = all_files[
            j * 1000 : (j + 1) * 1000
        ]

    return assignments


def file_to_workspaces(assignments: dict[str, list[str]]) -> dict[str, list[str]]:
    """Invert workspace assignments to {file_id: [workspace_id, ...]}."""
    mapping: dict[str, list[str]] = {}
    for ws_id, file_ids in assignments.items():
        for fid in file_ids:
            mapping.setdefault(fid, []).append(ws_id)
    return mapping


# ---------------------------------------------------------------------------
# Milvus setup
# ---------------------------------------------------------------------------
def setup_milvus(client: MilvusClient):
    """Create collection with schema matching OpenRAG + workspace_ids ARRAY field."""
    if client.has_collection(COLLECTION):
        client.drop_collection(COLLECTION)

    schema = client.create_schema(enable_dynamic_field=True)

    schema.add_field("_id", DataType.INT64, is_primary=True, auto_id=True)
    schema.add_field(
        "text",
        DataType.VARCHAR,
        max_length=65535,
        enable_analyzer=True,
        enable_match=True,
    )
    schema.add_field("partition", DataType.VARCHAR, max_length=65535, is_partition_key=True)
    schema.add_field("file_id", DataType.VARCHAR, max_length=65535)
    schema.add_field("vector", DataType.FLOAT_VECTOR, dim=VECTOR_DIM)
    schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
    schema.add_field(
        "workspace_ids",
        DataType.ARRAY,
        element_type=DataType.VARCHAR,
        max_capacity=20,
        max_length=128,
    )

    # BM25 function: text → sparse
    bm25_fn = Function(
        name="bm25",
        function_type=FunctionType.BM25,
        input_field_names=["text"],
        output_field_names=["sparse"],
    )
    schema.add_function(bm25_fn)

    index_params = client.prepare_index_params()
    index_params.add_index(field_name="file_id", index_type="INVERTED", index_name="file_id_idx")
    index_params.add_index(field_name="partition", index_type="INVERTED", index_name="partition_idx")
    index_params.add_index(
        field_name="vector",
        index_type="HNSW",
        metric_type="COSINE",
        params={"M": 128, "efConstruction": 256},
        index_name="vector_idx",
    )
    index_params.add_index(
        field_name="sparse",
        index_type="SPARSE_INVERTED_INDEX",
        metric_type="BM25",
        params={
            "bm25_k1": 1.2,
            "bm25_b": 0.75,
            "inverted_index_algo": "DAAT_MAXSCORE",
        },
        index_name="sparse_idx",
    )
    index_params.add_index(
        field_name="workspace_ids",
        index_type="INVERTED",
        index_name="workspace_ids_idx",
    )

    client.create_collection(
        collection_name=COLLECTION,
        schema=schema,
        index_params=index_params,
    )
    print(f"  Collection '{COLLECTION}' created with indexes.")


# ---------------------------------------------------------------------------
# PostgreSQL setup
# ---------------------------------------------------------------------------
def setup_postgres(engine):
    """Create PG tables for Approach B."""
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS file_workspaces CASCADE"))
        conn.execute(text("DROP TABLE IF EXISTS files CASCADE"))
        conn.execute(
            text("""
            CREATE TABLE files (
                id SERIAL PRIMARY KEY,
                file_id VARCHAR NOT NULL,
                partition_name VARCHAR NOT NULL,
                UNIQUE(file_id, partition_name)
            )
        """)
        )
        conn.execute(
            text(
                "CREATE INDEX ix_partition_file ON files(partition_name, file_id)"
            )
        )
        conn.execute(
            text("""
            CREATE TABLE file_workspaces (
                id SERIAL PRIMARY KEY,
                file_id VARCHAR NOT NULL,
                workspace_id VARCHAR NOT NULL,
                partition_name VARCHAR NOT NULL,
                UNIQUE(file_id, workspace_id, partition_name)
            )
        """)
        )
        conn.execute(
            text(
                "CREATE INDEX ix_ws_partition ON file_workspaces(workspace_id, partition_name)"
            )
        )
    print("  PostgreSQL tables created.")


# ---------------------------------------------------------------------------
# Data generation & insertion
# ---------------------------------------------------------------------------
def insert_data(
    client: MilvusClient,
    engine,
    assignments: dict[str, list[str]],
):
    """Generate and insert 100K chunks into Milvus + file/workspace records into PG."""
    rng = np.random.default_rng(42)
    file_ws_map = file_to_workspaces(assignments)
    all_file_ids = [f"file_{i:05d}" for i in range(FILES)]

    # --- Milvus inserts (batched) ---
    print(f"  Inserting {FILES * CHUNKS_PER_FILE:,} chunks into Milvus...")
    total_inserted = 0
    batch_data: list[dict] = []

    for file_idx, fid in enumerate(all_file_ids):
        ws_ids = file_ws_map.get(fid, [])
        for chunk_idx in range(CHUNKS_PER_FILE):
            batch_data.append(
                {
                    "text": random_text(rng),
                    "partition": PARTITION_NAME,
                    "file_id": fid,
                    "vector": random_vectors(rng, 1)[0],
                    "workspace_ids": ws_ids,
                }
            )

            if len(batch_data) >= INSERT_BATCH:
                client.insert(collection_name=COLLECTION, data=batch_data)
                total_inserted += len(batch_data)
                batch_data = []
                if total_inserted % 10000 == 0:
                    print(f"    {total_inserted:,} / {FILES * CHUNKS_PER_FILE:,}")

    if batch_data:
        client.insert(collection_name=COLLECTION, data=batch_data)
        total_inserted += len(batch_data)

    print(f"    {total_inserted:,} chunks inserted.")

    # Flush to make data searchable
    client.flush(COLLECTION)
    print("  Milvus flush complete.")

    # --- PostgreSQL inserts ---
    print("  Inserting file and workspace records into PostgreSQL...")
    with engine.begin() as conn:
        # files table
        for fid in all_file_ids:
            conn.execute(
                text(
                    "INSERT INTO files (file_id, partition_name) VALUES (:fid, :p)"
                ),
                {"fid": fid, "p": PARTITION_NAME},
            )

        # file_workspaces table
        for ws_id, file_ids in assignments.items():
            for fid in file_ids:
                conn.execute(
                    text(
                        "INSERT INTO file_workspaces (file_id, workspace_id, partition_name) "
                        "VALUES (:fid, :ws, :p)"
                    ),
                    {"fid": fid, "ws": ws_id, "p": PARTITION_NAME},
                )
    print("  PostgreSQL inserts complete.")


# ---------------------------------------------------------------------------
# Search functions
# ---------------------------------------------------------------------------
def make_query_vector(rng: np.random.Generator) -> list[float]:
    """Generate a single random query vector."""
    return random_vectors(rng, 1)[0]


def _hybrid_search(
    col: Collection,
    query_vector: list[float],
    query_text: str,
    filter_expr: str,
):
    """Run a hybrid (dense + BM25) search on the Collection API."""
    dense_req = AnnSearchRequest(
        data=[query_vector],
        anns_field="vector",
        param=DENSE_ANN_PARAMS,
        limit=TOP_K,
        expr=filter_expr,
    )
    sparse_req = AnnSearchRequest(
        data=[query_text],
        anns_field="sparse",
        param=SPARSE_ANN_PARAMS,
        limit=TOP_K,
        expr=filter_expr,
    )
    return col.hybrid_search(
        [dense_req, sparse_req],
        rerank=RRFRanker(k=100),
        limit=TOP_K,
        output_fields=["file_id"],
    )


def search_approach_a(
    client: MilvusClient,
    col: Collection,
    workspace_id: str,
    query_vector: list[float],
    query_text: str,
    search_type: str,
) -> float:
    """
    Approach A: Milvus ARRAY_CONTAINS on workspace_ids field.
    Returns elapsed time in seconds.
    """
    filter_expr = f'ARRAY_CONTAINS(workspace_ids, "{workspace_id}")'

    if search_type == "dense":
        t, _ = timed(
            lambda: client.search(
                collection_name=COLLECTION,
                data=[query_vector],
                anns_field="vector",
                filter=filter_expr,
                limit=TOP_K,
                output_fields=["file_id"],
                search_params=DENSE_SEARCH_PARAMS,
            )
        )
    else:  # hybrid
        t, _ = timed(
            lambda: _hybrid_search(col, query_vector, query_text, filter_expr)
        )

    return t


def pg_resolve_workspace(engine, workspace_id: str) -> tuple[float, list[str]]:
    """
    Approach B step 1: resolve workspace → file_ids via PostgreSQL.
    Returns (elapsed_seconds, file_ids).
    """

    def _query():
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT file_id FROM file_workspaces "
                    "WHERE workspace_id = :ws AND partition_name = :p"
                ),
                {"ws": workspace_id, "p": PARTITION_NAME},
            ).fetchall()
        return [r[0] for r in rows]

    t, file_ids = timed(_query)
    return t, file_ids


def search_approach_b_single(
    client: MilvusClient,
    col: Collection,
    file_ids: list[str],
    query_vector: list[float],
    query_text: str,
    search_type: str,
) -> float:
    """
    Approach B step 2 (single batch, ≤1000 files): Milvus search with file_id IN.
    Returns elapsed time in seconds.
    """
    id_list = ", ".join(f'"{fid}"' for fid in file_ids)
    filter_expr = f"file_id in [{id_list}]"

    if search_type == "dense":
        t, _ = timed(
            lambda: client.search(
                collection_name=COLLECTION,
                data=[query_vector],
                anns_field="vector",
                filter=filter_expr,
                limit=TOP_K,
                output_fields=["file_id"],
                search_params=DENSE_SEARCH_PARAMS,
            )
        )
    else:  # hybrid
        t, _ = timed(
            lambda: _hybrid_search(col, query_vector, query_text, filter_expr)
        )

    return t


def search_approach_b_batched(
    client: MilvusClient,
    col: Collection,
    file_ids: list[str],
    query_vector: list[float],
    query_text: str,
    search_type: str,
) -> tuple[float, float]:
    """
    Approach B step 2+3 (batched, >1000 files): multiple Milvus searches + merge.
    Returns (search_time, merge_time) in seconds.
    """
    batches = [
        file_ids[i : i + BATCH_SIZE] for i in range(0, len(file_ids), BATCH_SIZE)
    ]

    # Step 2: search batches in parallel using threads
    all_results = []

    def _search_one_batch(batch):
        id_list = ", ".join(f'"{fid}"' for fid in batch)
        filter_expr = f"file_id in [{id_list}]"
        if search_type == "dense":
            return client.search(
                collection_name=COLLECTION,
                data=[query_vector],
                anns_field="vector",
                filter=filter_expr,
                limit=TOP_K,
                output_fields=["file_id"],
                search_params=DENSE_SEARCH_PARAMS,
            )
        else:  # hybrid
            return _hybrid_search(col, query_vector, query_text, filter_expr)

    def _search_batches():
        with ThreadPoolExecutor(max_workers=len(batches)) as executor:
            futures = [executor.submit(_search_one_batch, b) for b in batches]
            for f in as_completed(futures):
                all_results.append(f.result())

    t_search, _ = timed(_search_batches)

    # Step 3: merge + dedup + re-sort
    def _merge():
        seen: dict[int, dict] = {}
        for batch_res in all_results:
            for hits in batch_res:
                for hit in hits:
                    _id = hit.id
                    dist = hit.distance
                    if _id not in seen or dist > seen[_id]["distance"]:
                        seen[_id] = {"id": _id, "distance": dist}
        merged = sorted(seen.values(), key=lambda x: x["distance"], reverse=True)
        return merged[:TOP_K]

    t_merge, _ = timed(_merge)
    return t_search, t_merge


# ---------------------------------------------------------------------------
# Write benchmarks
# ---------------------------------------------------------------------------
WRITE_FIELDS = ["_id", "text", "partition", "file_id", "vector", "workspace_ids"]


def bench_write_approach_a(client: MilvusClient) -> list[float]:
    """
    Approach A write: add 1 file (20 chunks) to a workspace via upsert.
    Measures: query existing chunks → append workspace_id → upsert.

    Note: partial_update=True doesn't work reliably with BM25 function fields
    in pymilvus 2.6.5 (the `text` → `sparse` function breaks on repeated upserts).
    So we query all fields and do a full upsert instead.
    """
    target_file = "file_00000"
    new_ws = "ws_write_test_a"
    times = []

    for _ in range(MEASURED_RUNS):

        def _write():
            # Step 1: query all fields for this file
            results = client.query(
                collection_name=COLLECTION,
                filter=f'file_id == "{target_file}"',
                output_fields=WRITE_FIELDS,
                limit=CHUNKS_PER_FILE + 10,
            )
            # Step 2: append workspace_id and full upsert
            upsert_data = []
            for row in results:
                ws_ids = list(row["workspace_ids"])
                if new_ws not in ws_ids:
                    ws_ids.append(new_ws)
                upsert_data.append(
                    {
                        "_id": row["_id"],
                        "text": row["text"],
                        "partition": row["partition"],
                        "file_id": row["file_id"],
                        "vector": row["vector"],
                        "workspace_ids": ws_ids,
                    }
                )
            if upsert_data:
                client.upsert(collection_name=COLLECTION, data=upsert_data)

        t, _ = timed(_write)
        times.append(t)

        # Cleanup: remove the added workspace_id for next iteration
        results = client.query(
            collection_name=COLLECTION,
            filter=f'file_id == "{target_file}"',
            output_fields=WRITE_FIELDS,
            limit=CHUNKS_PER_FILE + 10,
        )
        cleanup = []
        for row in results:
            ws_ids = [w for w in row["workspace_ids"] if w != new_ws]
            cleanup.append(
                {
                    "_id": row["_id"],
                    "text": row["text"],
                    "partition": row["partition"],
                    "file_id": row["file_id"],
                    "vector": row["vector"],
                    "workspace_ids": ws_ids,
                }
            )
        if cleanup:
            client.upsert(collection_name=COLLECTION, data=cleanup)

    return times


def bench_write_approach_b(engine) -> list[float]:
    """
    Approach B write: INSERT INTO file_workspaces for 1 file.
    """
    target_file = "file_00000"
    new_ws = "ws_write_test_b"
    times = []

    for _ in range(MEASURED_RUNS):

        def _write():
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO file_workspaces (file_id, workspace_id, partition_name) "
                        "VALUES (:fid, :ws, :p) "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {"fid": target_file, "ws": new_ws, "p": PARTITION_NAME},
                )

        t, _ = timed(_write)
        times.append(t)

        # Cleanup
        with engine.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM file_workspaces WHERE file_id = :fid AND workspace_id = :ws"
                ),
                {"fid": target_file, "ws": new_ws},
            )

    return times


# ---------------------------------------------------------------------------
# Main benchmark
# ---------------------------------------------------------------------------
def main(output_file: str | None = None):
    print("=" * 70)
    print("Workspace Search Benchmark")
    print("=" * 70)

    # Connect
    print("\n[1/6] Connecting...")
    client = MilvusClient(uri=MILVUS_URI)
    connections.connect(alias="bench_hybrid", uri=MILVUS_URI)
    engine = create_engine(PG_URL)
    print("  Connected to Milvus and PostgreSQL.")

    # Setup
    print("\n[2/6] Setting up schema...")
    setup_milvus(client)
    setup_postgres(engine)

    # Data generation
    print("\n[3/6] Generating and inserting data...")
    assignments = build_workspace_assignments()
    insert_data(client, engine, assignments)

    # Wait for index building
    print("  Waiting for Milvus indexes to build...")
    client.load_collection(COLLECTION)
    print("  Collection loaded.")

    rng = np.random.default_rng(99)
    col = Collection(COLLECTION, using="bench_hybrid")

    # Warmup — exercise both approaches and both search types
    print(f"\n[4/6] Warming up ({WARMUP_RUNS} queries per path)...")
    for _ in range(WARMUP_RUNS):
        qv = make_query_vector(rng)
        qt = random_text(rng)
        # Approach A dense
        client.search(
            collection_name=COLLECTION,
            data=[qv],
            anns_field="vector",
            filter='ARRAY_CONTAINS(workspace_ids, "ws_1000_a")',
            limit=TOP_K,
            output_fields=["file_id"],
            search_params=DENSE_SEARCH_PARAMS,
        )
        # Approach A hybrid
        _hybrid_search(col, qv, qt, 'ARRAY_CONTAINS(workspace_ids, "ws_1000_a")')
        # Approach B dense (file_id IN)
        client.search(
            collection_name=COLLECTION,
            data=[qv],
            anns_field="vector",
            filter='file_id in ["file_00000", "file_00001"]',
            limit=TOP_K,
            output_fields=["file_id"],
            search_params=DENSE_SEARCH_PARAMS,
        )
        # Approach B hybrid (file_id IN)
        _hybrid_search(col, qv, qt, 'file_id in ["file_00000", "file_00001"]')
        # PG warmup
        pg_resolve_workspace(engine, "ws_1000_a")
    print("  Warmup complete.")

    # Search benchmarks
    print(f"\n[5/6] Running search benchmarks ({MEASURED_RUNS} runs each)...")
    results_table = []

    for scenario_name, ws_id, num_files in SCENARIOS:
        for search_type in ["dense", "hybrid"]:
            print(f"\n  {scenario_name} / {search_type}:")

            # Use the same query vector and text for both approaches in each run
            times_a = []
            times_b_pg = []
            times_b_search = []
            times_b_merge = []
            times_b_total = []

            for run in range(MEASURED_RUNS):
                qv = make_query_vector(rng)
                qt = random_text(rng)

                # Approach A
                ta = search_approach_a(client, col, ws_id, qv, qt, search_type)
                times_a.append(ta)

                # Approach B
                t_pg, file_ids = pg_resolve_workspace(engine, ws_id)
                times_b_pg.append(t_pg)

                if len(file_ids) <= BATCH_SIZE:
                    t_search = search_approach_b_single(
                        client, col, file_ids, qv, qt, search_type
                    )
                    t_merge = 0.0
                else:
                    t_search, t_merge = search_approach_b_batched(
                        client, col, file_ids, qv, qt, search_type
                    )

                times_b_search.append(t_search)
                times_b_merge.append(t_merge)
                times_b_total.append(t_pg + t_search + t_merge)

            sa = stats_ms(times_a)
            sb_total = stats_ms(times_b_total)
            sb_pg = stats_ms(times_b_pg)
            sb_search = stats_ms(times_b_search)
            sb_merge = stats_ms(times_b_merge)

            print(f"    A (ARRAY):  median={sa['median']}ms  p95={sa['p95']}ms")
            print(
                f"    B (PG+IN):  median={sb_total['median']}ms  p95={sb_total['p95']}ms"
                f"  (pg={sb_pg['median']}ms  search={sb_search['median']}ms"
                f"  merge={sb_merge['median']}ms)"
            )

            batches = (
                max(1, (num_files + BATCH_SIZE - 1) // BATCH_SIZE)
                if num_files > BATCH_SIZE
                else 0
            )
            results_table.append(
                {
                    "Scenario": scenario_name,
                    "Search": search_type,
                    "Files": num_files,
                    "Batches": batches,
                    "A median (ms)": sa["median"],
                    "A p95 (ms)": sa["p95"],
                    "B total median (ms)": sb_total["median"],
                    "B total p95 (ms)": sb_total["p95"],
                    "B pg (ms)": sb_pg["median"],
                    "B search (ms)": sb_search["median"],
                    "B merge (ms)": sb_merge["median"],
                }
            )

    # Write benchmarks
    print(f"\n[6/6] Running write benchmarks ({MEASURED_RUNS} runs each)...")
    write_a_times = bench_write_approach_a(client)
    write_b_times = bench_write_approach_b(engine)

    wa = stats_ms(write_a_times)
    wb = stats_ms(write_b_times)

    # ---------------------------------------------------------------------------
    # Format results
    # ---------------------------------------------------------------------------
    write_table = [
        {
            "Approach": "A (ARRAY upsert)",
            "median (ms)": wa["median"],
            "mean (ms)": wa["mean"],
            "p95 (ms)": wa["p95"],
            "min (ms)": wa["min"],
            "max (ms)": wa["max"],
        },
        {
            "Approach": "B (PG INSERT)",
            "median (ms)": wb["median"],
            "mean (ms)": wb["mean"],
            "p95 (ms)": wb["p95"],
            "min (ms)": wb["min"],
            "max (ms)": wb["max"],
        },
    ]

    summary_lines = []
    for row in results_table:
        a = row["A median (ms)"]
        b = row["B total median (ms)"]
        winner = "A" if a < b else "B"
        diff = abs(a - b)
        ratio = max(a, b) / min(a, b) if min(a, b) > 0 else float("inf")
        summary_lines.append(
            f"  {row['Scenario']:20s} {row['Search']:7s}: "
            f"A={a:7.2f}ms  B={b:7.2f}ms  -> {winner} wins by {diff:.2f}ms ({ratio:.1f}x)"
        )
    write_winner = "A" if wa["median"] < wb["median"] else "B"
    summary_lines.append(
        f"\n  Write: A={wa['median']:.2f}ms  B={wb['median']:.2f}ms"
        f"  -> {write_winner} wins"
    )

    # Print to stdout
    print("\n" + "=" * 70)
    print("SEARCH RESULTS")
    print("=" * 70)
    print(tabulate(results_table, headers="keys", tablefmt="github", floatfmt=".2f"))
    print("\n" + "=" * 70)
    print("WRITE RESULTS (add 1 file / 20 chunks to workspace)")
    print("=" * 70)
    print(tabulate(write_table, headers="keys", tablefmt="github", floatfmt=".2f"))
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print("\n".join(summary_lines))
    print("\nDone.")

    # Write to file if requested
    if output_file:
        with open(output_file, "w") as f:
            f.write("# Workspace Search Benchmark Results\n\n")
            f.write("## Search Results\n\n")
            f.write(tabulate(results_table, headers="keys", tablefmt="github", floatfmt=".2f"))
            f.write("\n\n## Write Results (add 1 file / 20 chunks to workspace)\n\n")
            f.write(tabulate(write_table, headers="keys", tablefmt="github", floatfmt=".2f"))
            f.write("\n\n## Summary\n\n```\n")
            f.write("\n".join(summary_lines))
            f.write("\n```\n")
        print(f"\nResults written to {output_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Workspace search benchmark")
    parser.add_argument("-o", "--output", help="Write results to a markdown file")
    args = parser.parse_args()
    main(output_file=args.output)
