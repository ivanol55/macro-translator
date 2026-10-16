"""Repository rules: no data dumps or databases in git, prompts in files."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_gitignore_excludes_data_files():
    ignore = (ROOT / ".gitignore").read_text()
    for pattern in ("*.duckdb", "*.parquet", "*.csv.gz", "*.zip", "data/", ".env"):
        assert pattern in ignore.splitlines(), pattern


def test_no_data_files_are_tracked():
    result = subprocess.run(
        ["sh", str(ROOT / "scripts" / "check_no_data_files.sh")],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_prompts_live_in_files():
    prompts = ROOT / "backend" / "prompts"
    assert {p.name for p in prompts.glob("*.md")} >= {"parse.md", "select.md", "retry.md"}
    parse = (prompts / "parse.md").read_text()
    for rule in ("130 g en total", "cada uno", "chorrito", "valores_usuario_100g", "supuestos"):
        assert rule in parse, rule


def test_env_example_has_no_secret():
    for line in (ROOT / ".env.example").read_text().splitlines():
        if line.startswith("LLM_API_KEY="):
            assert line == "LLM_API_KEY="
