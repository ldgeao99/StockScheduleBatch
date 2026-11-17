"""GitHub Actions 러너에서 DART 접속 조건별 진단. DB 접근 없음."""
import time

import requests

CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
URLS = [
    "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261110000253",
    "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261117000038",
    "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261029000504",
]


def probe(label, headers, rounds=2, gap=0.0):
    for round_no in range(rounds):
        for url in URLS:
            started = time.monotonic()
            try:
                res = requests.get(url, headers=headers, timeout=(5, 15))
                result = f"HTTP {res.status_code} {len(res.content)}B"
            except requests.RequestException as exc:
                result = f"실패 {type(exc).__name__}: {str(exc)[-90:]}"
            print(f"[{label}] r{round_no} {url[-14:]} -> {result} ({time.monotonic() - started:.1f}s)")
            time.sleep(gap)


probe("기본UA", {})
probe("크롬UA", {"User-Agent": CHROME_UA})
probe("크롬UA+브라우저헤더", {"User-Agent": CHROME_UA, "Accept": "text/html,application/xhtml+xml",
                          "Accept-Language": "ko-KR,ko;q=0.9", "Referer": "https://dart.fss.or.kr/"})
probe("기본UA-1초간격", {}, gap=1.0)
