from __future__ import annotations

import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).parents[1]


def test_legacy_package_imports_do_not_preload_optional_pipelines() -> None:
    script = """
import importlib
import sys

for package in (
    "app.crawlers",
    "app.llm",
    "app.models",
    "app.repositories",
    "app.services",
    "app.workers",
):
    importlib.import_module(package)

optional_modules = {
    "app.crawlers.creator_platforms.douyin",
    "app.crawlers.ths_market_review_crawler",
    "app.crawlers.ths_morning_report_crawler",
    "app.llm.creator_content_analysis_llm",
    "app.llm.creator_opinion_verification_llm",
    "app.llm.morning_analysis_llm",
    "app.models.daily_market_analysis",
    "app.models.creator_monitoring",
    "app.models.news_ranking_snapshot",
    "app.repositories.daily_market_analysis_repository",
    "app.repositories.creator_monitoring_repository",
    "app.repositories.news_ranking_snapshot_repository",
    "app.services.creator_content_extraction_service",
    "app.services.creator_ingestion_service",
    "app.services.creator_opinion_analysis_service",
    "app.services.creator_opinion_verification_service",
    "app.services.morning_analysis_service",
    "app.services.news_ranking_service",
    "app.services.news_ranking_snapshot_service",
    "app.services.trading_calendar_service",
    "app.workers.creator_content_extraction_worker",
    "app.workers.creator_opinion_analysis_worker",
}
unexpected = sorted(optional_modules.intersection(sys.modules))
if unexpected:
    raise AssertionError(f"optional modules were preloaded: {unexpected}")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
