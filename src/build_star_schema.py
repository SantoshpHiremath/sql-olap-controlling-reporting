"""
Builds a real OLAP-style star schema for controlling reporting: a fact
table of postings (Buchungen) plus three dimension tables (Kostenstelle,
Konto, Zeit), loaded into a real SQL database (DuckDB) so it can be
queried with genuine SQL -- including T-SQL-equivalent OLAP extensions
(GROUP BY ROLLUP/CUBE/GROUPING SETS, window functions) that are the same
constructs used against a real SQL Server OLAP/data-warehouse setup.

Reuses the same underlying dataset shape as
controlling-excel-variance-analysis, but modeled properly as a star
schema instead of a single flat table -- this is deliberate: a flat
budget/actual table is what a controller opens in Excel, but a star
schema with a real time dimension (with year/quarter/month hierarchy
columns for drill-down) and a grain of one row per posting (not one row
per cost-center/account/month aggregate) is what an OLAP/SQL-Server-
backed reporting layer actually looks like.
"""
import random
import sys
import os
from datetime import date

import duckdb

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..",
                                  "controlling-excel-variance-analysis", "src"))

COST_CENTERS = [
    ("1000", "Produktion Backwaren", "Herstellkosten"),
    ("1100", "Produktion Konditorei", "Herstellkosten"),
    ("2000", "Filialen München Zentrum", "Vertrieb"),
    ("2100", "Filialen München Umland", "Vertrieb"),
    ("2200", "Filialen Franchise", "Vertrieb"),
    ("3000", "Logistik & Fuhrpark", "Logistik"),
    ("4000", "Einkauf & Wareneinsatz", "Material"),
    ("5000", "Verwaltung", "Verwaltung"),
    ("6000", "Marketing", "Verwaltung"),
]

ACCOUNTS = [
    ("60000", "Personalkosten", "Aufwand"),
    ("61000", "Materialaufwand", "Aufwand"),
    ("62000", "Energiekosten", "Aufwand"),
    ("63000", "Miete & Nebenkosten", "Aufwand"),
    ("64000", "Fuhrpark & Logistik", "Aufwand"),
    ("65000", "Marketing & Werbung", "Aufwand"),
    ("66000", "Sonstige betriebliche Aufwendungen", "Aufwand"),
]


def _quarter(month: int) -> int:
    return (month - 1) // 3 + 1


def build_dim_zeit(con):
    rows = []
    date_key = 1
    for year in (2025, 2026):
        for month in range(1, 13):
            d = date(year, month, 1)
            rows.append((
                date_key, d.isoformat(), year, _quarter(month), month,
                d.strftime("%B"), f"{year}-Q{_quarter(month)}", f"{year}-{month:02d}",
            ))
            date_key += 1
    con.executemany(
        "INSERT INTO Dim_Zeit VALUES (?,?,?,?,?,?,?,?)", rows,
    )
    return {r[7]: r[0] for r in rows}  # period string -> date_key


def build_dim_kostenstelle(con):
    rows = [(i + 1, cc_id, name, division) for i, (cc_id, name, division) in enumerate(COST_CENTERS)]
    con.executemany("INSERT INTO Dim_Kostenstelle VALUES (?,?,?,?)", rows)
    return {r[1]: r[0] for r in rows}  # cc_id -> surrogate key


def build_dim_konto(con):
    rows = [(i + 1, acc_id, name, art) for i, (acc_id, name, art) in enumerate(ACCOUNTS)]
    con.executemany("INSERT INTO Dim_Konto VALUES (?,?,?,?)", rows)
    return {r[1]: r[0] for r in rows}  # account_id -> surrogate key


def build_fact_buchungen(con, zeit_map, kst_map, konto_map, seed: int = 42):
    rng = random.Random(seed)

    baseline = {}
    for cc_id, _, _ in COST_CENTERS:
        for acc_id, _, _ in ACCOUNTS:
            cc_scale = {
                "1000": 3.2, "1100": 1.4, "2000": 2.6, "2100": 1.8,
                "2200": 1.1, "3000": 1.6, "4000": 2.2, "5000": 1.0, "6000": 0.6,
            }[cc_id]
            account_base = {
                "60000": 42000, "61000": 38000, "62000": 6500, "63000": 9000,
                "64000": 7000, "65000": 4000, "66000": 3000,
            }[acc_id]
            baseline[(cc_id, acc_id)] = round(account_base * cc_scale * rng.uniform(0.92, 1.08), 2)

    def seasonal_index(period, cc_id, acc_id):
        month = int(period.split("-")[1])
        idx = 1.0
        if acc_id == "62000":
            idx *= 1.25 if month in (11, 12, 1, 2) else 0.90
        if cc_id == "1100" and month in (11, 12):
            idx *= 1.45
        if cc_id == "6000" and month in (11, 12):
            idx *= 1.6
        if cc_id in ("2000", "2100", "2200") and month in (6, 7, 8):
            idx *= 1.08
        return idx

    overrun_start = "2026-04"
    fact_rows = []
    posting_id = 1
    periods = sorted(zeit_map.keys())

    for period in periods:
        for cc_id, _, _ in COST_CENTERS:
            for acc_id, _, _ in ACCOUNTS:
                base = baseline[(cc_id, acc_id)]
                s_idx = seasonal_index(period, cc_id, acc_id)
                budget = round(base * s_idx, 2)

                actual_factor = rng.gauss(1.0, 0.06)
                if cc_id == "3000" and acc_id == "64000" and period >= overrun_start:
                    actual_factor *= 1.28
                if cc_id == "1000" and acc_id == "66000" and period == "2026-03":
                    actual_factor *= 3.1
                if cc_id == "6000":
                    actual_factor *= 0.88
                actual = round(budget * actual_factor, 2)

                fact_rows.append((
                    posting_id, zeit_map[period], kst_map[cc_id], konto_map[acc_id],
                    budget, actual,
                ))
                posting_id += 1

    con.executemany(
        "INSERT INTO Fact_Buchungen VALUES (?,?,?,?,?,?)", fact_rows,
    )
    return len(fact_rows)


def build(db_path: str = "controlling_olap.duckdb", seed: int = 42):
    if os.path.exists(db_path):
        os.remove(db_path)
    con = duckdb.connect(db_path)

    con.execute("""
        CREATE TABLE Dim_Zeit (
            date_key INTEGER PRIMARY KEY,
            date_iso VARCHAR,
            jahr INTEGER,
            quartal INTEGER,
            monat INTEGER,
            monatsname VARCHAR,
            quartal_label VARCHAR,
            periode VARCHAR
        )
    """)
    con.execute("""
        CREATE TABLE Dim_Kostenstelle (
            kostenstelle_key INTEGER PRIMARY KEY,
            kostenstelle_id VARCHAR,
            kostenstelle_name VARCHAR,
            bereich VARCHAR
        )
    """)
    con.execute("""
        CREATE TABLE Dim_Konto (
            konto_key INTEGER PRIMARY KEY,
            konto_id VARCHAR,
            konto_name VARCHAR,
            konto_art VARCHAR
        )
    """)
    con.execute("""
        CREATE TABLE Fact_Buchungen (
            buchung_id INTEGER PRIMARY KEY,
            date_key INTEGER REFERENCES Dim_Zeit(date_key),
            kostenstelle_key INTEGER REFERENCES Dim_Kostenstelle(kostenstelle_key),
            konto_key INTEGER REFERENCES Dim_Konto(konto_key),
            budget_eur DECIMAL(14,2),
            ist_eur DECIMAL(14,2)
        )
    """)

    zeit_map = build_dim_zeit(con)
    kst_map = build_dim_kostenstelle(con)
    konto_map = build_dim_konto(con)
    n_fact = build_fact_buchungen(con, zeit_map, kst_map, konto_map, seed=seed)

    con.close()
    print(f"Wrote {db_path}: {n_fact} fact rows, "
          f"{len(zeit_map)} time periods, {len(kst_map)} cost centers, {len(konto_map)} accounts.")
    return db_path


if __name__ == "__main__":
    build()
