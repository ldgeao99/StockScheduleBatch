"""영업이익 주간 컨센서스 상향/하향 폭 1% 이상 종목을 텔레그램으로 전송.

금요일 또는 그 이전 마지막 한국증시 영업일의 다음 날 00:00 KST.
--dry-run은 조회/출력만, --scheduled는 실행일 판정 후 실행한다.
"""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP

import requests
from market_holidays import load_calendar

URL = "https://comp.wisereport.co.kr/earnings/e1030001.aspx"
KST = timezone(timedelta(hours=9))
MIN_CHANGE = Decimal("1")
HISTORY_DIR = Path(__file__).resolve().parent / "logs" / "earnings_consensus"


def fetch_consensus_html():
    """일시적인 조회 실패만 재시도한다. 전송은 재시도하지 않는다."""
    delays = (5, 15)
    for attempt in range(1, 4):
        started = time.monotonic()
        response = None
        retryable = False
        print(f"[earnings] WiseReport 조회 {attempt}/3 시작", flush=True)
        try:
            response = requests.get(URL, timeout=30)
            retryable = response.status_code in (500, 502, 503, 504)
            response.raise_for_status()
            html = response.text
        except requests.RequestException as exc:
            if response is None:
                retryable = (
                    isinstance(exc, (requests.ConnectionError, requests.Timeout))
                    and not isinstance(exc, requests.exceptions.SSLError)
                )
            status = response.status_code if response is not None else "응답 없음"
            print(f"[earnings] WiseReport 조회 {attempt}/3 실패: "
                  f"HTTP {status}, {type(exc).__name__}, "
                  f"소요 {time.monotonic() - started:.2f}초", flush=True)
            if not retryable or attempt == 3:
                raise
        else:
            print(f"[earnings] WiseReport 조회 {attempt}/3 성공: "
                  f"HTTP {response.status_code}, "
                  f"소요 {time.monotonic() - started:.2f}초", flush=True)
            return html
        finally:
            if response is not None:
                response.close()
        delay = delays[attempt - 1]
        print(f"[earnings] {delay}초 후 조회 재시도", flush=True)
        time.sleep(delay)


def weekly_run_date(friday, is_session):
    """금요일부터 거슬러 올라가 첫 개장일을 반환한다."""
    if friday.weekday() != 4:
        raise ValueError("기준일은 금요일이어야 합니다.")
    day = friday
    for _ in range(60):
        if day.weekday() < 5 and is_session(day):
            return day
        day -= timedelta(days=1)
    raise RuntimeError("60일 이내 한국증시 영업일을 찾지 못했습니다.")


def scheduled_run_date(today):
    """수집 파일의 휴장을 우선 적용하고 XKRX 달력으로 누락을 보완."""
    import exchange_calendars as xcals

    friday = today + timedelta(days=(4 - today.weekday()) % 7)
    calendar = xcals.get_calendar(
        "XKRX", start=(friday - timedelta(days=90)).isoformat(),
        end=(friday + timedelta(days=7)).isoformat(),
    )
    collected = {}

    def is_session(day):
        if day.year not in collected:
            try:
                data = load_calendar("KR", day.year)
            except FileNotFoundError:
                data = {"holidays": []}
            collected[day.year] = {item["date"] for item in data["holidays"]}
        if day.isoformat() in collected[day.year]:
            return False
        # 달력 범위 밖/로딩 오류는 실패로 처리하고 전송하지 않는다.
        return calendar.is_session(day.isoformat())

    return weekly_run_date(friday, is_session)


def load_dotenv():
    path = Path(__file__).with_name(".env")
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def parse_rows(html, direction="up"):
    if direction not in ("up", "down"):
        raise ValueError("direction은 up 또는 down이어야 합니다.")
    match = re.search(r"var\s+changeTnd\s*=\s*(\[.*?\]);", html, re.S)
    if not match:
        raise ValueError("changeTnd 데이터가 없습니다. 페이지 구조를 확인하세요.")
    raw = re.sub(r"\bid\s*:\s*'([^']*)'", r'"id":"\1"', match.group(1))
    raw = re.sub(r"\bdest\s*:", '"dest":', raw)
    data = json.loads(raw)
    if len(data) != 4 or data[1].get("id") != "TB1":
        raise ValueError("영업이익 데이터 구조가 변경되었습니다.")
    rows = data[1]["dest"][0 if direction == "up" else 1]
    result = []
    for row in rows:
        if len(row) != 10:
            raise ValueError("영업이익 행의 열 개수가 변경되었습니다.")
        change = Decimal(row[7].replace(",", ""))
        profit = Decimal(row[5].replace(",", ""))
        if not change.is_finite() or not profit.is_finite():
            raise ValueError("유효하지 않은 영업이익 또는 변화율입니다.")
        # 표시 반올림 이전의 원본 값으로 1% 이상 판정.
        if (direction == "up" and change >= MIN_CHANGE) or (
                direction == "down" and change <= -MIN_CHANGE):
            result.append((row[1].split("|", 1)[1], profit, change))
    return sorted(result, key=lambda item: item[2], reverse=direction == "up")


def build_messages(rows, now=None, direction="up"):
    if direction not in ("up", "down"):
        raise ValueError("direction은 up 또는 down이어야 합니다.")
    label = "상향" if direction == "up" else "하향"
    threshold = "1%" if direction == "up" else "-1%"
    date = (now or datetime.now(KST)).astimezone(KST).strftime("%Y-%m-%d")
    header = (
        f"✅ 영업이익 추정치 {label}종목 정리({date} 조회)\n\n"
        f"- 1주 전 대비 추정치 {threshold} 이상 {label} 종목\n"
        "(기업명/ 예상 영업이익/ 전주대비)"
    )
    lines = []
    for name, profit, change in rows:
        amount = profit.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        percent = change.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        lines.append(f"{name}/ {amount:,.0f}억/ {percent:+.1f}%")
    if not lines:
        lines = ["해당 종목이 없습니다."]
    messages = []
    current = header
    for line in lines:
        # Telegram의 4096자 제한보다 여유 있게, 행 단위로 분할.
        if len(current) + len(line) + 1 > 3500:
            messages.append(current)
            current = header + "\n(계속)"
        current += "\n" + line
    messages.append(current + "\n\n출처: " + URL)
    footer = "\n\n- 매주 금요일 갱신, 휴일일경우 앞당겨 갱신"
    return [message + footer for message in messages]


def send_message(token, chat_id, message):
    # 예외에 봇 토큰이 포함된 URL을 노출하지 않는다.
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message,
                  "link_preview_options": {"is_disabled": True}},
            timeout=30,
        )
    except requests.RequestException:
        raise RuntimeError("Telegram 요청 실패") from None
    if response.status_code != 200:
        raise RuntimeError(f"Telegram HTTP {response.status_code}")
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError("Telegram 전송이 거부되었습니다.")
    return payload["result"]["message_id"]


def save_sent_message(message, message_id, sent_at=None):
    """성공한 메시지마다 UTF-8 파일 저장. 후속 전송 실패 시에도 기존 기록 유지."""
    sent_at = (sent_at or datetime.now(KST)).astimezone(KST)
    directory = HISTORY_DIR / sent_at.strftime("%Y-%m-%d")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{sent_at:%H%M%S_%f}_{message_id}.txt"
    with path.open("x", encoding="utf-8") as history:
        history.write(f"전송 시각(KST): {sent_at.isoformat()}\n"
                      f"Telegram message_id: {message_id}\n\n{message}\n")
    return path


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--scheduled", action="store_true",
                        help="한국증시 휴장에 맞춰 실행일을 판정")
    args = parser.parse_args()
    if args.scheduled:
        today = datetime.now(KST).date()
        target = scheduled_run_date(today - timedelta(days=1)) + timedelta(days=1)
        if today != target:
            print(f"[earnings] 오늘은 전송일이 아닙니다. 예정일: {target} 00:00 KST")
            return
    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not args.dry_run and (not token or not chat_id):
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정")
    html = fetch_consensus_html()
    up_rows = parse_rows(html, "up")
    down_rows = parse_rows(html, "down")
    now = datetime.now(KST)
    messages = (build_messages(up_rows, now, "up")
                + build_messages(down_rows, now, "down"))
    if not args.dry_run:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    for message in messages:
        if args.dry_run:
            print(message)
        else:
            message_id = send_message(token, chat_id, message)
            path = save_sent_message(message, message_id)
            print(f"[earnings] 전송 기록 저장: {path}")
    print(f"[earnings] 1% 이상 상향 {len(up_rows)}종목 / "
          f"1% 이상 하향 {len(down_rows)}종목, "
          f"{'미리보기 완료' if args.dry_run else '전송 완료'}")


if __name__ == "__main__":
    main()
