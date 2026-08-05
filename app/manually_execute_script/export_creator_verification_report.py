from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
import json
from numbers import Real
from pathlib import Path
from typing import Any, Mapping, Sequence

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.core.config import PROJECT_ROOT, Settings
from app.crawlers.creator_platforms import get_enabled_accounts
from app.models.creator_monitoring import CN_TZ
from app.services.creator_daily_verification_service import current_source_window_bounds


DEFAULT_SOURCE_DATE = "2026-07-23"
DEFAULT_EVALUATION_DATE = "2026-07-24"
REPORT_VERSION = "creator_verification_report_v5"

VERDICT_LABELS = {
    "corroborated": "符合",
    "partially_corroborated": "部分符合",
    "minor_deviation": "轻微偏差",
    "contradicted": "不符合",
    "unverified": "无法验证",
    "not_triggered": "条件未触发",
}
DIRECTION_LABELS = {
    "bullish": "看多",
    "bearish": "看空",
    "neutral": "中性",
}


@dataclass(frozen=True)
class ReportOutputPaths:
    """保存一次报告导出的 Markdown 和 JSON 目标路径。"""

    # 供人工阅读的中文 Markdown 报告绝对路径。
    markdown: Path
    # 与 Markdown 内容对应、供程序继续处理的 JSON 报告绝对路径。
    json: Path


@dataclass(frozen=True)
class ReportExportResult:
    """记录报告覆盖日期、输出位置和关键文档数量。"""

    # 查询内部处理集合时使用的作品发布日期。
    source_date: str
    # 从博主唯一汇总文档筛选已验证观点时使用的结算日期。
    evaluation_date: str
    # 已生成 Markdown 报告的绝对路径。
    markdown_path: str
    # 已生成 JSON 报告的绝对路径。
    json_path: str
    # 来源日期内查询到的作品数量。
    work_count: int
    # 统一每日文档中包含的逐观点验证结果数量。
    opinion_count: int
    # 拥有有效滚动分并进入排行榜的博主数量。
    ranked_creator_count: int


def parse_date_text(value: str, *, field_name: str) -> date:
    """把命令行 ISO 日期转换为日期对象，并给非法输入附加字段名。"""

    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"{field_name} 必须是 YYYY-MM-DD 日期") from exc


def validate_report_dates(source_date: date, evaluation_date: date) -> None:
    """确认来源日期位于评价交易日对应的合法作品窗口内。

    普通交易日只接受前一日；周一或节后首个交易日允许上一交易日至开盘前全部
    休市日，规则与生产收盘验证服务完全一致。
    """

    window_start, window_end = current_source_window_bounds(evaluation_date)
    if not window_start.date() <= source_date < window_end.date():
        raise ValueError("source_date 不在 evaluation_date 的作品来源窗口内")


def normalize_datetime(value: Any) -> datetime | None:
    """把 MongoDB 时间或 ISO 文本规范为北京时间，无法解析时返回空值。"""

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=CN_TZ)
        return value.astimezone(CN_TZ)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=CN_TZ)
        return parsed.astimezone(CN_TZ)
    return None


def json_safe(value: Any) -> Any:
    """递归把日期时间和数据库对象转换为可稳定序列化的 JSON 值。"""

    if isinstance(value, datetime):
        normalized = normalize_datetime(value)
        return normalized.isoformat() if normalized is not None else str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def _account_value(account: Any, field_name: str, default: Any = None) -> Any:
    """同时支持平台账号对象和测试字典读取配置字段。"""

    if isinstance(account, Mapping):
        return account.get(field_name, default)
    return getattr(account, field_name, default)


def _configured_creators(accounts: Sequence[Any]) -> list[dict[str, Any]]:
    """把跨平台账号配置按逻辑博主去重，并保留最前展示顺序和名称。"""

    creators: dict[str, dict[str, Any]] = {}
    for account in accounts:
        if not bool(_account_value(account, "enabled", True)):
            continue
        creator_id = str(_account_value(account, "creator_id", "")).strip()
        if not creator_id:
            continue
        rank = int(_account_value(account, "rank", 9999))
        row = creators.setdefault(
            creator_id,
            {
                "creator_id": creator_id,
                "creator_name": str(
                    _account_value(account, "display_name", creator_id)
                ).strip()
                or creator_id,
                "rank": rank,
                "platforms": [],
            },
        )
        row["rank"] = min(int(row["rank"]), rank)
        platform = str(_account_value(account, "platform", "")).strip()
        if platform and platform not in row["platforms"]:
            row["platforms"].append(platform)
    return sorted(creators.values(), key=lambda item: (item["rank"], item["creator_id"]))


def _verification_opinions(
    document: Mapping[str, Any],
    *,
    works_by_key: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """把统一每日文档的观点结果与来源作品展示字段直接关联。"""

    rows: list[dict[str, Any]] = []
    for result in document.get("opinion_results") or []:
        if not isinstance(result, Mapping):
            continue
        work_key = str(result.get("work_key") or "").strip()
        work = works_by_key.get(work_key, {})
        analysis = work.get("analysis")
        source_opinion: Mapping[str, Any] = {}
        if isinstance(analysis, Mapping):
            source_opinion = next(
                (
                    item
                    for item in analysis.get("opinions") or []
                    if isinstance(item, Mapping)
                    and item.get("opinion_id") == result.get("opinion_id")
                ),
                {},
            )
        row = {
            "opinion_id": result.get("opinion_id"),
            "work_key": work_key,
            "platform": work.get("platform"),
            "title": work.get("title"),
            "canonical_url": work.get("canonical_url"),
            "source_published_at": result.get("source_published_at"),
            "target_type": result.get("target_type"),
            "target_id": result.get("target_id"),
            "target_name": result.get("target_name"),
            "direction": result.get("direction"),
            "claim": result.get("claim"),
            "metric": result.get("metric"),
            "horizon": source_opinion.get("horizon"),
            "conditions": source_opinion.get("conditions") or [],
            "source_quote": source_opinion.get("source_quote"),
            "verdict": result.get("verdict"),
            "verdict_label": VERDICT_LABELS.get(
                str(result.get("verdict")), str(result.get("verdict") or "")
            ),
            "reason": result.get("reason"),
            "evidence_refs": result.get("evidence_refs") or [],
            "web_evidence": result.get("web_evidence") or [],
            "opinion_score": result.get("opinion_score"),
        }
        rows.append(json_safe(row))
    return rows


def build_report(
    *,
    source_date: date,
    evaluation_date: date,
    accounts: Sequence[Any],
    works: Sequence[Mapping[str, Any]],
    verifications: Sequence[Mapping[str, Any]],
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """从作品表和每博主唯一观点文档构建兼容格式的验证报告。"""

    validate_report_dates(source_date, evaluation_date)
    generated = generated_at or datetime.now(CN_TZ)
    works_by_key = {
        str(item.get("work_key")): item
        for item in works
        if str(item.get("work_key") or "").strip()
    }
    verification_by_creator = {
        str(item.get("creator_id")): item
        for item in verifications
        if str(item.get("creator_id") or "").strip()
    }
    work_counts: dict[str, int] = {}
    for work in works:
        creator_id = str(work.get("creator_id") or "").strip()
        work_counts[creator_id] = work_counts.get(creator_id, 0) + 1

    creators = _configured_creators(accounts)
    known_ids = {item["creator_id"] for item in creators}
    for creator_id, document in verification_by_creator.items():
        if creator_id in known_ids:
            continue
        creators.append(
            {
                "creator_id": creator_id,
                "creator_name": str(document.get("creator_name") or creator_id),
                "rank": 9999,
                "platforms": [],
            }
        )

    creator_rows: list[dict[str, Any]] = []
    for creator in creators:
        creator_id = creator["creator_id"]
        document = verification_by_creator.get(creator_id)
        opinions = (
            _verification_opinions(document, works_by_key=works_by_key)
            if document is not None
            else []
        )
        creator_rows.append(
            json_safe(
                {
                    **creator,
                    "source_work_count": work_counts.get(creator_id, 0),
                    "verification_status": (
                        document.get("status") if document is not None else "missing"
                    ),
                    "daily_score": (
                        document.get("daily_score") if document is not None else None
                    ),
                    "rolling_score": (
                        document.get("rolling_score") if document is not None else None
                    ),
                    "daily_sample_count": (
                        document.get("daily_sample_count") if document is not None else 0
                    ),
                    "sample_count": (
                        document.get("sample_count") if document is not None else 0
                    ),
                    "warning": document.get("warning") if document is not None else None,
                    "error": document.get("error") if document is not None else None,
                    "market_facts": (
                        document.get("market_facts") if document is not None else {}
                    ),
                    "opinions": opinions,
                }
            )
        )

    ranking = [
        {
            "creator_id": item["creator_id"],
            "creator_name": item["creator_name"],
            "daily_score": item["daily_score"],
            "rolling_score": item["rolling_score"],
            "sample_count": item["sample_count"],
        }
        for item in creator_rows
        if item["verification_status"] == "completed"
        and isinstance(item["rolling_score"], Real)
        and not isinstance(item["rolling_score"], bool)
    ]
    ranking.sort(
        key=lambda item: (
            -float(item["rolling_score"]),
            (
                -float(item["daily_score"])
                if isinstance(item["daily_score"], Real)
                and not isinstance(item["daily_score"], bool)
                else 1.0
            ),
            item["creator_id"],
        )
    )
    for position, item in enumerate(ranking, start=1):
        item["position"] = position

    opinion_count = sum(len(item["opinions"]) for item in creator_rows)
    return {
        "report_version": REPORT_VERSION,
        "generated_at": generated.astimezone(CN_TZ).isoformat(),
        "source_date": source_date.isoformat(),
        "evaluation_date": evaluation_date.isoformat(),
        "summary": {
            "enabled_creator_count": len(_configured_creators(accounts)),
            "verification_document_count": len(verifications),
            "work_count": len(works),
            "opinion_count": opinion_count,
            "ranked_creator_count": len(ranking),
        },
        "ranking": ranking,
        "creators": creator_rows,
    }


def _markdown_cell(value: Any) -> str:
    """把任意标量压缩为不会破坏 Markdown 表格的单行文本。"""

    text = "" if value is None else str(value)
    return " ".join(text.split()).replace("|", "\\|")


def render_markdown(report: Mapping[str, Any]) -> str:
    """把两表报告渲染为包含排行榜和逐观点验证结果的中文 Markdown。"""

    summary = report["summary"]
    lines = [
        f"# 博主观点收盘验证报告（{report['evaluation_date']}）",
        "",
        f"- 作品来源日期：{report['source_date']}",
        f"- 来源作品：{summary['work_count']} 条",
        f"- 已验证观点：{summary['opinion_count']} 条",
        f"- 进入排名博主：{summary['ranked_creator_count']} 位",
        "",
        "## 博主评分排名",
        "",
        "| 排名 | 博主 | 当日分 | 近7日观点评分 | 样本数 |",
        "| ---: | --- | ---: | ---: | ---: |",
    ]
    for item in report["ranking"]:
        lines.append(
            "| {position} | {name} | {daily} | {rolling} | {count} |".format(
                position=item["position"],
                name=_markdown_cell(item["creator_name"]),
                daily=_markdown_cell(item["daily_score"]),
                rolling=_markdown_cell(item["rolling_score"]),
                count=item["sample_count"],
            )
        )
    if not report["ranking"]:
        lines.append("| - | 暂无完成评分 | - | - | 0 |")

    lines.extend(["", "## 逐博主观点", ""])
    for creator in report["creators"]:
        lines.extend(
            [
                f"### {_markdown_cell(creator['creator_name'])}",
                "",
                (
                    f"状态：{creator['verification_status']}；来源作品："
                    f"{creator['source_work_count']}；当日分："
                    f"{_markdown_cell(creator['daily_score']) or '-'}；滚动分："
                    f"{_markdown_cell(creator['rolling_score']) or '-'}。"
                ),
                "",
            ]
        )
        if creator.get("warning"):
            lines.extend([f"提示：{_markdown_cell(creator['warning'])}", ""])
        if creator.get("error"):
            lines.extend([f"错误：{_markdown_cell(creator['error'])}", ""])
        if not creator["opinions"]:
            lines.extend(["本次没有已验证观点。", ""])
            continue
        for index, opinion in enumerate(creator["opinions"], start=1):
            direction = DIRECTION_LABELS.get(
                str(opinion.get("direction")), str(opinion.get("direction") or "")
            )
            lines.extend(
                [
                    f"#### 观点 {index}：{_markdown_cell(opinion.get('target_name'))}",
                    "",
                    f"- 原观点：{_markdown_cell(opinion.get('claim'))}",
                    f"- 原文摘录：{_markdown_cell(opinion.get('source_quote')) or '-'}",
                    f"- 时间范围：{_markdown_cell(opinion.get('horizon')) or '-'}",
                    f"- 方向：{direction}",
                    f"- 验证：{_markdown_cell(opinion.get('verdict_label'))}",
                    f"- 单观点分：{_markdown_cell(opinion.get('opinion_score')) or '不计分'}",
                    f"- 结论理由：{_markdown_cell(opinion.get('reason'))}",
                ]
            )
            if opinion.get("canonical_url"):
                lines.append(f"- 来源：{opinion['canonical_url']}")
            for evidence in opinion.get("web_evidence") or []:
                lines.append(
                    f"- 网页证据：[{_markdown_cell(evidence.get('title'))}]"
                    f"({evidence.get('url')})：{_markdown_cell(evidence.get('quote'))}"
                )
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


async def load_report_documents(
    database: AsyncIOMotorDatabase,
    *,
    source_date: date,
    evaluation_date: date,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """只读查询两张生产业务表，并转换为报告现有的输入结构。"""

    start_at = datetime.combine(source_date, time.min, tzinfo=CN_TZ)
    end_at = datetime.combine(source_date, time.max, tzinfo=CN_TZ)
    works, analyses = await asyncio.gather(
        database["creator_works"]
        .find(
            {"published_at": {"$gte": start_at, "$lte": end_at}},
            {"_id": 0},
        )
        .sort([("published_at", 1), ("work_key", 1)])
        .to_list(None),
        database["creator_opinion_analyses"]
        .find({}, {"creator_name": 1, "verified_opinions": 1, "accuracy_score": 1})
        .sort([("_id", 1)])
        .to_list(None),
    )
    verifications: list[dict[str, Any]] = []
    target_date = evaluation_date.isoformat()
    for analysis in analyses:
        verified = list(analysis.get("verified_opinions") or [])
        current = [item for item in verified if item.get("verification_date") == target_date]
        scores = [item.get("score") for item in current if item.get("score") is not None]
        all_scores = [item.get("score") for item in verified if item.get("score") is not None]
        verifications.append(
            {
                "creator_id": str(analysis.get("_id")),
                "creator_name": analysis.get("creator_name"),
                "market_date": target_date,
                "status": "completed",
                "daily_score": (
                    round((sum(scores) / len(scores) + 1.0) * 50.0, 2)
                    if scores
                    else None
                ),
                "rolling_score": analysis.get("accuracy_score"),
                "daily_sample_count": len(scores),
                "sample_count": len(all_scores),
                "market_facts": {},
                "opinion_results": [
                    {
                        "opinion_id": item.get("opinion_id"),
                        "work_key": item.get("work_key"),
                        "source_published_at": item.get("published_at_beijing"),
                        "target_type": item.get("target_type"),
                        "target_name": item.get("target_name"),
                        "direction": item.get("direction"),
                        "claim": item.get("opinion"),
                        "verdict": item.get("verdict"),
                        "reason": item.get("reason"),
                        "opinion_score": item.get("score"),
                        "evidence_refs": [],
                        "web_evidence": [],
                    }
                    for item in current
                ],
            }
        )
    return list(works), verifications


def resolve_output_paths(
    *,
    evaluation_date: date,
    output_dir: Path | None,
    markdown_output: Path | None,
    json_output: Path | None,
) -> ReportOutputPaths:
    """根据输出目录和可选显式文件名生成两个互不冲突的绝对路径。"""

    active_output_dir = output_dir or PROJECT_ROOT / ".local" / "reports"
    stem = f"creator_opinion_verification_{evaluation_date.isoformat()}"
    markdown_path = (markdown_output or active_output_dir / f"{stem}.md").expanduser().resolve()
    json_path = (json_output or active_output_dir / f"{stem}.json").expanduser().resolve()
    if markdown_path == json_path:
        raise ValueError("Markdown 和 JSON 输出路径不能相同")
    return ReportOutputPaths(markdown=markdown_path, json=json_path)


async def export_report(
    *,
    source_date_text: str = DEFAULT_SOURCE_DATE,
    evaluation_date_text: str = DEFAULT_EVALUATION_DATE,
    output_dir: Path | None = None,
    markdown_output: Path | None = None,
    json_output: Path | None = None,
) -> ReportExportResult:
    """从两张生产业务表只读加载数据，并输出 Markdown 与 JSON 报告。"""

    source_date = parse_date_text(source_date_text, field_name="source_date")
    evaluation_date = parse_date_text(
        evaluation_date_text, field_name="evaluation_date"
    )
    validate_report_dates(source_date, evaluation_date)
    paths = resolve_output_paths(
        evaluation_date=evaluation_date,
        output_dir=output_dir,
        markdown_output=markdown_output,
        json_output=json_output,
    )
    settings = Settings()
    client = AsyncIOMotorClient(
        settings.mongo_uri,
        tz_aware=True,
        tzinfo=CN_TZ,
    )
    try:
        database: AsyncIOMotorDatabase = client[settings.mongo_db_name]
        works, verifications = await load_report_documents(
            database,
            source_date=source_date,
            evaluation_date=evaluation_date,
        )
    finally:
        client.close()

    report = build_report(
        source_date=source_date,
        evaluation_date=evaluation_date,
        accounts=get_enabled_accounts(),
        works=works,
        verifications=verifications,
    )
    markdown_text = render_markdown(report)
    json_text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    paths.markdown.parent.mkdir(parents=True, exist_ok=True)
    paths.json.parent.mkdir(parents=True, exist_ok=True)
    paths.markdown.write_text(markdown_text, encoding="utf-8")
    paths.json.write_text(json_text, encoding="utf-8")
    return ReportExportResult(
        source_date=source_date.isoformat(),
        evaluation_date=evaluation_date.isoformat(),
        markdown_path=str(paths.markdown),
        json_path=str(paths.json),
        work_count=int(report["summary"]["work_count"]),
        opinion_count=int(report["summary"]["opinion_count"]),
        ranked_creator_count=int(report["summary"]["ranked_creator_count"]),
    )


def build_argument_parser() -> argparse.ArgumentParser:
    """创建只包含来源日期、评价日期和输出路径的报告命令行参数。"""

    parser = argparse.ArgumentParser(
        description="只读导出博主作品和统一收盘验证评分报告",
    )
    parser.add_argument("--source-date", default=DEFAULT_SOURCE_DATE)
    parser.add_argument("--evaluation-date", default=DEFAULT_EVALUATION_DATE)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--markdown-output", type=Path, default=None)
    parser.add_argument("--json-output", type=Path, default=None)
    return parser


def main() -> None:
    """解析命令行参数，执行异步只读导出并打印中文 JSON 摘要。"""

    args = build_argument_parser().parse_args()
    result = asyncio.run(
        export_report(
            source_date_text=args.source_date,
            evaluation_date_text=args.evaluation_date,
            output_dir=args.output_dir,
            markdown_output=args.markdown_output,
            json_output=args.json_output,
        )
    )
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()


__all__ = [
    "DEFAULT_EVALUATION_DATE",
    "DEFAULT_SOURCE_DATE",
    "ReportExportResult",
    "ReportOutputPaths",
    "build_argument_parser",
    "build_report",
    "export_report",
    "json_safe",
    "load_report_documents",
    "normalize_datetime",
    "parse_date_text",
    "render_markdown",
    "resolve_output_paths",
    "validate_report_dates",
]
