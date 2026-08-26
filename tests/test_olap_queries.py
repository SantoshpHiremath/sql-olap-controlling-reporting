"""
Independent verification of the star schema and OLAP queries.

Each test re-derives the expected result independently (via pandas on
the raw generated data, not by trusting the SQL) and compares it
against what the actual SQL query returns from the real DuckDB engine.
This is the same "don't trust the tool's own output, re-derive it a
different way" pattern used to verify the DAX measures in
powerbi-dax-semantic-model.
"""
import os
import sys

import duckdb
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from build_star_schema import build, COST_CENTERS, ACCOUNTS  # noqa: E402


@pytest.fixture(scope="module")
def db_and_reference(tmp_path_factory):
    tmp_dir = tmp_path_factory.mktemp("olap")
    db_path = str(tmp_dir / "controlling_olap.duckdb")
    build(db_path=db_path, seed=42)
    con = duckdb.connect(db_path, read_only=True)

    # Independent reference: pull the star schema back out into a flat
    # pandas frame via plain SELECTs (not the OLAP queries under test),
    # so the "ground truth" doesn't share logic with what's being tested.
    fact = con.execute("SELECT * FROM Fact_Buchungen").fetchdf()
    dim_kst = con.execute("SELECT * FROM Dim_Kostenstelle").fetchdf()
    dim_konto = con.execute("SELECT * FROM Dim_Konto").fetchdf()
    dim_zeit = con.execute("SELECT * FROM Dim_Zeit").fetchdf()

    flat = (fact
            .merge(dim_kst, on="kostenstelle_key")
            .merge(dim_konto, on="konto_key")
            .merge(dim_zeit, on="date_key"))

    yield con, flat
    con.close()


def test_star_schema_row_counts(db_and_reference):
    con, flat = db_and_reference
    assert len(flat) == 24 * len(COST_CENTERS) * len(ACCOUNTS)
    assert con.execute("SELECT COUNT(*) FROM Dim_Kostenstelle").fetchone()[0] == len(COST_CENTERS)
    assert con.execute("SELECT COUNT(*) FROM Dim_Konto").fetchone()[0] == len(ACCOUNTS)
    assert con.execute("SELECT COUNT(*) FROM Dim_Zeit").fetchone()[0] == 24


def test_foreign_keys_all_resolve(db_and_reference):
    """Every fact row must join to exactly one row in each dimension --
    proves there are no orphaned surrogate keys."""
    con, _ = db_and_reference
    orphans_kst = con.execute("""
        SELECT COUNT(*) FROM Fact_Buchungen f
        LEFT JOIN Dim_Kostenstelle k ON f.kostenstelle_key = k.kostenstelle_key
        WHERE k.kostenstelle_key IS NULL
    """).fetchone()[0]
    orphans_konto = con.execute("""
        SELECT COUNT(*) FROM Fact_Buchungen f
        LEFT JOIN Dim_Konto ko ON f.konto_key = ko.konto_key
        WHERE ko.konto_key IS NULL
    """).fetchone()[0]
    orphans_zeit = con.execute("""
        SELECT COUNT(*) FROM Fact_Buchungen f
        LEFT JOIN Dim_Zeit z ON f.date_key = z.date_key
        WHERE z.date_key IS NULL
    """).fetchone()[0]
    assert orphans_kst == 0
    assert orphans_konto == 0
    assert orphans_zeit == 0


def test_rollup_subtotals_match_independent_groupby(db_and_reference):
    con, flat = db_and_reference
    result = con.execute("""
        SELECT k.bereich, SUM(f.ist_eur) AS ist_eur
        FROM Fact_Buchungen f
        JOIN Dim_Kostenstelle k ON f.kostenstelle_key = k.kostenstelle_key
        GROUP BY ROLLUP (k.bereich)
        ORDER BY k.bereich NULLS LAST
    """).fetchdf()

    expected_by_bereich = flat.groupby("bereich")["ist_eur"].sum()
    expected_total = flat["ist_eur"].sum()

    subtotal_rows = result[result["bereich"].notna()]
    for _, row in subtotal_rows.iterrows():
        assert row["ist_eur"] == pytest.approx(expected_by_bereich[row["bereich"]], abs=0.01)

    grand_total_row = result[result["bereich"].isna()]
    assert len(grand_total_row) == 1
    assert grand_total_row.iloc[0]["ist_eur"] == pytest.approx(expected_total, abs=0.01)


def test_cube_grand_total_matches_full_sum(db_and_reference):
    con, flat = db_and_reference
    result = con.execute("""
        SELECT k.bereich, z.quartal_label, SUM(f.ist_eur) AS ist_eur
        FROM Fact_Buchungen f
        JOIN Dim_Kostenstelle k ON f.kostenstelle_key = k.kostenstelle_key
        JOIN Dim_Zeit z ON f.date_key = z.date_key
        GROUP BY CUBE (k.bereich, z.quartal_label)
    """).fetchdf()

    grand_total = result[result["bereich"].isna() & result["quartal_label"].isna()]
    assert len(grand_total) == 1
    assert grand_total.iloc[0]["ist_eur"] == pytest.approx(flat["ist_eur"].sum(), abs=0.01)

    # A CUBE must have 4 grains: (bereich,quartal), (bereich,-), (-,quartal), (-,-)
    n_bereich = flat["bereich"].nunique()
    n_quartal = flat["quartal_label"].nunique()
    full_grain = result[result["bereich"].notna() & result["quartal_label"].notna()]
    bereich_only = result[result["bereich"].notna() & result["quartal_label"].isna()]
    quartal_only = result[result["bereich"].isna() & result["quartal_label"].notna()]
    assert len(full_grain) == n_bereich * n_quartal
    assert len(bereich_only) == n_bereich
    assert len(quartal_only) == n_quartal


def test_window_function_ytd_matches_cumulative_sum(db_and_reference):
    con, flat = db_and_reference
    result = con.execute("""
        SELECT
            k.kostenstelle_name, z.jahr, z.monat,
            SUM(f.ist_eur) AS monats_ist_eur,
            SUM(SUM(f.ist_eur)) OVER (
                PARTITION BY k.kostenstelle_name, z.jahr ORDER BY z.monat
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
            ) AS ytd_ist_eur
        FROM Fact_Buchungen f
        JOIN Dim_Kostenstelle k ON f.kostenstelle_key = k.kostenstelle_key
        JOIN Dim_Zeit z ON f.date_key = z.date_key
        GROUP BY k.kostenstelle_name, z.jahr, z.monat
    """).fetchdf()

    # Independent check for one cost center / year: December YTD must
    # equal the sum of all 12 months' actuals for that cost center/year.
    cc_name = "Produktion Backwaren"
    dec_row = result[(result["kostenstelle_name"] == cc_name)
                      & (result["jahr"] == 2026) & (result["monat"] == 12)]
    assert len(dec_row) == 1

    expected_year_total = flat[(flat["kostenstelle_name"] == cc_name)
                                 & (flat["jahr"] == 2026)]["ist_eur"].sum()
    assert dec_row.iloc[0]["ytd_ist_eur"] == pytest.approx(expected_year_total, abs=0.01)


def test_rank_partition_resets_per_quarter(db_and_reference):
    con, _ = db_and_reference
    result = con.execute("""
        SELECT * FROM (
            SELECT
                z.quartal_label, k.kostenstelle_name, ko.konto_name,
                SUM(f.ist_eur) - SUM(f.budget_eur) AS abweichung_eur,
                RANK() OVER (
                    PARTITION BY z.quartal_label
                    ORDER BY ABS(SUM(f.ist_eur) - SUM(f.budget_eur)) DESC
                ) AS rang
            FROM Fact_Buchungen f
            JOIN Dim_Kostenstelle k ON f.kostenstelle_key = k.kostenstelle_key
            JOIN Dim_Konto ko ON f.konto_key = ko.konto_key
            JOIN Dim_Zeit z ON f.date_key = z.date_key
            GROUP BY z.quartal_label, k.kostenstelle_name, ko.konto_name
        ) ranked
        WHERE rang = 1
    """).fetchdf()

    # Every quarter (8 total: 2025 Q1-Q4, 2026 Q1-Q4) must have exactly
    # one rank-1 row -- proves the partition genuinely resets per quarter
    # rather than ranking globally.
    assert result["quartal_label"].nunique() == 8
    assert len(result) == 8


def test_yoy_pivot_matches_independent_groupby(db_and_reference):
    con, flat = db_and_reference
    result = con.execute("""
        SELECT
            k.kostenstelle_name,
            SUM(CASE WHEN z.jahr = 2025 THEN f.ist_eur ELSE 0 END) AS ist_2025_eur,
            SUM(CASE WHEN z.jahr = 2026 THEN f.ist_eur ELSE 0 END) AS ist_2026_eur
        FROM Fact_Buchungen f
        JOIN Dim_Kostenstelle k ON f.kostenstelle_key = k.kostenstelle_key
        JOIN Dim_Zeit z ON f.date_key = z.date_key
        GROUP BY k.kostenstelle_name
    """).fetchdf().set_index("kostenstelle_name")

    expected = flat.groupby(["kostenstelle_name", "jahr"])["ist_eur"].sum().unstack("jahr")

    for cc_name in expected.index:
        assert result.loc[cc_name, "ist_2025_eur"] == pytest.approx(expected.loc[cc_name, 2025], abs=0.01)
        assert result.loc[cc_name, "ist_2026_eur"] == pytest.approx(expected.loc[cc_name, 2026], abs=0.01)


def test_logistik_fuel_overrun_signal_visible_only_from_april_2026(db_and_reference):
    """The injected sustained fuel-cost overrun (Logistik/Fuhrpark &
    Logistik account, from 2026-04 onward) must show a real positive
    deviation from April 2026 but not before -- proves the OLAP query
    surfaces genuine time-bound signal, not an artifact of the query
    itself."""
    con, _ = db_and_reference
    result = con.execute("""
        SELECT z.periode, SUM(f.ist_eur) - SUM(f.budget_eur) AS abweichung_eur
        FROM Fact_Buchungen f
        JOIN Dim_Kostenstelle k ON f.kostenstelle_key = k.kostenstelle_key
        JOIN Dim_Konto ko ON f.konto_key = ko.konto_key
        JOIN Dim_Zeit z ON f.date_key = z.date_key
        WHERE k.kostenstelle_id = '3000' AND ko.konto_id = '64000'
        GROUP BY z.periode
        ORDER BY z.periode
    """).fetchdf()

    before = result[result["periode"] < "2026-04"]["abweichung_eur"]
    after = result[result["periode"] >= "2026-04"]["abweichung_eur"]

    # After the overrun starts, deviation should be consistently and
    # substantially positive; before, it should be small/mixed noise.
    assert (after > 500).mean() > 0.8, "Expected the fuel overrun to show up as a sustained positive deviation"
    assert before.abs().mean() < after.mean(), "Pre-overrun deviation should be much smaller than post-overrun"
