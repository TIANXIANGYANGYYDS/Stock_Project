from app.services.creator_opinion_scope import is_historical_a_share_opinion


def test_historical_scope_keeps_a_share_comparison_and_rejects_foreign_target() -> None:
    assert is_historical_a_share_opinion(
        {
            "target_type": "market",
            "target_name": "A股",
            "claim": "A股不受美股下跌影响",
        }
    )
    assert not is_historical_a_share_opinion(
        {
            "target_type": "index",
            "target_name": "纳斯达克指数",
            "claim": "纳指短期反弹",
        }
    )


def test_explicit_scope_takes_precedence_over_migration_heuristic() -> None:
    assert not is_historical_a_share_opinion(
        {
            "market_scope": "non_a_share",
            "target_type": "market",
            "target_name": "A股",
        }
    )


def test_scope_uses_a_share_stock_and_market_view_whitelists() -> None:
    assert is_historical_a_share_opinion(
        {
            "target_type": "stock",
            "target_name": "贵州茅台",
            "claim": "贵州茅台股价短期可能反弹",
        }
    )
    assert not is_historical_a_share_opinion(
        {
            "target_type": "stock",
            "target_name": "英伟达",
            "claim": "英伟达市值未来可能达到5万亿美元",
        }
    )
    assert not is_historical_a_share_opinion(
        {
            "target_type": "sector",
            "target_name": "高技术制造业",
            "claim": "高技术制造业保持较快扩张",
        }
    )


def test_scope_rejects_macro_index_but_keeps_a_share_price_index_view() -> None:
    assert not is_historical_a_share_opinion(
        {
            "target_type": "sector",
            "target_name": "房地产",
            "claim": "房地产行业商务活动指数低于临界点，景气偏弱",
        }
    )
    assert is_historical_a_share_opinion(
        {
            "target_type": "index",
            "target_name": "上证指数",
            "claim": "波段底形成后日线级别双底将上补缺口",
        }
    )
    assert is_historical_a_share_opinion(
        {
            "target_type": "index",
            "target_name": "指数",
            "claim": "指数会守住3800点",
        },
        source_text="今天复盘A股市场",
    )
