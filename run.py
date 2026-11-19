import argparse
import os
import sys
from pathlib import Path
from tempfile import mkdtemp

import pytest

from function.reporting import generate_allure_report, publish_latest_report, write_report_environment
from function.settings import BASE_URLS, get_environment, get_testdata


PROJECT_DIR = Path(__file__).resolve().parent
REPORTS_DIR = PROJECT_DIR / "reports"


def main():
    os.chdir(PROJECT_DIR)
    environment = get_environment()
    data_group = get_testdata()
    REPORTS_DIR.mkdir(exist_ok=True)
    run_dir = Path(mkdtemp(prefix="run-", dir=REPORTS_DIR))
    results_dir = run_dir / "allure-results"
    print(f"本轮报告目录 {run_dir}", flush=True)

    pytest_args = [
        "testcase", "-v", "-s", "--tb=short",
        "--no-showlocals", "--allure-no-capture",
        f"--log-file={run_dir / 'http.log'}", "--log-file-level=INFO",
        f"--junitxml={run_dir / 'junit.xml'}",
        f"--alluredir={results_dir}",
    ]
    if environment == "prod":
        pytest_args.extend(["-m", "pro"])
    result = pytest.main(pytest_args)
    if not any(results_dir.glob("*-result.json")):
        return result

    write_report_environment(
        results_dir, environment=environment, base_url=BASE_URLS[environment],
        data_group=data_group,
    )
    html_dir = run_dir / "allure-report"
    report = generate_allure_report(results_dir, html_dir)
    report_html = html_dir / "index.html"
    if report == 0 and report_html.is_file():
        publish_latest_report(report_html, REPORTS_DIR / "allure-report")
    return result or report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="运行接口场景并生成 Allure 报告")
    parser.add_argument("--env", choices=BASE_URLS, help="指定运行环境，默认 preview")
    args = parser.parse_args()
    if args.env:
        os.environ["BUZZING_ENV"] = args.env
    try:
        sys.exit(main())
    except ValueError as error:
        sys.exit(str(error))
