import os
from html import escape
from pathlib import Path
from xml.etree import ElementTree


RESULT_LABELS = {"passed": "通过", "failure": "失败", "error": "错误", "skipped": "跳过"}


def read_junit_results(reports_dir):
    files = list(reports_dir.glob("run-*/junit.xml"))
    if not files:
        return []

    try:
        junit_file = max(files, key=lambda path: path.stat().st_mtime_ns)
        root = ElementTree.parse(junit_file).getroot()
    except (OSError, ElementTree.ParseError):
        return []

    results = []
    for case in root.iter("testcase"):
        status = next(
            (tag for tag in ("error", "failure", "skipped") if case.find(tag) is not None),
            "passed",
        )
        results.append((case.get("name", "未命名用例"), status, case.get("time")))
    return results


def result_totals(results):
    counts = dict.fromkeys(RESULT_LABELS, 0)
    for _, status, _ in results:
        counts[status] += 1
    return " · ".join(f"{label} {counts[status]}" for status, label in RESULT_LABELS.items())


def main():
    environment = os.environ["BUZZING_ENV"]
    scenario_status = os.getenv("REPORT_OUTCOME")
    outcome = {"success": "通过", "failure": "失败"}.get(scenario_status, "场景未执行")
    results = read_junit_results(Path("reports")) if scenario_status in {"success", "failure"} else []
    lines = [f"## Buzzing 接口自动化 · {environment}", "", f"运行结果 **{outcome}**", ""]
    if results:
        lines.extend([
            f"**用例 {len(results)} 条** · {result_totals(results)}",
            "",
            "| 用例 | 结果 | 耗时（秒） |",
            "|---|---|---:|",
        ])
        for name, status, elapsed in results:
            name = escape(" ".join(name.split()), quote=False).replace("|", "&#124;")
            try:
                duration = f"{float(elapsed):.2f}"
            except (TypeError, ValueError):
                duration = "—"
            lines.append(f"| <code>{name}</code> | {RESULT_LABELS[status]} | {duration} |")
    else:
        lines.append("本轮暂无用例明细，请查看运行日志。")

    if report_url := os.getenv("REPORT_ARTIFACT_URL"):
        lines.extend(["", f"[下载完整 Allure 报告]({report_url})"])

    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
        summary.write("\n".join(lines) + "\n")
    print(f"在线摘要已生成，用例 {len(results)} 条。")


if __name__ == "__main__":
    main()
