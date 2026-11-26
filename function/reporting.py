import json
import os
import shutil
import subprocess
from pathlib import Path
from tempfile import NamedTemporaryFile
from xml.etree import ElementTree

import allure


PROJECT_NAME = "Buzzing 接口自动化"
REPORT_SUITE_ORDER = (
    "身份与登录",
    "市场",
    "Orderbook 盘口",
    "市价单",
    "限价单",
    "Position 持仓",
    "市场争议与结算",
)


def ordered_suite_name(name):
    return f"{REPORT_SUITE_ORDER.index(name) + 1:02d} {name}"


def attach_market_sample(event, **identifiers):
    sample = {"title": event["title"], "event_id": event["id"], **identifiers}
    allure.attach(
        json.dumps(sample, ensure_ascii=False, indent=2),
        name="本轮选中市场",
        attachment_type=allure.attachment_type.JSON,
    )


def junit_summary(reports_dir, *, bold_total=False):
    files = list(reports_dir.glob("run-*/junit.xml"))
    if not files:
        return "本轮暂无用例统计，请查看 Actions 日志。"

    junit_file = max(files, key=lambda path: path.stat().st_mtime_ns)
    try:
        root = ElementTree.parse(junit_file).getroot()
    except (OSError, ElementTree.ParseError):
        return "本轮 JUnit 报告无法读取，请查看 Actions 日志。"

    outcomes = ("error", "failure", "skipped")
    counts = dict.fromkeys((*outcomes, "passed"), 0)
    for test in root.iter("testcase"):
        result = next((tag for tag in outcomes if test.find(tag) is not None), "passed")
        counts[result] += 1

    total = f"用例 {sum(counts.values())} 条"
    if bold_total:
        total = f"**{total}**"
    return (
        f"{total}\n通过 {counts['passed']} · 失败 {counts['failure']} · "
        f"错误 {counts['error']} · 跳过 {counts['skipped']}"
    )


def write_report_environment(results_dir, *, environment, base_url, data_group):
    (results_dir / "environment.properties").write_text(
        f"Environment={environment}\nAPI.Base.URL={base_url}\nDefault.test.account={data_group}\n",
        encoding="utf-8",
    )


def generate_allure_report(results_dir, html_dir):
    allure_cli = shutil.which("allure") or shutil.which(
        "allure", path=os.pathsep.join(("/opt/homebrew/bin", "/usr/local/bin"))
    )
    if allure_cli is None:
        print(f"Allure 原始结果 {results_dir}")
        return 0

    report = subprocess.run(
        [
            allure_cli, "generate", str(results_dir),
            "--name", PROJECT_NAME, "--single-file", "-o", str(html_dir),
        ],
        check=False,
    )
    if report.returncode == 0:
        print(f"Allure HTML {html_dir / 'index.html'}")
    return report.returncode


def publish_latest_report(report_html, latest_dir):
    latest_dir.mkdir(exist_ok=True)
    latest = latest_dir / "index.html"
    with NamedTemporaryFile(dir=latest_dir, delete=False) as pending:
        temporary = Path(pending.name)
        try:
            with report_html.open("rb") as source:
                shutil.copyfileobj(source, pending)
            pending.close()
            temporary.replace(latest)
        finally:
            temporary.unlink(missing_ok=True)
    print(f"最新 Allure 报告 {latest}")
