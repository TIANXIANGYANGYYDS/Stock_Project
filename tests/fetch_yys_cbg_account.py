"""抓取阴阳师藏宝阁当前页面的账号和完整详情。"""

import argparse
import json
import os
import sys
import uuid

from curl_cffi import requests


BASE_URL = "https://yys.cbg.163.com"
DEFAULT_URL = (
    "https://yys.cbg.163.com/cgi/mweb/"
    "?refer_sn=019FF006-515A-06A0-B393-B1ACDAB3D37C"
)
PLATFORM_NAMES = {1: "iOS", 2: "Android", 3: "PC"}


def get_json(response):
    response.raise_for_status()
    data = response.json()
    if data.get("status_code") not in (None, "OK"):
        raise RuntimeError(data.get("msg") or data.get("status_code"))
    return data


def parse_account_data(equip_desc):
    if isinstance(equip_desc, str):
        return json.loads(equip_desc)
    return equip_desc or {}


def format_account(list_item, detail):
    equip = {**list_item, **detail}
    server_id = equip["serverid"]
    order_sn = equip["game_ordersn"]
    other_info = equip.get("other_info") or {}
    price = equip.get("price")
    account_data = parse_account_data(equip.pop("equip_desc", None))

    return {
        "listing_url": f"{BASE_URL}/cgi/mweb/equip/{server_id}/{order_sn}",
        "server_id": server_id,
        "order_sn": order_sn,
        "name": equip.get("format_equip_name") or equip.get("equip_name"),
        "level": equip.get("level_desc") or equip.get("equip_level"),
        "area_name": equip.get("area_name"),
        "server_name": equip.get("server_name"),
        "platform": PLATFORM_NAMES.get(equip.get("platform_type")),
        "price_yuan": price / 100 if price is not None else None,
        "collect_count": equip.get("collect_num"),
        "summary": equip.get("desc_sumup_short"),
        "basic_attributes": other_info.get("basic_attrs", []),
        "highlights": other_info.get("highlights") or equip.get("highlights", []),
        "account_data": account_data,
        "detail_data": equip,
    }


def fetch_detail(session, list_item, referer, common_data):
    server_id = list_item["serverid"]
    order_sn = list_item["game_ordersn"]
    detail_url = f"{BASE_URL}/cgi/mweb/equip/{server_id}/{order_sn}"

    response = session.post(
        f"{BASE_URL}/cgi/api/get_equip_detail",
        params={"client_type": "h5"},
        data={
            "serverid": server_id,
            "ordersn": order_sn,
            **common_data,
        },
        headers={"Origin": BASE_URL, "Referer": detail_url},
        timeout=30,
    )
    detail = get_json(response)["equip"]

    if not detail.get("equip_desc"):
        response = session.get(
            f"{BASE_URL}/cgi/api/get_equip_desc",
            params={
                "serverid": server_id,
                "ordersn": order_sn,
                "client_type": "h5",
            },
            headers={"Referer": referer},
            timeout=30,
        )
        detail["equip_desc"] = get_json(response)["equip_desc"]

    return format_account(list_item, detail)


def fetch_accounts(url=DEFAULT_URL, count=15, cookie=None):
    session = requests.Session(impersonate="chrome")
    session.headers.update(
        {
            "Accept-Language": "zh-CN,zh;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
        }
    )
    if cookie:
        session.headers["Cookie"] = cookie

    home_response = session.get(url, timeout=30)
    home_response.raise_for_status()
    referer = str(home_response.url)

    common_data = {
        "page_session_id": str(uuid.uuid4()).upper(),
        "traffic_trace": '{"field_id":"","content_id":""}',
    }
    response = session.get(
        f"{BASE_URL}/cgi/api/query",
        params={
            "search_type": "role",
            "page": 1,
            "client_type": "h5",
            **common_data,
        },
        headers={"Referer": referer},
        timeout=30,
    )
    list_items = get_json(response)["result"][:count]

    accounts = []
    for index, item in enumerate(list_items, 1):
        order_sn = item.get("game_ordersn")
        print(f"[{index}/{len(list_items)}] 正在获取 {order_sn}", file=sys.stderr)
        try:
            accounts.append(fetch_detail(session, item, referer, common_data))
        except Exception as exc:
            accounts.append(
                {
                    "server_id": item.get("serverid"),
                    "order_sn": order_sn,
                    "detail_error": str(exc),
                    "list_data": item,
                }
            )

    failed = sum("detail_error" in account for account in accounts)
    return {
        "account_count": len(accounts),
        "detail_success_count": len(accounts) - failed,
        "detail_failure_count": failed,
        "accounts": accounts,
    }


def main():
    parser = argparse.ArgumentParser(description="抓取阴阳师藏宝阁账号完整信息")
    parser.add_argument("url", nargs="?", default=DEFAULT_URL)
    parser.add_argument("--count", type=int, default=15, help="抓取当前页前多少个账号")
    parser.add_argument("--output", default="yys_accounts.json", help="结果文件")
    parser.add_argument("--cookie", default=os.getenv("YYS_CBG_COOKIE"))
    args = parser.parse_args()

    result = fetch_accounts(args.url, args.count, args.cookie)
    with open(args.output, "w", encoding="utf-8") as file:
        json.dump(result, file, ensure_ascii=False, indent=2)

    print(
        f"完成：成功 {result['detail_success_count']}，"
        f"失败 {result['detail_failure_count']}，结果已保存到 {args.output}"
    )


if __name__ == "__main__":
    main()
