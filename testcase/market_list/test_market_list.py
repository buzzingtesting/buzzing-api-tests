import allure
import pytest

from function.market import (
    MARKET_EVENT_PAGE_SCHEMA,
    get_sports_navigation,
    list_esports_props,
    list_market_events,
    select_esports_props_league,
)
from function.reporting import PROJECT_NAME, ordered_suite_name
from function.response_checks import expect_native
from function.settings import load_test_data


pytestmark = [
    allure.parent_suite(PROJECT_NAME),
    allure.suite(ordered_suite_name("市场")),
    allure.sub_suite("列表"),
    allure.epic(PROJECT_NAME),
    allure.feature("市场"),
]

HOME_MARKET_LIST = load_test_data()["market_cases"]["home_list"]


@allure.title("市场列表筛选 · {sort_label}")
@pytest.mark.parametrize(
    ("sort_by", "sort_label"),
    [
        pytest.param(sort_by, label, id=sort_by)
        for sort_by, label in HOME_MARKET_LIST["sort_modes"].items()
    ],
)
def test_market_event_list(api_client, sort_by, sort_label):
    page_size = HOME_MARKET_LIST["page_size"]
    with allure.step(f"{sort_label} 排序下至少返回 1 条市场事件"):
        page = expect_native(
            list_market_events(api_client, params={"page_size": page_size, "sort_by": sort_by}),
            status=200,
            schema=MARKET_EVENT_PAGE_SCHEMA,
        )
        assert len(page["items"]) <= page_size


@allure.title("市场事件列表按游标查询下一页")
def test_market_event_list_next_page(api_client):
    page_size = HOME_MARKET_LIST["page_size"]
    params = {"page_size": page_size, "sort_by": "top_picks"}

    with allure.step("查询首页并获取下一页游标"):
        first_page = expect_native(
            list_market_events(api_client, params=params),
            status=200,
            schema=MARKET_EVENT_PAGE_SCHEMA,
        )
        assert len(first_page["items"]) <= page_size
        assert first_page.get("has_more"), "Preview 测试数据需要至少两页热门市场事件"
        cursor = first_page["next_cursor"]

    with allure.step("用本轮游标查询下一页"):
        next_page = expect_native(
            list_market_events(api_client, params={**params, "cursor": cursor}),
            status=200,
            schema=MARKET_EVENT_PAGE_SCHEMA,
        )
        assert len(next_page["items"]) <= page_size
        first_ids = {event["id"] for event in first_page["items"]}
        assert any(event["id"] not in first_ids for event in next_page["items"]), "下一页应有新的事件"
        if next_page.get("has_more"):
            assert next_page["next_cursor"] != cursor, "下一页游标应向前推进"


@allure.title("E-sports 分类 · Props")
def test_esports_props_category(api_client, test_data):
    case = test_data["market_cases"]["esports_props"]
    with allure.step("从本轮 E-sports 导航选取含 Props 的联赛"):
        league_slug = case.get("league_slug") or select_esports_props_league(api_client)
        allure.dynamic.parameter("电竞联赛", league_slug)
    params = case["params"]
    expected_fields = {"league_slug": league_slug, "tab": "props", **params}
    with allure.step(f"查询 {league_slug} Props 并核对筛选参数和市场数量"):
        page = expect_native(
            list_esports_props(api_client, league_slug, params=params),
            status=200,
            schema={
                "type": "object",
                "required": ["items", "total", *expected_fields],
                "properties": {
                    **{key: {"const": value} for key, value in expected_fields.items()},
                    "total": {"type": "integer", "minimum": 0},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["id", "title"],
                            "properties": {
                                "id": {"type": "integer", "minimum": 1},
                                "title": {"type": "string", "minLength": 1},
                            },
                        },
                    },
                },
            },
        )
        assert page["total"] > 0 and page["items"], f"{league_slug} Props 分类没有市场"
        assert len(page["items"]) <= min(page["total"], params["page_size"])


@allure.title("Sports 分类 · 体育导航")
def test_sports_navigation_category(api_client):
    with allure.step("查询体育导航并确认 Sports 有赛事或 Props"):
        navigation = expect_native(
            get_sports_navigation(api_client),
            status=200,
            schema={
                "type": "object",
                "required": ["sports", "nav_sports"],
                "properties": {
                    "nav_sports": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["slug", "name"],
                            "properties": {
                                "slug": {"type": "string", "minLength": 1},
                                "name": {"type": "string", "minLength": 1},
                            },
                        },
                    },
                    "sports": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["sport", "count", "props_count"],
                            "properties": {
                                "sport": {"type": "string", "minLength": 1},
                                "count": {"type": "integer", "minimum": 0},
                                "props_count": {"type": "integer", "minimum": 0},
                            },
                        },
                    },
                },
            },
        )
        assert any(node["slug"] != "esports" for node in navigation["nav_sports"]), "Sports 分类导航为空"
        assert any(
            item["sport"] != "esports" and (item["count"] > 0 or item["props_count"] > 0)
            for item in navigation["sports"]
        ), "Sports 分类没有赛事或 Props"
