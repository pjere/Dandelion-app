"""A backtest needs French demand, and ENTSO-E cannot supply it.

`io/fr_history.py` reads `conso_realised` from `master_hourly`, which `build_master` fills
from RTE. The ENTSO-E fallback covers `prod_*` generation only — deliberately, because the
two sources disagree on the consumption leg by construction. So an install with a complete
ENTSO-E history still cannot back-test, and finding that out ten minutes into a run is
exactly the failure this check exists to prevent.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from drivers.preflight import FR_HISTORY_COLUMNS, check_fr_history  # noqa: E402


def make_master(path: Path, year: int, hours: int, *, table: bool = True) -> Path:
    """A master with the FULL column set, so the hour-count tests isolate the hour count."""
    con = sqlite3.connect(path)
    if table:
        cols = ", ".join(f"{c} REAL" for c in FR_HISTORY_COLUMNS)
        con.execute(f"CREATE TABLE master_hourly (ts_utc TEXT, {cols})")
        holes = ",".join("?" * (len(FR_HISTORY_COLUMNS) + 1))
        con.executemany(
            f"INSERT INTO master_hourly VALUES ({holes})",
            [(f"{year}-01-01T{h % 24:02d}:00:00+00:00",
              *([50000.0] * len(FR_HISTORY_COLUMNS))) for h in range(hours)],
        )
    else:
        con.execute("CREATE TABLE ingest_log (source TEXT)")
    con.commit()
    con.close()
    return path


def test_a_full_year_passes(tmp_path):
    result = check_fr_history(make_master(tmp_path / "m.db", 2019, 8760), 2019)
    assert result.ok and "8,760" in result.reason


def test_a_missing_database_names_the_account_that_fills_it(tmp_path):
    result = check_fr_history(tmp_path / "absent.db", 2019)
    assert not result.ok
    assert "RTE" in result.reason
    assert result.remedy_job == "extract-rte"


def test_a_database_without_the_master_table_offers_the_merge(tmp_path):
    """Ingested-but-not-merged is a distinct state from never-downloaded, and has a
    different fix."""
    result = check_fr_history(make_master(tmp_path / "m.db", 2019, 0, table=False), 2019)
    assert not result.ok and result.remedy_job == "build-master"


def test_a_partial_year_is_refused(tmp_path):
    """The failure mode that matters: a backtest on 60% of a year reports nothing unusual."""
    result = check_fr_history(make_master(tmp_path / "m.db", 2019, 5000), 2019)
    assert not result.ok
    assert "5,000" in result.reason and "3,760 short" in result.reason


def test_a_year_that_is_almost_complete_still_passes(tmp_path):
    """Real data has the odd hole — 8,759 of 8,760 is the owner's actual 2019."""
    assert check_fr_history(make_master(tmp_path / "m.db", 2019, 8759), 2019).ok


def test_the_tolerance_is_absolute_not_proportional(tmp_path):
    """1% of a year is 88 hours. That would accept three missing days as a complete year,
    which is precisely the silent-partial-history failure this check exists to stop."""
    assert not check_fr_history(make_master(tmp_path / "m.db", 2019, 8760 - 72), 2019).ok
    assert check_fr_history(make_master(tmp_path / "n.db", 2019, 8760 - 12), 2019).ok


def test_a_leap_year_expects_more_hours(tmp_path):
    result = check_fr_history(make_master(tmp_path / "m.db", 2020, 8760), 2020)
    assert not result.ok and "8,784" in result.reason
    assert result.detail["missing"] == 24, "a whole missing Feb 29 must not slip through"


@pytest.mark.parametrize("year,leap", [(2000, True), (1900, False), (2024, True)])
def test_the_century_rule_is_applied(tmp_path, year, leap):
    result = check_fr_history(make_master(tmp_path / f"{year}.db", year, 8760), year)
    assert result.ok is not leap


def test_a_file_that_is_not_a_database_is_reported_not_raised(tmp_path):
    path = tmp_path / "not.db"
    path.write_text("definitely not sqlite", encoding="utf-8")
    result = check_fr_history(path, 2019)
    assert not result.ok and "could not be read" in result.reason


def test_the_check_never_creates_a_database(tmp_path):
    absent = tmp_path / "nothing.db"
    check_fr_history(absent, 2019)
    assert not absent.exists()


def test_an_entsoe_only_master_is_named_as_such(tmp_path):
    """Measured against the live install: with ENTSO-E ingested and no RTE, build_master
    emits master_hourly with price columns only. That is a meaningful state with a specific
    fix, not a corrupt database, and the message has to say which account is missing."""
    path = tmp_path / "m.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE master_hourly (ts_utc TEXT, price_da_fr REAL)")
    con.executemany("INSERT INTO master_hourly VALUES (?, ?)",
                    [(f"2019-01-01T{h:02d}:00:00+00:00", 40.0) for h in range(24)])
    con.commit()
    con.close()
    result = check_fr_history(path, 2019)
    assert not result.ok
    assert "RTE" in result.reason
    assert "could not be read" not in result.reason, "a missing column is not corruption"
    assert result.remedy_job == "extract-rte"


# ------------------------------------------------------ ENTSO-E stack-sizing inputs

from drivers.preflight import check_stack_inputs  # noqa: E402


def test_missing_installed_capacity_is_refused_not_warned(tmp_path):
    """io/entsoe_hist.py:117 swallows the missing table and returns {}, and the caller
    sizes stacks from a generation proxy that upstream measured at +22 EUR/MWh of bias.
    No shipped entry point fills the table, so the degraded state is the DEFAULT one."""
    path = tmp_path / "m.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE dim_production_unit (eic_code TEXT, fuel_type TEXT)")
    con.executemany("INSERT INTO dim_production_unit VALUES (?, ?)",
                    [(f"EIC{i}", "NUCLEAR") for i in range(50)])
    con.commit()
    con.close()
    result = check_stack_inputs(path, 2019)
    assert not result.ok
    assert result.remedy_job == "backfill-entsoe-extras"
    assert "22" in result.reason, "the measured cost belongs in the message"


def test_an_empty_capacity_table_counts_as_missing(tmp_path):
    """The table existing proves nothing — build_master creates tables it never fills."""
    path = tmp_path / "m.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE entsoe_installed_capacity (series_key TEXT, value REAL)")
    con.execute("CREATE TABLE dim_production_unit (eic_code TEXT, fuel_type TEXT)")
    con.executemany("INSERT INTO dim_production_unit VALUES (?, ?)",
                    [(f"EIC{i}", "NUCLEAR") for i in range(50)])
    con.commit()
    con.close()
    assert not check_stack_inputs(path, 2019).ok


def test_capacity_for_real_zones_passes(tmp_path):
    path = tmp_path / "m.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE entsoe_installed_capacity (series_key TEXT, value REAL)")
    con.executemany("INSERT INTO entsoe_installed_capacity VALUES (?, ?)",
                    [(z, 1000.0) for z in ("FR", "DE_LU", "BE", "CH", "ES", "GB", "IT_NORTH")])
    con.execute("CREATE TABLE dim_production_unit (eic_code TEXT, fuel_type TEXT)")
    con.executemany("INSERT INTO dim_production_unit VALUES (?, ?)",
                    [(f"EIC{i}", "NUCLEAR") for i in range(50)])
    con.commit()
    con.close()
    result = check_stack_inputs(path, 2019)
    assert result.ok and result.detail["zones"] == 7


def test_an_empty_unit_registry_is_refused(tmp_path):
    """io/fr_fleet.py:102 keeps only rows with a known fuel_type, so an empty
    dim_production_unit yields an EMPTY French fleet and a backtest with no power stations
    to dispatch. Nothing raises — the prices are just nonsense."""
    path = tmp_path / "m.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE entsoe_installed_capacity (series_key TEXT, value REAL)")
    con.execute("INSERT INTO entsoe_installed_capacity VALUES ('FR', 1000.0)")
    con.execute("CREATE TABLE dim_production_unit (eic_code TEXT, fuel_type TEXT)")
    con.commit()
    con.close()
    result = check_stack_inputs(path, 2019)
    assert not result.ok
    assert result.remedy_job == "reconcile-units"
    assert "no account" in result.reason, "this remedy needs no credentials — say so"


def test_a_complete_install_passes(tmp_path):
    path = tmp_path / "m.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE entsoe_installed_capacity (series_key TEXT, value REAL)")
    con.executemany("INSERT INTO entsoe_installed_capacity VALUES (?, ?)",
                    [(z, 1.0) for z in ("FR", "BE", "CH")])
    con.execute("CREATE TABLE dim_production_unit (eic_code TEXT, fuel_type TEXT)")
    con.executemany("INSERT INTO dim_production_unit VALUES (?, ?)",
                    [(f"EIC{i}", "NUCLEAR") for i in range(50)])
    con.commit()
    con.close()
    result = check_stack_inputs(path, 2019)
    assert result.ok and result.detail == {"zones": 3, "units": 50}


def test_a_master_missing_generation_columns_is_refused(tmp_path):
    """Measured: a master built from 2019 alone has no `prod_wind_offshore` column, because
    France commissioned none before 2022 and build_master PIVOTS whatever RTE reported. The
    2019 backtest died on exactly that. `conso_realised` being present proves nothing."""
    path = tmp_path / "m.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE master_hourly (ts_utc TEXT, conso_realised REAL, "
                "prod_solar REAL, prod_wind_onshore REAL)")
    con.executemany("INSERT INTO master_hourly VALUES (?,?,?,?)",
                    [(f"2019-01-01T{h:02d}:00:00+00:00", 5e4, 0.0, 0.0) for h in range(24)])
    con.commit()
    con.close()
    result = check_fr_history(path, 2019)
    assert not result.ok
    assert "prod_wind_offshore" in result.reason
    assert "2022" in result.reason, "explain WHY the column is absent"
    assert len(result.detail["missing"]) == 10, "name every missing column, not just one"


def test_a_complete_column_set_passes(tmp_path):
    from drivers.preflight import FR_HISTORY_COLUMNS

    path = tmp_path / "m.db"
    con = sqlite3.connect(path)
    cols = ", ".join(f"{c} REAL" for c in FR_HISTORY_COLUMNS)
    con.execute(f"CREATE TABLE master_hourly (ts_utc TEXT, {cols})")
    holes = ",".join("?" * (len(FR_HISTORY_COLUMNS) + 1))
    con.executemany(f"INSERT INTO master_hourly VALUES ({holes})",
                    [(f"2019-01-01T{h % 24:02d}:00:00+00:00",
                      *([1.0] * len(FR_HISTORY_COLUMNS))) for h in range(8760)])
    con.commit()
    con.close()
    assert check_fr_history(path, 2019).ok
