# Contributing

Thank you for your help. Keep changes small and focused.

## Rules that do not change

1. **The LLM never calculates and never gives nutrition values.** It only reads the text
   (ingredients, quantity as written, state, assumptions) and chooses one candidate from
   a list. All arithmetic is in `backend/macro_translator/calc.py`.
2. **No data in git.** Do not commit databases or dumps (`*.duckdb`, `*.parquet`,
   `*.csv.gz`, `*.zip`, `data/`). CI fails if you do.
3. **The attribution in the footer stays.** See [DATA_LICENSES.md](DATA_LICENSES.md).

## Set up

You need [uv](https://docs.astral.sh/uv/) and Python 3.12.

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

The tests use small fixtures and a fake LLM. They do not need a model or the full data.

## Change a prompt

The prompts are in `backend/prompts/*.md`. The app reads them on each request, so you can
edit them while the app runs.

1. Try the change from the terminal:

   ```bash
   LLM_BASE_URL=http://127.0.0.1:8080/v1 LLM_MODEL=your-model \
     uv run python scripts/cli.py "huevos cocidos, 2, 130g. 10 ml de aceite"
   ```

2. Run the acceptance tests against a real model:

   ```bash
   LIVE_LLM=1 LLM_BASE_URL=http://127.0.0.1:8080/v1 LLM_MODEL=your-model \
     uv run pytest tests/test_live_llm.py
   ```

3. Write in the pull request which models you used and the result.

## Change the ETL

- `etl/build_db.py` is the documented method that makes the derivative database (ODbL).
  Write a short comment for each new rule.
- Inspect the real schema before you change a query. Do not guess column names.
- If the tests need new rows, add them to `tests/fixtures/make_fixtures.py` and run it.

## Pull requests

- One topic for each pull request.
- Add or update a test for each behaviour change.
- CI must pass: lint, tests, Docker build.
