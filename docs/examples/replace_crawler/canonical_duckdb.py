import duckdb

con = duckdb.connect()
con.execute("INSTALL httpfs")
con.execute("LOAD httpfs")
rows = con.execute(
    """
    SELECT id
    FROM read_parquet(?)
    WHERE bbox.xmax >= 85.49 AND bbox.xmin <= 85.56
      AND bbox.ymax >= 28.26 AND bbox.ymin <= 28.32
      AND CAST(datetime AS DATE) >= DATE '2026-09-01'
      AND "umbra-py:canonical" = true
    LIMIT 5
    """,
    [
        "https://github.com/reesehammer/umbra-py/releases/download/"
        "catalog-index/umbra-open-data.parquet"
    ],
).fetchall()
for (item_id,) in rows:
    print(item_id)
