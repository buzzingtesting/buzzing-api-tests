import os
import shutil
import subprocess
from pathlib import Path
from tempfile import NamedTemporaryFile


PROJECT_NAME = "Buzzing 接口自动化"


def write_report_environment(results_dir, *, environment, base_url, data_group):
    (results_dir / "environment.properties").write_text(
        f"Environment={environment}\nAPI.Base.URL={base_url}\nTest.data={data_group}\n",
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
