# StockScheduleCrawler

## 공용 증시 휴장 파일

휴장 수집 배치는 기존 Firestore 저장과 함께 `data/market_holidays/연도/KR.json`(한국),
`data/market_holidays/연도/US.json`(미국)을 생성합니다. 날짜, 거래소, 휴장 사유를 누적 저장하며
같은 일정은 중복 제거합니다. 날짜는 각 시장의 현지 날짜입니다.

Actions는 파일을 기본 브랜치에 자동 커밋하여 다른 배치가 checkout 후 읽을 수 있게 합니다.
기본 브랜치 보호 규칙이 봇의 push를 막으면 커밋 단계가 실패할 수 있으며, 파일은 실행 artifact에도 90일 보관됩니다.

```python
from market_holidays import load_calendar, has_collected_holiday

calendar = load_calendar("KR", 2026)
holiday_found = has_collected_holiday("US", "2026-12-25")
```

원본 페이지에서 수집한 범위만 포함하므로 `complete_year`는 `false`입니다.
연도별 파일이라고 연간 전체 휴장이 확보된 것은 아닙니다. 파일이 없으면 예외가 발생하고,
`has_collected_holiday`가 false여도 개장일이라고 단정할 수 없습니다. 주말/조기폐장 판정은 별도입니다.

## 영업이익 컨센서스 텔레그램 배치

- 화~토 00:00 KST (`0 15 * * 1-5` UTC)에 확인하고, 토요일 00:00에 전송합니다. 금요일이 한국증시 휴장일이면 직전 영업일의 다음 날 00:00에 전송합니다. 연속 휴장/주말/연도 경계를 거슬러 판정합니다.
- 한국 휴장 JSON에 있는 일정과 `exchange-calendars`의 XKRX 거래일 달력을 함께 사용합니다. 파일에 누락된 휴장은 XKRX로 보완하며, 달력 조회 실패 시 전송하지 않고 배치를 실패 처리합니다.
- WiseReport 주간컨센서스의 영업이익 원본 변화율이 +1% 이상인 상향 종목과 -1% 이하인 하향 종목을 별도 메시지로 전송합니다. 각 목록은 변화 폭이 큰 순서이며, 해당 종목이 없어도 안내 메시지를 전송합니다.
- WiseReport 조회는 GitHub runner에서 정상 수집이 확인된 `curl`을 사용합니다(`curl` 설치 필요, `ubuntu-latest`에는 기본 제공). 연결 오류·타임아웃·불완전 응답·HTTP 500/502/503/504 발생 시 5초, 15초 대기 후 최대 3회 시도합니다. 시도 횟수·소요 시간·HTTP 상태·curl 종료 코드를 로그에 남기며, 인증서 오류와 그 외 HTTP 오류는 즉시 실패합니다. TLS 검증을 유지하며 파싱 및 텔레그램 전송은 재시도하지 않습니다.
- 영업이익은 억원 단위 정수, 변화율은 소수점 한 자리로 반올림합니다.
- 메시지 날짜는 한국 시간 조회일이며, 자료 발표일을 의미하지 않습니다.
- 기존 GitHub Secrets `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`를 사용합니다.
- 수동 실행: Actions의 `earnings_consensus_telegram (Sat)` → Run workflow. 날짜/휴장 여부와 관계없이 최신 조회 결과를 즉시 전송하고 기록합니다. 예약 실행만 실행일 판정을 적용합니다.
- 날짜 판정 없이 수동 전송: `python earnings_consensus_telegram.py`. 예약 실행과 동일하게 확인: `python earnings_consensus_telegram.py --scheduled` (`exchange-calendars>=4.12,<5` 필요).
- 전송 없는 로컬 확인: `python earnings_consensus_telegram.py --dry-run` (`requests` 필요).
- 전송 성공한 메시지는 `logs/earnings_consensus/YYYY-MM-DD/시각_message_id.txt`에 UTF-8로 저장합니다. 전송 시각(KST)과 실제 전송 문구를 기록하며, 미리보기나 전송 실패는 기록하지 않습니다.
- Actions 실행에서는 기록을 `earnings-consensus-실행ID-시도번호` artifact로 업로드해 90일 보관합니다. 실행 상세 화면에서 다운로드할 수 있으며, 일부 메시지 전송 후 실패해도 성공한 메시지 기록은 업로드합니다.
- GitHub Actions 예약 실행은 지연될 수 있습니다. 워크플로우를 기본 브랜치에 반영해야 예약이 활성화됩니다.

### WiseReport 접속 장애 진단

- Actions의 `Diagnose WiseReport connection (no Telegram)`에서 수동 실행할 수 있습니다. 진단 스크립트 또는 해당 워크플로우를 `main`에 반영할 때도 자동 실행합니다.
- 동일한 GitHub runner에서 `requests`와 `curl`로 컨센서스 페이지 및 동일 호스트의 루트 페이지를 비교합니다. HTTP 상태, 소요 시간, 응답 크기, 실제 영업이익 데이터 파싱 가능 여부를 Actions 요약에 기록합니다.
- 진단에는 Telegram Secrets를 주입하지 않으며 메시지를 보내지 않습니다. TLS 검증을 유지하고 응답 본문·쿠키는 로그에 출력하지 않습니다.
- 워크플로우 성공 표시는 진단 완료를 뜻합니다. 접속 성공 여부는 요약의 HTTP 상태와 파싱 결과로 확인합니다. 두 도구 모두 실패하면 로컬과 비교하거나 사이트 측에 접근 정책을 확인해야 합니다.
- 조회 코드 변경 시 재시도 테스트와 실제 배치의 `--dry-run`도 자동 실행해 데이터 수집·메시지 생성까지 검증합니다. 이 단계가 실패하면 워크플로우도 실패합니다. Telegram 전송 성공은 별도로 확인해야 합니다.
