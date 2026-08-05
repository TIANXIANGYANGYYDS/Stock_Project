from datetime import date, datetime

from app.manually_execute_script.export_creator_verification_report import (
    build_argument_parser,
    build_report,
    render_markdown,
    validate_report_dates,
)
from app.models.creator_monitoring import CN_TZ


SOURCE_TIME = datetime(2026, 7, 23, 10, tzinfo=CN_TZ)
EVALUATION_TIME = datetime(2026, 7, 24, 18, tzinfo=CN_TZ)


def test_report_date_validation_accepts_all_monday_weekend_sources() -> None:
    """验证周一报告允许分别导出周五、周六和周日来源作品。"""

    for source_day in (24, 25, 26):
        validate_report_dates(date(2026, 7, source_day), date(2026, 7, 27))


def test_report_reads_unified_results_and_preserves_unscored_creators() -> None:
    """验证报告直接展示统一文档结论，并且不会给缺失文档的博主补零分。"""

    accounts = [
        {
            "rank": 1,
            "creator_id": "creator_a",
            "display_name": "博主甲",
            "platform": "douyin",
        },
        {
            "rank": 2,
            "creator_id": "creator_b",
            "display_name": "博主乙",
            "platform": "weibo",
        },
    ]
    works = [
        {
            "work_key": "douyin:work",
            "creator_id": "creator_a",
            "platform": "douyin",
            "title": "观点作品",
            "canonical_url": "https://example.com/work",
            "published_at": SOURCE_TIME,
        },
        {
            "work_key": "weibo:failed",
            "creator_id": "creator_b",
            "platform": "weibo",
            "title": "失败作品",
            "published_at": SOURCE_TIME,
        },
    ]
    verifications = [
        {
            "creator_id": "creator_a",
            "creator_name": "博主甲",
            "market_date": "2026-07-24",
            "status": "completed",
            "daily_score": 100,
            "rolling_score": 87.5,
            "daily_sample_count": 1,
            "sample_count": 8,
            "market_facts": {"target": {"return_pct": 2.4}},
            "opinion_results": [
                {
                    "opinion_id": "douyin:work:1",
                    "work_key": "douyin:work",
                    "source_published_at": SOURCE_TIME,
                    "target_type": "sector",
                    "target_name": "半导体",
                    "direction": "bullish",
                    "claim": "半导体次日上涨",
                    "metric": "板块涨跌幅",
                    "verdict": "corroborated",
                    "reason": "半导体板块收涨2.4%，与观点一致。",
                    "evidence_refs": ["facts.target.return_pct"],
                    "web_evidence": [],
                    "opinion_score": 1.0,
                }
            ],
        }
    ]

    report = build_report(
        source_date=date(2026, 7, 23),
        evaluation_date=date(2026, 7, 24),
        accounts=accounts,
        works=works,
        verifications=verifications,
        generated_at=EVALUATION_TIME,
    )

    assert report["summary"]["work_count"] == 2
    assert report["summary"]["opinion_count"] == 1
    assert report["ranking"][0]["creator_id"] == "creator_a"
    creator_b = next(
        item for item in report["creators"] if item["creator_id"] == "creator_b"
    )
    assert creator_b["verification_status"] == "missing"
    assert creator_b["daily_score"] is None
    markdown = render_markdown(report)
    assert "半导体次日上涨" in markdown
    assert "半导体板块收涨2.4%" in markdown
    assert "博主乙" in markdown


def test_report_cli_contains_no_legacy_snapshot_filter() -> None:
    """验证报告命令只暴露两表查询所需参数，不再接受旧快照筛选。"""

    args = build_argument_parser().parse_args([])

    assert not hasattr(args, "snapshot_id")
