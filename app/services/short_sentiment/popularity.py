"""Bind immutable THS popularity observations to the quote's market clock."""
from app.repositories.market_observation_repository import HOT_COLLECTION
from app.services.short_sentiment.facts import instant, number

MAX_AGE_SECONDS = 900


async def attach_popularity(db, snapshot):
    at = instant(snapshot['meta']['as_of'])
    row = await db[HOT_COLLECTION].find_one(
        {'trade_date': snapshot['meta']['trade_date'], 'observed_at': {'$lte': at}},
        sort=[('observed_at', -1)])
    captured = instant(row.get('observed_at')) if row else None
    age = (at - captured).total_seconds() if captured else None
    available = age is not None and 0 <= age <= MAX_AGE_SECONDS
    by_code = {s['code']: s for s in row['stocks']} if available else {}
    common = {'provider': 'ths', 'snapshot_id': str(row['_id']) if row else None,
              'observed_at': captured.isoformat() if captured else None,
              'age_seconds': age, 'max_age_seconds': MAX_AGE_SECONDS}
    snapshot['meta']['popularity'] = {**common, 'status': 'available' if available else 'stale' if row else 'missing'}
    for stock in snapshot['stocks'].values():
        item = by_code.get(stock['code'])
        stock['popularity'] = {**common, 'rank': item['rank'] if item else None,
            'heat': number(item.get('heat')) if item else None,
            'rank_change': item.get('rank_change') if item else None,
            'status': 'ranked' if item else 'not_ranked' if available else 'stale' if row else 'missing'}
