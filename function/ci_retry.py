import json

import pytest

from function.api_client import order_write_started


class RetryCases:
    """Record failed pytest node IDs and limit the next run to those IDs."""

    def __init__(self, selection=None):
        self.selection = selection
        self.failures = {}

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item):
        token = order_write_started.set(False)
        try:
            return (yield)
        finally:
            order_write_started.reset(token)

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_makereport(self, item, call):
        report = yield
        if report.failed:
            reason = None
            if order_write_started.get():
                reason = "首轮已尝试提交或撤单，请按首轮报告回查原订单"
            elif item.get_closest_marker("trade_followup"):
                reason = "此用例依赖首轮买单，需使用原订单继续核对"
            if reason or item.nodeid not in self.failures:
                self.failures[item.nodeid] = reason
        return report

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, config, items):
        if self.selection is None:
            return
        missing = self.selection.keys() - {item.nodeid for item in items}
        if missing:
            raise pytest.UsageError(f"补跑用例未收集到：{', '.join(sorted(missing))}")
        selected, deselected = [], []
        for item in items:
            if item.nodeid not in self.selection:
                deselected.append(item)
                continue
            if reason := self.selection[item.nodeid]:
                item.add_marker(pytest.mark.skip(reason=reason))
            selected.append(item)
        items[:] = selected
        config.hook.pytest_deselected(items=deselected)


def load_retry_cases(path):
    cases = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cases, dict) or not cases or any(
        not nodeid.startswith("testcase/") or (reason is not None and not isinstance(reason, str))
        for nodeid, reason in cases.items()
    ):
        raise ValueError("补跑文件应包含首轮失败的 pytest 用例 ID")
    return cases
