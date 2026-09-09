"""finviz 맵(All Stocks / Market Cap)을 헤드리스 크롬으로 캡처해 텔레그램으로 전송.

GitHub Actions 배치용(매일 07:00 KST = 22:00 UTC). finviz 맵은 정적 이미지가
아니라 페이지에서 canvas로 그려지므로, 실제 브라우저로 렌더링한 뒤 map canvas를
스크린샷으로 뜬다. 덕분에 URL의 날짜/시간값을 알 필요 없이 '항상 최신 맵'을 얻는다.
(맵 안에 'as of ... ET' 시각이 함께 그려져 있어 이미지만으로 시점 확인 가능)

환경변수(=GitHub Secrets):
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

로컬/CI 설치:
    pip install playwright requests
    python -m playwright install --with-deps chromium
"""
import os
import sys
import requests
from playwright.sync_api import sync_playwright

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# 보낼 맵: (표시이름, finviz map 페이지 URL)
MAPS = [
    ("All Stocks", "https://finviz.com/map?t=sec_all&st=d1"),
    ("Market Cap", "https://finviz.com/map?t=cap&st=d1"),
]

# CI 러너용 크롬 실행 플래그(컨테이너에서 sandbox 비활성 필수)
LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
]

# 쿠키/동의 배너가 뜨면 눌러볼 후보(있을 때만, best-effort)
CONSENT_SELECTORS = [
    "#onetrust-accept-btn-handler",
    'button:has-text("Accept all")',
    'button:has-text("Accept")',
    'button:has-text("AGREE")',
    'button:has-text("I Agree")',
    'button:has-text("Consent")',
]

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def capture(page, page_url, render_wait_ms=3500, timeout_ms=45000):
    """map 페이지를 열어 map canvas(canvas.chart)를 PNG bytes로 반환."""
    page.goto(page_url, wait_until="networkidle", timeout=timeout_ms)
    for sel in CONSENT_SELECTORS:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=600):
                btn.click(timeout=800)
                page.wait_for_timeout(300)
                break
        except Exception:
            pass
    page.wait_for_selector("canvas.chart", timeout=timeout_ms)
    page.wait_for_timeout(render_wait_ms)   # 그리기 여유
    return page.locator("canvas.chart").screenshot()


def send_photo(png_bytes, caption):
    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    resp = requests.post(
        api,
        data={"chat_id": CHAT_ID, "caption": caption},
        files={"photo": ("finviz_map.png", png_bytes, "image/png")},
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Telegram HTTP {resp.status_code} - {resp.text}")


def main():
    if not BOT_TOKEN or not CHAT_ID:
        print("[finviz] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정", file=sys.stderr)
        sys.exit(1)

    failures = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=LAUNCH_ARGS)
        try:
            ctx = browser.new_context(
                viewport={"width": 1440, "height": 900},
                device_scale_factor=2,
                user_agent=USER_AGENT,
            )
            for label, url in MAPS:
                try:
                    page = ctx.new_page()
                    png = capture(page, url)
                    page.close()
                    send_photo(png, f"📊 Finviz 맵 ({label}, 1D)")
                    print(f"  → 전송 완료: {label}")
                except Exception as e:
                    failures += 1
                    print(f"  전송 실패({label}): {e}", file=sys.stderr)
        finally:
            browser.close()

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
