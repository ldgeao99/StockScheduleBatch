"""GitHub Actions 러너에서 DART 계열 호스트 접속 가능 여부 진단. DB 접근 없음."""
import socket
import time

import requests

TARGETS = [
    "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261110000258",
    "https://opendart.fss.or.kr/api/list.json?crtfc_key=0&page_count=1",
    "https://m.dart.fss.or.kr/",
]

try:
    print("러너 공인 IP 국가:", requests.get("https://ipinfo.io/country", timeout=10).text.strip())
except requests.RequestException as exc:
    print("러너 IP 확인 실패:", type(exc).__name__)

for url in TARGETS:
    host = url.split("/")[2]
    try:
        print(f"[{host}] DNS: {socket.gethostbyname(host)}")
    except OSError as exc:
        print(f"[{host}] DNS 실패: {exc}")
    started = time.monotonic()
    try:
        res = requests.get(url, timeout=(10, 20))
        print(f"[{host}] HTTP {res.status_code}, {len(res.content)} bytes, "
              f"{time.monotonic() - started:.1f}s, 본문: {res.text[:120]!r}")
    except requests.RequestException as exc:
        print(f"[{host}] 실패 {type(exc).__name__} ({time.monotonic() - started:.1f}s)")
