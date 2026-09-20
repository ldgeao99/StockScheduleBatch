"""finviz 맵(All Stocks / Market Cap)을 헤드리스 크롬으로 캡처해 텔레그램으로 전송.

GitHub Actions 배치용(화~토 04:00 KST = 월~금 19:00 UTC). finviz 맵은 정적 이미지가
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

try:
    from google.cloud import firestore
except Exception:
    firestore = None
from crawler_schedule import update_my_schedule


def _load_dotenv():
    """스크립트와 같은 폴더의 .env를 읽어 환경변수로 채운다(이미 설정된 값은 유지).
    → GitHub Actions에선 Secrets(환경변수)가 우선, 로컬에선 .env로 실행 가능."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())   # 기존 환경변수 우선


_load_dotenv()
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# 파이어베이스 엔진 초기화(다른 배치와 동일 패턴). 키 없거나 실패 시 db=None → 기록만 생략.
FIREBASE_KEY_PATH = "stockcalender-13042-firebase-adminsdk-fbsvc-18b1748d9a.json"
db = None
if firestore and os.path.exists(FIREBASE_KEY_PATH):
    try:
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = FIREBASE_KEY_PATH
        db = firestore.Client(project="stockcalender-13042")
    except Exception as e:
        print(f"[finviz] Firestore 초기화 실패(로그/일정 기록 생략): {e}", file=sys.stderr)
logs_ref = db.collection("crawler_logs") if db else None

TASK_NAME = "[finviz_map_telegram] Finviz 시장 히트맵 텔레그램 전송"

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


# map canvas 위에 겹쳐 뜨는 finviz 안내 팝오버(Matrix/"Why Is It Moving")와
# Elite 업그레이드 모달을 숨긴다. 파란 팝오버는 div[role=dialog].bg-blue-500,
# Elite 모달은 native <dialog open>. (상단 배너/네비는 canvas 밖이라 캡처 안 됨)
_HIDE_POPUP_CSS = 'dialog[open], div[role="dialog"].bg-blue-500{display:none !important;}'


def _dismiss_popups(page):
    """안내 팝오버/모달을 CSS로 숨기고 열린 dialog는 닫는다(best-effort)."""
    try:
        page.add_style_tag(content=_HIDE_POPUP_CSS)
        page.evaluate("document.querySelectorAll('dialog').forEach(d=>{try{d.close()}catch(e){}})")
    except Exception:
        pass


def capture(page, page_url, render_wait_ms=5000, timeout_ms=45000):
    """map 페이지를 열어 map canvas(canvas.chart)를 PNG bytes로 반환."""
    # finviz map은 광고/지속 네트워크 요청이 있어 'networkidle'에 도달하지 못해
    # goto가 타임아웃난다. DOM 로드까지만 기다린 뒤, canvas 등장 + 렌더 여유로 처리.
    page.goto(page_url, wait_until="domcontentloaded", timeout=timeout_ms)
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
    _dismiss_popups(page)   # 안내 팝오버/Elite 모달 숨김(늦게 떠도 CSS로 커버)
    # 데이터 로딩이 잦아들도록 잠깐만 시도(도달 못 해도 무시 - 하드 실패 방지)
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(render_wait_ms)   # 그리기 여유
    _dismiss_popups(page)   # 렌더 대기 중 늦게 뜬 팝업 한 번 더 정리
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


def _log_result(status, sent, failed, message):
    """crawler_logs 에 실행 결과 기록(다른 배치와 동일 컬렉션/필드). best-effort."""
    if not logs_ref:
        return
    try:
        logs_ref.add({
            "timestamp": firestore.SERVER_TIMESTAMP,
            "status": status,
            "task_name": TASK_NAME,
            "added_count": sent,
            "failed_count": failed,
            "skipped_count": 0,
            "message": message,
        })
    except Exception as e:
        print(f"[finviz] 로그 기록 실패: {e}", file=sys.stderr)


def main():
    # 전송 성공/실패와 무관하게 '다음 실행 예정시간'을 crawler_schedules 에 기록
    update_my_schedule(db, __file__, display_name=TASK_NAME)

    if not BOT_TOKEN or not CHAT_ID:
        msg = "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정"
        print(f"[finviz] {msg}", file=sys.stderr)
        _log_result("FAILED", 0, len(MAPS), msg)
        sys.exit(1)

    sent = 0
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
                    sent += 1
                    print(f"  → 전송 완료: {label}")
                except Exception as e:
                    failures += 1
                    print(f"  전송 실패({label}): {e}", file=sys.stderr)
        finally:
            browser.close()

    status = "SUCCESS" if failures == 0 else "FAILED"
    _log_result(status, sent, failures,
                f"Finviz 맵 텔레그램 전송 - 성공 {sent}건 / 실패 {failures}건")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
