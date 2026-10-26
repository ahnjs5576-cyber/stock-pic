# 대형주 급락 종목 추천 (stock_picker)

싸게 살 기회를 찾기 위한 서비스입니다. **코스피·미국 대형주** 중 **최근 고점 대비 20% 이상 하락한 종목**을 찾아,
**왜 빠졌는지**(사건이 있었는지, 시장 전체가 빠졌는지, 뚜렷한 이유가 없는지)를 함께 알려줍니다.

## 동작 방식

1. **대상**: 코스피·미국 시가총액 상위 종목 후보군(`stock_picker/universe.py`)에서
   시가총액 기준(코스피 5조원, 미국 100억 달러 이상)을 넘는 대형주만 남깁니다.
2. **하락 필터**: 최근 60거래일(약 3개월) 최고 종가 대비 현재가가 20% 이상 낮은 종목만 남깁니다.
3. **하락 원인 판별** (`stock_picker/analysis.py`)

   | 판정 | 기준 | 의미 |
   |---|---|---|
   | 시장 전반 하락 | 같은 기간 지수(코스피 / S&P 500)가 10% 이상 하락했고 종목의 초과 하락이 10%p 미만 | 시장 회복 시 동반 반등 가능 |
   | 사건성 하락 | 하루 8% 이상 급락한 날이 있거나, 고점 이후 뉴스 제목에 실적 쇼크·소송·규제·증자 등 키워드 | 사건이 기업 가치를 훼손했는지 확인 필요 |
   | 뚜렷한 이유 없는 하락 | 위 두 경우가 아님 | 수급·심리에 의한 하락일 수 있어 저가 매수 후보 |

   사건성 하락이면 어떤 뉴스가 근거인지(제목·날짜·링크·분류)를 함께 보여줍니다.
4. **(선택) AI 설명**: `--ai`를 켜면 Claude가 웹 검색으로 하락 원인을 확인해 몇 문장으로 정리합니다.

데이터는 yfinance(시세·시가총액·영문 뉴스)와 Google 뉴스 RSS(한국어 뉴스)를 사용합니다.

## 웹 화면

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py      # 브라우저에서 http://localhost:8501
```

시장·하락률·기간·하락 원인으로 거르고, 종목별로 하락 원인 설명·관련 뉴스·주가 차트를 볼 수 있습니다.

### 모의투자

화면 위쪽에서 **💼 모의투자**를 고르면 가상 원화 계좌(기본 1,000만원)로 거래할 수 있습니다.

- 추천 종목을 펼쳐서 바로 **모의 매수**할 수 있고, 모의투자 화면에서 후보 종목 아무거나 사고팔 수 있습니다.
- 최근 종가로 체결됩니다. 미국 주식은 그때의 원/달러 환율로 환산하며, 수수료·세금은 반영하지 않습니다.
- 총자산·수익률·평가손익·실현손익, 보유 종목별 수익률, 거래 내역을 보여줍니다.
- 왼쪽의 **계좌 이름**으로 계좌를 구분합니다. 같은 이름으로 들어오면 같은 계좌가 열리고, 로그인은 없습니다.
- 계좌는 서버의 `data/portfolios/`에 저장됩니다. 저장 위치는 `PAPER_DATA_DIR` 환경변수로 바꿀 수 있습니다.
  무료 서버는 재시작하면 파일이 지워질 수 있으니 **계좌 관리 → 백업 파일 내려받기**로 백업해 두세요.

### 인터넷에 올려서 링크로 보기 (Streamlit Community Cloud, 무료)

1. https://share.streamlit.io 에 GitHub 계정으로 로그인
2. **Create app** → **Deploy a public app from GitHub**
3. Repository `ahnjs5576-cyber/stock-pic`, Branch는 이 코드가 있는 브랜치, Main file path `streamlit_app.py` 입력 후 **Deploy**
4. (선택) AI 분석 버튼을 쓰려면 앱 **Settings → Secrets**에 `ANTHROPIC_API_KEY = "sk-ant-..."` 추가

배포가 끝나면 `https://<앱이름>.streamlit.app` 형태의 링크가 생깁니다.

## 명령줄 사용법

```bash
pip install -r requirements.txt

python -m stock_picker                    # 코스피 + 미국, 20% 이상 하락
python -m stock_picker --market kospi     # 코스피만
python -m stock_picker --market us --drop 30 --lookback 120
python -m stock_picker --json             # JSON 출력 (웹/앱 연동용)
ANTHROPIC_API_KEY=... python -m stock_picker --ai   # Claude로 하락 이유 설명 추가
```

## 테스트

```bash
pip install -r requirements-dev.txt
pytest
```

테스트는 가짜 데이터 제공자를 써서 네트워크 없이 돌아갑니다.

## 기준 바꾸기

`stock_picker/config.py`의 `ScreenConfig`에서 하락률, 기간, 대형주 시가총액 기준, 급락·시장 하락 판정 기준을 조정할 수 있습니다.
후보 종목은 `stock_picker/universe.py`에서 추가·삭제합니다.

> ※ 정보 제공용이며 투자 권유가 아닙니다. 하락한 주식이 반드시 반등하는 것은 아닙니다.
