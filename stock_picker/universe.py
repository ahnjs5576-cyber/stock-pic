"""추천 대상 후보군: 코스피·미국 시가총액 상위 대형주.

목록은 후보군일 뿐이며, 실제 실행 시 시가총액 기준(config)으로 한 번 더 걸러낸다.
"""
from __future__ import annotations

from .models import Market, Stock

KOSPI_LARGE_CAPS: list[tuple[str, str]] = [
    ("005930.KS", "삼성전자"),
    ("000660.KS", "SK하이닉스"),
    ("373220.KS", "LG에너지솔루션"),
    ("207940.KS", "삼성바이오로직스"),
    ("005380.KS", "현대차"),
    ("000270.KS", "기아"),
    ("068270.KS", "셀트리온"),
    ("005490.KS", "POSCO홀딩스"),
    ("035420.KS", "NAVER"),
    ("035720.KS", "카카오"),
    ("051910.KS", "LG화학"),
    ("006400.KS", "삼성SDI"),
    ("105560.KS", "KB금융"),
    ("055550.KS", "신한지주"),
    ("086790.KS", "하나금융지주"),
    ("316140.KS", "우리금융지주"),
    ("138040.KS", "메리츠금융지주"),
    ("024110.KS", "기업은행"),
    ("012330.KS", "현대모비스"),
    ("028260.KS", "삼성물산"),
    ("032830.KS", "삼성생명"),
    ("000810.KS", "삼성화재"),
    ("003670.KS", "포스코퓨처엠"),
    ("066570.KS", "LG전자"),
    ("011070.KS", "LG이노텍"),
    ("015760.KS", "한국전력"),
    ("034020.KS", "두산에너빌리티"),
    ("012450.KS", "한화에어로스페이스"),
    ("064350.KS", "현대로템"),
    ("329180.KS", "HD현대중공업"),
    ("009540.KS", "HD한국조선해양"),
    ("042660.KS", "한화오션"),
    ("267260.KS", "HD현대일렉트릭"),
    ("011200.KS", "HMM"),
    ("017670.KS", "SK텔레콤"),
    ("030200.KS", "KT"),
    ("033780.KS", "KT&G"),
    ("096770.KS", "SK이노베이션"),
    ("010950.KS", "S-Oil"),
    ("034730.KS", "SK"),
    ("402340.KS", "SK스퀘어"),
    ("003550.KS", "LG"),
    ("010130.KS", "고려아연"),
    ("018260.KS", "삼성에스디에스"),
    ("009150.KS", "삼성전기"),
    ("047050.KS", "포스코인터내셔널"),
    ("323410.KS", "카카오뱅크"),
    ("259960.KS", "크래프톤"),
    ("352820.KS", "하이브"),
]

US_LARGE_CAPS: list[tuple[str, str]] = [
    ("AAPL", "Apple"), ("MSFT", "Microsoft"), ("NVDA", "NVIDIA"), ("AMZN", "Amazon"),
    ("GOOGL", "Alphabet"), ("META", "Meta Platforms"), ("TSLA", "Tesla"),
    ("AVGO", "Broadcom"), ("BRK-B", "Berkshire Hathaway"), ("LLY", "Eli Lilly"),
    ("JPM", "JPMorgan Chase"), ("V", "Visa"), ("MA", "Mastercard"), ("UNH", "UnitedHealth"),
    ("XOM", "Exxon Mobil"), ("WMT", "Walmart"), ("JNJ", "Johnson & Johnson"),
    ("PG", "Procter & Gamble"), ("HD", "Home Depot"), ("COST", "Costco"),
    ("ORCL", "Oracle"), ("ABBV", "AbbVie"), ("MRK", "Merck"), ("CVX", "Chevron"),
    ("KO", "Coca-Cola"), ("PEP", "PepsiCo"), ("BAC", "Bank of America"),
    ("NFLX", "Netflix"), ("CRM", "Salesforce"), ("AMD", "AMD"), ("ADBE", "Adobe"),
    ("TMO", "Thermo Fisher"), ("CSCO", "Cisco"), ("ACN", "Accenture"),
    ("MCD", "McDonald's"), ("ABT", "Abbott"), ("INTC", "Intel"), ("QCOM", "Qualcomm"),
    ("TXN", "Texas Instruments"), ("IBM", "IBM"), ("INTU", "Intuit"),
    ("AMGN", "Amgen"), ("PFE", "Pfizer"), ("NKE", "Nike"), ("DIS", "Disney"),
    ("CAT", "Caterpillar"), ("BA", "Boeing"), ("GE", "GE Aerospace"),
    ("UNP", "Union Pacific"), ("GS", "Goldman Sachs"), ("MS", "Morgan Stanley"),
    ("WFC", "Wells Fargo"), ("C", "Citigroup"), ("SBUX", "Starbucks"),
    ("PYPL", "PayPal"), ("UBER", "Uber"), ("NOW", "ServiceNow"), ("PLTR", "Palantir"),
    ("MU", "Micron"), ("AMAT", "Applied Materials"), ("LRCX", "Lam Research"),
    ("BMY", "Bristol-Myers Squibb"), ("CVS", "CVS Health"), ("T", "AT&T"),
    ("VZ", "Verizon"), ("LOW", "Lowe's"), ("TGT", "Target"), ("MDT", "Medtronic"),
    ("LMT", "Lockheed Martin"), ("DE", "Deere"),
]

BENCHMARKS: dict[Market, str] = {
    Market.KOSPI: "^KS11",   # 코스피 지수
    Market.US: "^GSPC",      # S&P 500
}

CURRENCY: dict[Market, str] = {Market.KOSPI: "KRW", Market.US: "USD"}


def get_universe(markets: list[Market]) -> list[Stock]:
    stocks: list[Stock] = []
    if Market.KOSPI in markets:
        stocks += [Stock(t, n, Market.KOSPI) for t, n in KOSPI_LARGE_CAPS]
    if Market.US in markets:
        stocks += [Stock(t, n, Market.US) for t, n in US_LARGE_CAPS]
    return stocks
