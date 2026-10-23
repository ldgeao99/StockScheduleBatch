"""WiseReport 접속 진단. DB 접근 및 텔레그램 전송 없이 실행한다."""
import os
from pathlib import Path
import subprocess
import tempfile
import time

import requests

from earnings_consensus_telegram import URL, parse_rows


def inspect_html(html):
    try:
        up = parse_rows(html, "up")
        down = parse_rows(html, "down")
    except (ValueError, KeyError, IndexError, TypeError):
        return "영업이익 데이터 파싱 불가"
    return f"파싱 성공: 상향 {len(up)}개 / 하향 {len(down)}개"


def check_requests(url):
    started = time.monotonic()
    try:
        with requests.get(url, timeout=(10, 30)) as response:
            return (f"HTTP {response.status_code}; {len(response.content)} bytes; "
                    f"{inspect_html(response.text)}; "
                    f"{time.monotonic() - started:.2f}초")
    except requests.RequestException as exc:
        # 원문 예외 대신 유형만 기록해 프록시 정보 등 노출 방지.
        cause = exc
        causes = []
        while cause is not None and len(causes) < 5:
            causes.append(type(cause).__name__)
            cause = cause.__cause__ or cause.__context__
        return f"연결 실패: {' → '.join(causes)}; {time.monotonic() - started:.2f}초"


def check_curl(url):
    with tempfile.TemporaryDirectory() as directory:
        body = Path(directory) / "body.html"
        result = subprocess.run(
            ["curl", "--silent", "--show-error", "--location", "--retry", "0",
             "--connect-timeout", "10", "--max-time", "30",
             "--output", str(body), "--write-out", "%{http_code} %{time_total}", url],
            capture_output=True, text=True, timeout=40, check=False,
        )
        html = body.read_text(encoding="utf-8", errors="replace") if body.exists() else ""
        # stderr 및 페이지 본문은 인증 정보/쿠키가 포함될 수 있어 기록하지 않는다.
        return (f"exit {result.returncode}; HTTP/seconds {result.stdout.strip()}; "
                f"{body.stat().st_size if body.exists() else 0} bytes; {inspect_html(html)}")


def main():
    rows = []
    for label, url in (("컨센서스", URL), ("동일 호스트 루트", "https://comp.wisereport.co.kr/")):
        for client, check in (("requests", check_requests), ("curl", check_curl)):
            try:
                outcome = check(url)
            except (OSError, subprocess.TimeoutExpired) as exc:
                outcome = f"진단 도구 실패: {type(exc).__name__}"
            rows.append(f"| {label} | {client} | {outcome} |")
            print(f"[diagnostic] {label} / {client}: {outcome}", flush=True)
    report = (
        "## WiseReport 연결 진단\n\n"
        "텔레그램 전송 없이 동일 runner에서 비교한 결과입니다. TLS 검증은 유지합니다.\n\n"
        "| 대상 | 도구 | 결과 |\n|---|---|---|\n" + "\n".join(rows) + "\n\n"
        "두 도구 모두 실패하면 실행 환경/네트워크/사이트 측 정책을 추가 확인해야 합니다. "
        "curl만 성공하면 클라이언트별 요청 방식 차이를 조사합니다. "
        "HTTP 200만으로 정상 수집으로 판단하지 말고 파싱 성공 여부를 확인하세요. "
        "워크플로우 성공은 진단 완료를 뜻하며 접속 성공을 보장하지 않습니다.\n"
    )
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(report)


if __name__ == "__main__":
    main()
