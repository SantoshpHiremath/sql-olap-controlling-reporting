"""
Runs every query in olap_queries.sql against the real star schema
database and prints the results -- proves the queries actually execute
against a real SQL engine, not just that the .sql file parses visually.
"""
import os
import re

import duckdb


def split_queries(sql_text: str) -> list:
    """Split the .sql file into individual statements on the '-- ====='
    banner lines, stripping comments."""
    blocks = re.split(r"-- =+\n", sql_text)
    queries = []
    for block in blocks:
        lines = [l for l in block.splitlines() if not l.strip().startswith("--")]
        stmt = "\n".join(lines).strip()
        if stmt:
            queries.append(stmt.rstrip(";"))
    return queries


def run_all(db_path: str = "controlling_olap.duckdb", sql_path: str = "olap_queries.sql"):
    con = duckdb.connect(db_path, read_only=True)
    with open(sql_path, "r", encoding="utf-8") as f:
        sql_text = f.read()

    queries = split_queries(sql_text)
    results = []
    for i, q in enumerate(queries, start=1):
        title_match = re.search(r"Query \d+: ([^\n]+)", sql_text.split(q[:30])[0][-500:]) \
            if False else None  # titles are printed via the banners already stripped; keep simple
        df = con.execute(q).fetchdf()
        print(f"\n--- Query {i} ({len(df)} rows) ---")
        print(df.head(10).to_string(index=False))
        results.append(df)

    con.close()
    return results


if __name__ == "__main__":
    run_all()
