-- OLAP-style controlling reporting queries against the star schema.
-- Syntax used here (GROUP BY ROLLUP / CUBE / GROUPING SETS, window
-- functions) is T-SQL-compatible SQL, i.e. the same constructs used
-- against a real Microsoft SQL Server / SQL Server Analysis Services
-- OLAP setup -- see README for the honest disclosure on the DuckDB
-- substitution.

-- =====================================================================
-- Query 1: ROLLUP -- Budget vs. Ist by Bereich -> Kostenstelle, with
-- subtotal-per-Bereich and grand-total rows automatically generated.
-- This is the classic OLAP "drill up/down a hierarchy" query.
-- =====================================================================
SELECT
    COALESCE(k.bereich, 'GESAMT') AS bereich,
    COALESCE(k.kostenstelle_name, '') AS kostenstelle,
    SUM(f.budget_eur) AS budget_eur,
    SUM(f.ist_eur) AS ist_eur,
    SUM(f.ist_eur) - SUM(f.budget_eur) AS abweichung_eur,
    ROUND(100.0 * (SUM(f.ist_eur) - SUM(f.budget_eur)) / NULLIF(SUM(f.budget_eur), 0), 2) AS abweichung_pct
FROM Fact_Buchungen f
JOIN Dim_Kostenstelle k ON f.kostenstelle_key = k.kostenstelle_key
GROUP BY ROLLUP (k.bereich, k.kostenstelle_name)
ORDER BY k.bereich NULLS LAST, k.kostenstelle_name NULLS LAST;


-- =====================================================================
-- Query 2: CUBE -- every combination of Bereich x Quartal, including
-- Bereich-only, Quartal-only, and grand-total rows in a single result
-- set (a full OLAP cube slice).
-- =====================================================================
SELECT
    COALESCE(k.bereich, 'ALLE BEREICHE') AS bereich,
    COALESCE(z.quartal_label, 'ALLE QUARTALE') AS quartal,
    SUM(f.ist_eur) AS ist_eur
FROM Fact_Buchungen f
JOIN Dim_Kostenstelle k ON f.kostenstelle_key = k.kostenstelle_key
JOIN Dim_Zeit z ON f.date_key = z.date_key
GROUP BY CUBE (k.bereich, z.quartal_label)
ORDER BY bereich, quartal;


-- =====================================================================
-- Query 3: GROUPING SETS -- an ad-hoc report combining two different
-- aggregation grains in one query: totals by Kostenstelle AND totals
-- by Konto, without the cross-product CUBE would produce.
-- =====================================================================
SELECT
    k.kostenstelle_name,
    ko.konto_name,
    SUM(f.ist_eur) AS ist_eur
FROM Fact_Buchungen f
JOIN Dim_Kostenstelle k ON f.kostenstelle_key = k.kostenstelle_key
JOIN Dim_Konto ko ON f.konto_key = ko.konto_key
GROUP BY GROUPING SETS ((k.kostenstelle_name), (ko.konto_name))
ORDER BY k.kostenstelle_name NULLS LAST, ko.konto_name NULLS LAST;


-- =====================================================================
-- Query 4: Window functions -- monthly Ist total per Kostenstelle with
-- a running (year-to-date) total and a month-over-month delta, the
-- same pattern as Power BI's SAMEPERIODLASTYEAR/DATESYTD but expressed
-- in plain SQL window functions.
-- =====================================================================
SELECT
    k.kostenstelle_name,
    z.periode,
    SUM(f.ist_eur) AS monats_ist_eur,
    SUM(SUM(f.ist_eur)) OVER (
        PARTITION BY k.kostenstelle_name, z.jahr
        ORDER BY z.monat
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS ytd_ist_eur,
    SUM(f.ist_eur) - LAG(SUM(f.ist_eur)) OVER (
        PARTITION BY k.kostenstelle_name
        ORDER BY z.jahr, z.monat
    ) AS delta_zum_vormonat_eur
FROM Fact_Buchungen f
JOIN Dim_Kostenstelle k ON f.kostenstelle_key = k.kostenstelle_key
JOIN Dim_Zeit z ON f.date_key = z.date_key
GROUP BY k.kostenstelle_name, z.periode, z.jahr, z.monat
ORDER BY k.kostenstelle_name, z.jahr, z.monat;


-- =====================================================================
-- Query 5: RANK() -- top cost-center/account combinations by absolute
-- budget deviation per quarter (drill-down exception report, ranked
-- within each quarter partition).
-- =====================================================================
SELECT *
FROM (
    SELECT
        z.quartal_label,
        k.kostenstelle_name,
        ko.konto_name,
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
WHERE rang <= 3
ORDER BY quartal_label, rang;


-- =====================================================================
-- Query 6: YoY comparison per Kostenstelle (2025 vs 2026), the SQL
-- equivalent of the Excel-workbook YoY-Vergleich sheet, expressed as a
-- pivoted (year-as-columns) report using conditional aggregation.
-- =====================================================================
SELECT
    k.kostenstelle_name,
    SUM(CASE WHEN z.jahr = 2025 THEN f.ist_eur ELSE 0 END) AS ist_2025_eur,
    SUM(CASE WHEN z.jahr = 2026 THEN f.ist_eur ELSE 0 END) AS ist_2026_eur,
    SUM(CASE WHEN z.jahr = 2026 THEN f.ist_eur ELSE 0 END)
        - SUM(CASE WHEN z.jahr = 2025 THEN f.ist_eur ELSE 0 END) AS veraenderung_eur
FROM Fact_Buchungen f
JOIN Dim_Kostenstelle k ON f.kostenstelle_key = k.kostenstelle_key
JOIN Dim_Zeit z ON f.date_key = z.date_key
GROUP BY k.kostenstelle_name
ORDER BY k.kostenstelle_name;
