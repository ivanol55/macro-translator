# Data licences

The **code** in this repository is under the MIT licence (see [LICENSE](LICENSE)).
The **data** has other licences. This repository does **not** contain or distribute the
generated database or the downloaded dumps. Each user builds the database with the ETL
(`etl/build_db.py`) from the official sources. The only exception is the small test
fixture in `tests/fixtures` (see the end of this file).

## Open Food Facts

- Source: the official Parquet dump on Hugging Face,
  <https://huggingface.co/datasets/openfoodfacts/product-database>.
  The ETL uses only this dump. It does not use the Open Food Facts API for bulk downloads.
- The database is under the **Open Database License (ODbL) 1.0**:
  <https://opendatacommons.org/licenses/odbl/1.0/>.
- The individual contents are under the **Database Contents License (DbCL) 1.0**:
  <https://opendatacommons.org/licenses/dbcl/1.0/>.
- Terms of use: <https://world.openfoodfacts.org/terms-of-use>.
- Attribution guidelines: <https://wiki.openfoodfacts.org/ODBL_License> (section
  "Attribution guidelines").

The database that the ETL makes (`macros.duckdb`) is a **derivative database** of Open Food
Facts. It is therefore under the **ODbL 1.0**, as the licence requires.

### How this project follows the ODbL

1. **Attribution.** The footer of the app always shows this text, with links to
   openfoodfacts.org and to the licence:
   "Contiene información de Open Food Facts, disponible bajo la Open Database License (ODbL)".
   No configuration value removes it. Each row from Open Food Facts also links to the
   product page on openfoodfacts.org.
2. **Share-alike and the method.** The ETL script in this repository is the complete
   method that makes the derivative database from the original dump: download, country
   filter, columns, cleaning rules, de-duplication and the search index. ODbL section 4.6
   lets you offer "the method of making the Derivative Database" instead of the database.
   The public repository gives that method to anyone.
3. **Public instances.** If you run an instance that other people can open (for example
   on the internet), you must:
   - keep the attribution in the footer visible, and
   - keep the link to this repository (or to your fork with the ETL) in the footer, so that
     users can get the method that makes the database. If you change the ETL, publish
     your changes.

## USDA FoodData Central

- Source: the "Foundation Foods" and "SR Legacy" CSV downloads from
  <https://fdc.nal.usda.gov/download-datasets>. No API key is necessary.
- Licence: **public domain**. USDA publishes FoodData Central under
  **CC0 1.0 Universal**: <https://creativecommons.org/publicdomain/zero/1.0/>.
  USDA asks users to name FoodData Central as the source.
- The app footer shows "Datos de USDA FoodData Central" with a link to
  <https://fdc.nal.usda.gov/>, and each USDA row links to its food page.

Suggested citation (from USDA):

> U.S. Department of Agriculture, Agricultural Research Service, Beltsville Human Nutrition
> Research Center. FoodData Central. [Internet]. [cited (enter date)]. Available from
> https://fdc.nal.usda.gov/.

Data type citations:

> U.S. Department of Agriculture, Agricultural Research Service. FoodData Central:
> Foundation Foods. Version Current: April 2026. Internet: fdc.nal.usda.gov.
>
> U.S. Department of Agriculture, Agricultural Research Service. FoodData Central:
> SR Legacy, 2018. Internet: fdc.nal.usda.gov.

## Test fixtures

The tests do not download data. They use a small fixture:

- `tests/fixtures/off_sample.jsonl`: 24 real Open Food Facts products (some columns only),
  sold in Spain or near the test cases. **Licence: ODbL 1.0 (database) and DbCL 1.0
  (contents). Source: Open Food Facts, <https://world.openfoodfacts.org>.** Each row keeps
  its barcode, so you can find the product at
  `https://world.openfoodfacts.org/product/<barcode>`.
- `tests/fixtures/usda/`: 18 real foods from FoodData Central Foundation Foods (April 2026)
  and SR Legacy (April 2018), with only the nutrients the app uses.
  **Licence: CC0 1.0. Source: USDA FoodData Central.**

`tests/fixtures/make_fixtures.py` makes these files from the full dumps and lists each row
and why the tests need it.

## What CI checks

`scripts/check_no_data_files.sh` runs in CI. It fails when git tracks a `*.duckdb`,
`*.parquet`, `*.csv.gz` or `*.zip` file, anything in `data/`, or `.env`.
