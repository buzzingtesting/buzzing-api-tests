from time import time

import allure
import pytest

from function.market import (
    MARKET_EVENT_ITEMS_SCHEMA,
    find_market_event,
    get_market_event_detail,
    get_sports_match_detail,
    is_open_market_event,
    is_single_binary_market_event,
    list_event_markets,
    list_market_events,
)
from function.market_account import check_market_account_activity
from function.reporting import PROJECT_NAME, attach_market_sample
from function.response_checks import expect_native


pytestmark = [
    allure.parent_suite(PROJECT_NAME),
    allure.suite("市场详情"),
    allure.epic(PROJECT_NAME),
    allure.feature("市场详情"),
]

BINARY_OPTIONS_SCHEMA = {
    "type": "array",
    "minItems": 2,
    "maxItems": 2,
    "items": {
        "type": "object",
        "required": ["outcome_label"],
        "properties": {"outcome_label": {"type": "string", "minLength": 1}},
    },
}

TEAM_SCHEMA = {
    "type": "object",
    "required": ["id", "name"],
    "properties": {
        "id": {"type": "integer"},
        "name": {"type": "string", "minLength": 1},
    },
}

SPORTS_MATCH_SCHEMA = {
    "type": "object",
    "required": [
        "match", "markets", "market_sections", "market_sections_layout", "market_sections_layout_version",
    ],
    "properties": {
        "match": {
            "type": "object",
            "required": ["id", "event_id", "home_team", "away_team"],
            "properties": {
                "id": {"type": "integer"},
                "event_id": {"type": "integer"},
                "home_team": TEAM_SCHEMA,
                "away_team": TEAM_SCHEMA,
            },
        },
        "markets": {"type": "object", "minProperties": 1},
        "market_sections_layout": {"type": "string"},
        "market_sections_layout_version": {"type": "integer"},
        "market_sections": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["tab_key", "blocks"],
                "properties": {
                    "tab_key": {"type": "string", "minLength": 1},
                    "blocks": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "required": ["key", "kind"],
                            "properties": {
                                "key": {"type": "string", "minLength": 1},
                                "kind": {"type": "string", "minLength": 1},
                            },
                        },
                    },
                },
            },
        },
    },
}


def _check_ordinary_binary_detail(api_client, case):
    now = time()
    with allure.step("从本轮市场列表查找尚未结束的金融类单一二元市场"):
        event = find_market_event(
            api_client,
            case["list_params"],
            lambda item: _is_ordinary_binary(item, case["taxonomy_sport_slug"], now),
        )
        assert event is not None, "市场列表中未找到尚未结束的金融类单一二元市场"
        listed_market = event["markets"][0]
        event_id = int(event["id"])
        market_id = int(listed_market["market"]["id"])
        attach_market_sample(event, market_id=market_id)

    with allure.step("查询详情并核对事件、主市场和两个结果选项"):
        detail = expect_native(
            get_market_event_detail(api_client, event_id),
            status=200,
            schema={
                "type": "object",
                "required": ["id", "title", "markets_count", "primary_market"],
                "properties": {
                    "id": {"const": event_id},
                    "title": {"const": event["title"]},
                    "markets_count": {"const": 1},
                    "primary_market": {
                        "type": "object",
                        "required": ["id", "event_id", "outcome_kind", "options"],
                        "properties": {
                            "id": {"const": market_id},
                            "event_id": {"const": event_id},
                            "outcome_kind": {"const": "binary"},
                            "options": BINARY_OPTIONS_SCHEMA,
                        },
                    },
                },
            },
        )
        expected_labels = {option["outcome_label"] for option in listed_market["options"]}
        actual_labels = {option["outcome_label"] for option in detail["primary_market"]["options"]}
        assert len(expected_labels) == 2 and actual_labels == expected_labels, "详情应与列表的两个选项一致"
    return detail["primary_market"]


def _check_non_exclusive_multi_detail(api_client, case):
    series_slug = case["series_slug"]
    with allure.step("从本轮市场列表选取尚未结束的多子市场事件"):
        items = _list_market_series(api_client, series_slug)
        now = time()
        event = next(
            (
                item for item in items
                if item.get("series_slug") == series_slug
                and item.get("market_count", 0) >= case["min_markets"]
                and is_open_market_event(item, now)
            ),
            None,
        )
        assert event is not None, "市场列表中未找到尚未结束的多元非互斥事件"
        event_id = int(event["id"])
        attach_market_sample(event)

    with allure.step("查询事件详情并核对子市场归属、数量和 Yes/No 选项"):
        detail = expect_native(
            get_market_event_detail(api_client, event_id),
            status=200,
            schema={
                "type": "object",
                "required": ["id", "title", "series_slug", "neg_risk", "markets_count", "markets"],
                "properties": {
                    "id": {"const": event_id},
                    "title": {"const": event["title"]},
                    "series_slug": {"const": series_slug},
                    "neg_risk": {"const": False},
                    "markets_count": {"const": event["market_count"]},
                    "markets": {
                        "type": "array",
                        "minItems": case["min_markets"],
                        "maxItems": event["market_count"],
                        "items": {
                            "type": "object",
                            "required": ["id", "event_id", "outcome_kind", "options"],
                            "properties": {
                                "id": {"type": "integer", "minimum": 1},
                                "event_id": {"const": event_id},
                                "outcome_kind": {"const": "binary"},
                                "options": BINARY_OPTIONS_SCHEMA,
                            },
                        },
                    },
                },
            },
        )
        markets = detail["markets"]
        assert len({market["id"] for market in markets}) == len(markets), "子市场 ID 应唯一"
        expected_labels = set(case["outcome_labels"])
        for market in markets:
            assert {option["outcome_label"] for option in market["options"]} == expected_labels
    return markets[0]


def _check_sports_match_detail(api_client, case):
    with allure.step("从本轮市场列表查找有效的体育比赛"):
        event = find_market_event(api_client, case["list_params"], _is_active_sports_match)
        assert event is not None, "市场列表中未找到带比赛 ID 的有效体育市场"
        match_id = event["sport"]["meta"]["match_id"]
        attach_market_sample(event, match_id=match_id)

    with allure.step("查询体育详情并核对比赛、事件和 Blocks 布局"):
        params = case["detail_params"]
        detail = expect_native(
            get_sports_match_detail(api_client, match_id, params=params),
            status=200,
            schema=SPORTS_MATCH_SCHEMA,
        )
        match = detail["match"]
        assert match["id"] == match_id, "详情的比赛 ID 应与列表一致"
        assert str(match["event_id"]) == event["id"], "详情的事件 ID 应与列表一致"
        assert match["home_team"]["id"] != match["away_team"]["id"], "主客队应是不同球队"
        assert detail["market_sections_layout"] == params["layout"]
        assert detail["market_sections_layout_version"] == params["blocks_version"]
        market = next(
            (
                market for groups in detail["markets"].values() for group in groups
                for market in group["markets"] if market.get("event_id") == match["event_id"]
            ),
            None,
        )
        assert market is not None, "体育详情未找到属于该比赛事件的子市场"
    return market


def _is_ordinary_binary(event, taxonomy_sport_slug, now):
    return (
        (event.get("taxonomy") or {}).get("sport_slug") == taxonomy_sport_slug
        and is_open_market_event(event, now)
        and is_single_binary_market_event(event)
    )


def _is_active_sports_match(event):
    if event.get("is_sport") is not True or event.get("status") != "active":
        return False
    meta = (event.get("sport") or {}).get("meta") or {}
    match_id = meta.get("match_id")
    return type(match_id) is int and match_id > 0


def _check_market_series_detail(api_client, case):
    series_slug = case["series_slug"]
    detail_fields = {"series_slug": series_slug, **case["detail"]}
    expected_labels = set(case["outcome_labels"])

    with allure.step("按系列查询市场列表并选取尚未结束的事件"):
        items = _list_market_series(api_client, series_slug)
        now = time()
        for event in items:
            if (
                event.get("series_slug") == series_slug
                and event.get("cadence") == detail_fields["cadence"]
                and event.get("archetype") == detail_fields["archetype"]
                and is_open_market_event(event, now)
            ):
                break
        else:
            pytest.fail(f"市场列表中未找到尚未结束的 {series_slug} 事件", pytrace=False)
        attach_market_sample(event)

    with allure.step("核对事件详情的 ID、标题、币种和周期"):
        detail = expect_native(
            get_market_event_detail(api_client, event["id"]),
            status=200,
            schema={
                "type": "object",
                "required": ["id", "title", "markets", *detail_fields],
                "properties": {
                    "id": {"type": "integer"},
                    "title": {"type": "string", "minLength": 1},
                    "markets": {"type": "array"},
                    **{field: {"const": value} for field, value in detail_fields.items()},
                },
            },
        )
        assert str(detail["id"]) == event["id"], "详情的事件 ID 应与列表一致"
        assert detail["title"] == event["title"], "详情标题应与列表一致"

    with allure.step("核对子市场数量、事件归属和结果选项"):
        children = expect_native(
            list_event_markets(api_client, event["id"]),
            status=200,
            schema={
                "type": "object",
                "required": ["event_id", "markets"],
                "properties": {
                    "event_id": {"const": event["id"]},
                    "markets": {
                        "type": "array",
                        "minItems": case["min_markets"],
                        "items": {
                            "type": "object",
                            "required": ["market", "options"],
                            "properties": {
                                "market": {
                                    "type": "object",
                                    "required": ["id", "event_id", "outcome_kind", "outcome_count"],
                                    "properties": {
                                        "id": {"type": "string", "minLength": 1},
                                        "event_id": {"const": detail["id"]},
                                        "outcome_kind": {"const": "binary"},
                                        "outcome_count": {"const": 2},
                                    },
                                },
                                "options": {
                                    "type": "array",
                                    "minItems": 2,
                                    "maxItems": 2,
                                    "items": {
                                        "type": "object",
                                        "required": ["outcome_label"],
                                        "properties": {"outcome_label": {"type": "string"}},
                                    },
                                },
                            },
                        },
                    },
                },
            },
        )
        market_ids = {child["market"]["id"] for child in children["markets"]}
        assert len(market_ids) == len(children["markets"]), "子市场 ID 应唯一"
        for child in children["markets"]:
            assert {option["outcome_label"] for option in child["options"]} == expected_labels
    selected = children["markets"][0]
    return {**selected["market"], "options": selected["options"]}


def _list_market_series(api_client, series_slug):
    page = expect_native(
        list_market_events(api_client, params={"series_slug": series_slug}),
        status=200,
        schema={
            "type": "object",
            "required": ["items"],
            "properties": {"items": {**MARKET_EVENT_ITEMS_SCHEMA, "minItems": 0}},
        },
    )
    return page["items"]


MARKET_DETAIL_CASES = {
    "btc_1h": ("二元数字货币循环市场", _check_market_series_detail),
    "xrp_daily_hit_price": ("周期多档触价市场", _check_market_series_detail),
    "ordinary_binary": ("普通二元市场（金融类）", _check_ordinary_binary_detail),
    "non_exclusive_multi": ("多元非互斥市场", _check_non_exclusive_multi_detail),
    "sports_match_blocks": ("体育比赛", _check_sports_match_detail),
}


@allure.title("市场详情 · {market_label}")
@pytest.mark.parametrize(
    ("case_name", "market_label"),
    [
        pytest.param(case_name, label, id=case_name)
        for case_name, (label, _) in MARKET_DETAIL_CASES.items()
    ],
)
def test_market_detail(authenticated_api_client, test_data, case_name, market_label):
    _, check_detail = MARKET_DETAIL_CASES[case_name]
    cases = test_data["market_cases"]
    market = check_detail(authenticated_api_client, cases[case_name])
    check_market_account_activity(authenticated_api_client, market, cases["market_account"])
