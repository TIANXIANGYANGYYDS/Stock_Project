"""Read-only capital source probe. DDE availability requires actual numeric data."""
import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path

import httpx

from app.crawlers.fund_flow_crawler import FundFlowCrawler
from app.crawlers.realtime_market_crawler import CN_TZ
from app.services.market_observation_service import observation_now

DDE_QUERY_URL = "https://www.iwencai.com/gateway/aime/stream-query"


def classify_dde_response(status, body):
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if status == 401 or payload.get('status_code') == -1935:
        reason = 'login_required'
    elif status in (403, 429):
        reason = 'access_blocked'
    elif not body.strip():
        reason = 'empty_response'
    else:
        reason = 'response_requires_schema_and_metric_verification'
    # A 200 HTML / SSE envelope is not evidence of a usable DDE retail value.
    return {'available': False, 'reason': reason, 'http_status': status,
            'provider_status': payload.get('status_code'), 'response_bytes': len(body.encode()),
            'source_url': DDE_QUERY_URL, 'metric': 'THS_DDE_RETAIL_PARTICIPATION'}


async def probe_dde():
    try:
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            response = await client.post(DDE_QUERY_URL,
                headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.iwencai.com/'},
                json={'question': '600519，DDE散户数量', 'source': 'ths_iwencai_pc_xuangu',
                      'input_type': 'click', 'default_fallback': False,
                      'entity_info': {'device_type': 'pc'}, 'dialog_model': 'CUSTOMER_AGENT',
                      'agent_tools': [{'tool_id': 'FinQuery', 'tool_param': {'domain': 'stock', 'perpage': 50}}]})
            return classify_dde_response(response.status_code, response.text)
    except httpx.HTTPError as exc:
        return {'available': False, 'reason': 'transport_error', 'error_type': type(exc).__name__,
                'source_url': DDE_QUERY_URL, 'metric': 'THS_DDE_RETAIL_PARTICIPATION'}


async def run(output=None):
    now, offset = await observation_now()
    crawler = FundFlowCrawler()
    try:
        data = await crawler.fetch(now.date().isoformat(), clock_offset=offset,
                                   codes=['600519', '000001', '920002'])
        funds = {k: v for k, v in data.items() if k != 'records'}
        funds['samples'] = [{k: v for k, v in row.items() if k != 'raw'} for row in data['records']]
    except Exception as exc:
        funds = {'available': False, 'error_type': type(exc).__name__}
    finally:
        await crawler.close()
    report = {'checked_at': datetime.now(CN_TZ), 'corrected_now': now,
              'fund_flow': funds, 'dde_retail': await probe_dde()}
    content = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if output:
        path = Path(output); path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
    print(content)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output')
    args = parser.parse_args()
    asyncio.run(run(args.output))
