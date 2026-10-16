#!/usr/bin/env python3
"""Make the small test fixtures from the real dumps in data/cache.

The fixtures are a few dozen real rows from each source. The tests use them
instead of the full downloads. Run this script only to change the fixtures:

    python tests/fixtures/make_fixtures.py --cache data/cache

Open Food Facts rows: ODbL 1.0 / DbCL 1.0, see DATA_LICENSES.md.
USDA FoodData Central rows: public domain (CC0 1.0).
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent

# Open Food Facts barcodes, chosen for the tests.
OFF_CODES = {
    # The reference case and the foods near it
    "2324200006036": "Sepia Faraónica Limpia (Hacendado), 78/18/0.8/0.5",
    "8480000242747": "Sepia troceada (Hacendado)",
    "8480000310101": "Huevos cocidos (Hacendado)",
    "8411384313037": "Huevos cocidos (Huevos Guillén)",
    "8437004036841": "Huevos cocidos camperos",
    "8437004577382": "Huevas cocidas de merluza: same stem as huevos, other food",
    "8437004577399": "Huevas cocidas de bacalao",
    "8480000047403": "Aceite de oliva virgen extra (Hacendado), per 100 ml",
    "8480000047182": "Aceite de oliva virgen extra (Hacendado), 200 ml",
    "8410010006206": "Aceite de oliva virgen extra (Carbonell)",
    # Words that match, food that does not
    "8420720007011": "Unicornio (La Ibense): a sweet",
    "7798188380160": "La barrita del unicornio",
    "8429815002290": "Galvi Helado unicornio",
    # Branded and generic products
    "647300900720002558": "Pechuga de pollo a la plancha (BonÀrea)",
    "8480000211187": "GRIEGO (Hacendado)",
    "8480000209047": "Yogur griego stracciatella (Hacendado)",
    "8480000210906": "Yogurt Griego Limón y Jengibre (Hacendado)",
    "8480000399038": "Salmorejo (Hacendado)",
    "8480000399014": "Salmorejo fresco (MERCADONA)",
    # Rows that the cleaning must remove
    "0696859027390": "Avocado oil: 928 kcal per 100 g (> 900)",
    "8410762220059": "pizza jamón y queso: kcal and macros do not match",
    "8427889251132": "Huevos camperos: no fat value",
    "647302400720002902": "Pechuga de pollo a la plancha: 888 kcal typo, incoherent",
    # Not sold in Spain: the country filter must remove it
    "6111099000254": "Huile d'olive (Morocco/France)",
}
OFF_NUTRIENTS = ("energy-kcal", "proteins", "carbohydrates", "fat", "energy")

# USDA FDC descriptions, chosen for the tests: (release folder, description)
USDA_FOODS = [
    ("sr_legacy", "Egg, whole, cooked, hard-boiled"),
    ("sr_legacy", "Egg, whole, cooked, fried"),
    ("sr_legacy", "Egg, whole, cooked, poached"),
    ("sr_legacy", "Egg, whole, raw, fresh"),
    ("sr_legacy", "Oil, olive, salad or cooking"),
    ("sr_legacy", "Oil, corn, peanut, and olive"),
    ("sr_legacy", "Mollusks, cuttlefish, mixed species, raw"),
    ("sr_legacy", "Mollusks, cuttlefish, mixed species, cooked, moist heat"),
    ("sr_legacy", "Chicken, broilers or fryers, breast, meat only, cooked, roasted"),
    ("sr_legacy", "Chicken breast tenders, breaded, cooked, microwaved"),
    ("sr_legacy", "Yogurt, Greek, plain, lowfat"),
    ("sr_legacy", "Candies, hard"),
    ("sr_legacy", "Kielbasa, fully cooked, grilled"),
    ("sr_legacy", "Alcoholic beverage, beer, regular, all"),  # incoherent without alcohol
    ("foundation", "Eggs, Grade A, Large, egg whole"),
    ("foundation", "Eggplant, raw"),  # energy only as Atwater (2047/2048)
    ("foundation", "Oil, olive, extra virgin"),  # no macros: the ETL drops it
    (
        "sr_legacy",
        "Chicken, broiler or fryers, breast, skinless, boneless, meat only, cooked, grilled",
    ),
]
USDA_NUTRIENT_IDS = (1003, 1004, 1005, 1008, 2047, 2048)


def make_off(con: duckdb.DuckDBPyConnection, parquet: Path) -> None:
    codes = ", ".join(f"'{c}'" for c in OFF_CODES)
    names = ", ".join(f"'{n}'" for n in OFF_NUTRIENTS)
    rows = con.execute(f"""
        SELECT code, product_name, brands, countries_tags, obsolete, unique_scans_n,
               list_filter(nutriments, lambda x: list_contains([{names}], x.name)) AS nutriments
        FROM read_parquet('{parquet}')
        WHERE code IN ({codes})
        ORDER BY code
    """).fetchall()
    cols = [
        "code",
        "product_name",
        "brands",
        "countries_tags",
        "obsolete",
        "unique_scans_n",
        "nutriments",
    ]
    out = HERE / "off_sample.jsonl"
    with out.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(dict(zip(cols, r, strict=True)), ensure_ascii=False) + "\n")
    missing = set(OFF_CODES) - {r[0] for r in rows}
    print(f"{out}: {len(rows)} rows" + (f", missing {sorted(missing)}" if missing else ""))


def make_usda(con: duckdb.DuckDBPyConnection, cache: Path) -> None:
    folders = {
        "sr_legacy": next(cache.glob("FoodData_Central_sr_legacy_food_csv_*_csv")),
        "foundation": next(cache.glob("FoodData_Central_foundation_food_csv_*_csv")),
    }
    for key, folder in folders.items():
        wanted = [d for k, d in USDA_FOODS if k == key]
        dest = HERE / "usda" / key
        dest.mkdir(parents=True, exist_ok=True)
        food = con.execute(
            "SELECT * FROM read_csv(?, header = true, all_varchar = true) "
            "WHERE description IN (SELECT unnest(?)) AND data_type IN "
            "('sr_legacy_food', 'foundation_food') ORDER BY fdc_id",
            [str(folder / "food.csv"), wanted],
        )
        _write(dest / "food.csv", food)
        ids = [
            r[0]
            for r in con.execute(
                "SELECT fdc_id FROM read_csv(?, header = true, all_varchar = true) "
                "WHERE description IN (SELECT unnest(?)) AND data_type IN "
                "('sr_legacy_food', 'foundation_food')",
                [str(folder / "food.csv"), wanted],
            ).fetchall()
        ]
        fn = con.execute(
            "SELECT * FROM read_csv(?, header = true, all_varchar = true) "
            "WHERE fdc_id IN (SELECT unnest(?)) AND CAST(nutrient_id AS INTEGER) IN (SELECT "
            "unnest(?)) ORDER BY fdc_id, nutrient_id",
            [str(folder / "food_nutrient.csv"), ids, list(USDA_NUTRIENT_IDS)],
        )
        _write(dest / "food_nutrient.csv", fn)
        nut = con.execute(
            "SELECT * FROM read_csv(?, header = true, all_varchar = true) "
            "WHERE CAST(id AS INTEGER) IN (SELECT unnest(?)) ORDER BY id",
            [str(folder / "nutrient.csv"), list(USDA_NUTRIENT_IDS)],
        )
        _write(dest / "nutrient.csv", nut)
        print(f"{dest}: {len(ids)} foods (wanted {len(wanted)})")


def _write(path: Path, rel: duckdb.DuckDBPyConnection) -> None:
    cols = [d[0] for d in rel.description]
    rows = rel.fetchall()
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_ALL)
        w.writerow(cols)
        w.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", default="data/cache")
    cache = Path(parser.parse_args().cache)
    con = duckdb.connect()
    make_off(con, cache / "off_food.parquet")
    make_usda(con, cache)


if __name__ == "__main__":
    main()
