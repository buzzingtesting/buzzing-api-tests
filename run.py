import argparse
import json
import os
import sys
from pathlib import Path
from tempfile import mkdtemp

import pytest

from function.ci_retry import RetryCases, load_retry_cases
from function.reporting import generate_allure_report, publish_latest_report, write_report_environment
from function.settings import BASE_URLS, get_environment, get_testdata


PROJECT_DIR = Path(__file__).resolve().parent
REPORTS_DIR = PROJECT_DIR / "reports"


def main(*, allow_test_failures=False, trade_runs=(), dispute_market_id=None,
         dispute_no_position_market_id=None, disputed_market_id=None, retry_cases=None):
    trade_runs = tuple(Path(path).resolve() for path in trade_runs)
    retry_selection = load_retry_cases(Path(retry_cases)) if retry_cases else None
    os.chdir(PROJECT_DIR)
    environment = get_environment()
    if trade_runs and environment != "preview":
        raise ValueError("受控交易只能加入 Preview 运行")
    dispute_args = []
    for option, market_id in (
        ("--dispute-market-id", dispute_market_id),
        ("--dispute-no-position-market-id", dispute_no_position_market_id),
        ("--disputed-market-id", disputed_market_id),
    ):
        if market_id is not None:
            dispute_args.extend([option, str(market_id)])
    if dispute_args and trade_runs:
        raise ValueError("请单独运行手动争议回归")
    if retry_selection and (environment != "preview" or dispute_args):
        raise ValueError("失败用例补跑只适用于 Preview 完整回归")
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
    if dispute_args:
        pytest_args.extend(["-m", "manual_dispute", *dispute_args])
    elif environment == "prod":
        pytest_args.extend(["-m", "pro"])
    for path in trade_runs:
        pytest_args.extend(["--trade-run", str(path)])
    retry = RetryCases(retry_selection)
    result = pytest.main(pytest_args, plugins=[retry])
    github_output = os.getenv("GITHUB_OUTPUT")
    if retry.failures and environment == "preview" and not retry_selection and not dispute_args:
        payload = json.dumps(retry.failures, ensure_ascii=False, separators=(",", ":"))
        (run_dir / "retry-cases.json").write_text(payload + "\n", encoding="utf-8")
        if github_output:
            with Path(github_output).open("a", encoding="utf-8") as output:
                output.write(f"retry_cases={payload}\n")
    if not any(results_dir.glob("*-result.json")):
        if allow_test_failures and result == pytest.ExitCode.OK:
            print("本轮未生成 Allure 原始结果", file=sys.stderr)
            return 1
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
        if allow_test_failures and result == pytest.ExitCode.TESTS_FAILED:
            if github_output:
                with Path(github_output).open("a", encoding="utf-8") as output:
                    output.write("test_outcome=failure\n")
            print("场景执行完毕，失败用例已记录在报告中")
            return 0
    elif allow_test_failures and report == 0:
        print("本轮未生成 Allure HTML 报告", file=sys.stderr)
        return result or 1
    return result or report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="运行接口场景并生成 Allure 报告")
    parser.add_argument("--env", choices=BASE_URLS, help="指定运行环境，默认 preview")
    parser.add_argument("--trade-run", action="append", type=Path, default=[],
                        help="加入已批准的 Preview 交易参数 JSON，可重复传入")
    parser.add_argument("--retry-cases", type=Path, help="只补跑文件中记录的首轮失败用例")
    parser.add_argument("--dispute-market-id", type=int, help="指定 Preview 自营市场并单独运行争议回归")
    parser.add_argument("--dispute-no-position-market-id", type=int, help="指定 CI 账号无持仓的 Preview 争议市场")
    parser.add_argument("--disputed-market-id", type=int, help="指定 Preview 争议中的市场并验证再次发起的资格")
    parser.add_argument(
        "--allow-test-failures", action="store_true",
        help="用例失败时，报告生成成功后以成功状态退出",
    )
    args = parser.parse_args()
    if args.env:
        os.environ["BUZZING_ENV"] = args.env
    try:
        sys.exit(main(allow_test_failures=args.allow_test_failures, trade_runs=args.trade_run,
                      dispute_market_id=args.dispute_market_id, dispute_no_position_market_id=args.dispute_no_position_market_id,
                      disputed_market_id=args.disputed_market_id, retry_cases=args.retry_cases))
    except ValueError as error:
        sys.exit(str(error))
