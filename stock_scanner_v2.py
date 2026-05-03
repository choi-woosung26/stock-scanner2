import streamlit as st
from tradingview_screener import Query, col
import pandas as pd
import FinanceDataReader as fdr
import yfinance as yf
from datetime import datetime, timedelta

st.set_page_config(page_title="주식 스캐너 v2", page_icon="📈", layout="wide")

st.title("📈 한국 주식 종목 검색기 v2")
st.markdown("""
**검색 조건**
- 📅 월봉 현재 캔들(0봉)에서 **MA10(10개월 이평선) 돌파**
- 📦 월봉 **10봉 평균 거래량의 300% 이상** 거래량
- ❌ 일봉 200일선보다 현재가가 **400% 이상 높으면 제외**
- 🚫 ETF · 스팩 · 우선주 자동 제외
""")

# ── 사이드바 설정 ────────────────────────────────────────────────
st.sidebar.header("🔍 검색 설정")

min_vol_m = st.sidebar.number_input(
    "📦 월봉 최소 거래량 (절대값 하한)",
    value=100000, step=10000,
    help="월봉 평균 거래량 300% 조건에 더해 최소 거래량 하한선"
)

vol_ratio = st.sidebar.slider(
    "📊 월봉 평균 대비 거래량 배수 (%)",
    min_value=100, max_value=1000, value=300, step=50,
    help="10봉 평균 거래량의 몇 % 이상인 종목을 검색할지 설정합니다."
)

ma200_exclude_ratio = st.sidebar.slider(
    "❌ 200일선 대비 현재가 제외 기준 (%)",
    min_value=200, max_value=1000, value=400, step=50,
    help="현재가가 200일선보다 이 비율 이상 높으면 제외합니다."
)

st.sidebar.markdown("💰 **주가 범위 (원)**")
min_price = st.sidebar.number_input("최소 금액", value=2000, step=500, min_value=0)
max_price = st.sidebar.number_input("최대 금액", value=100000, step=1000, min_value=0)

# ── KRX 종목 정보 로딩 ───────────────────────────────────────────
@st.cache_data(ttl=3600)
def load_krx_name_map():
    try:
        df = fdr.StockListing('KRX')
        code_col = next((c for c in df.columns if c in ['Code', 'Symbol', '종목코드', 'code']), None)
        name_col = next((c for c in df.columns if c in ['Name', '종목명', 'name', '이름']), None)

        if code_col is None or name_col is None:
            st.warning(f"컬럼 탐지 실패. 실제 컬럼: {list(df.columns)}")
            return {}, set()

        df[code_col] = df[code_col].astype(str).str.zfill(6)
        name_map = dict(zip(df[code_col], df[name_col]))

        exclude_set = set()
        for _, row in df.iterrows():
            code = str(row[code_col]).zfill(6)
            name = str(row[name_col])

            # 코드 끝자리가 '0'이 아닌 경우 제외 (우선주 등)
            if not code.endswith('0'):
                exclude_set.add(code)
                continue

            # 이름 기반 키워드 제외
            exclude_keywords = [
                '스팩', 'SPAC', '리츠', 'REIT', '인프라', '환기',
                '수익증권', 'ETF', 'ETN', 'ELW'
            ]
            if any(kw in name.upper() for kw in exclude_keywords):
                exclude_set.add(code)

        return name_map, exclude_set
    except Exception as e:
        st.warning(f"종목 정보 로딩 실패: {e}")
        return {}, set()

# ── TradingView: 1차 후보 추출 ───────────────────────────────────
# 조건: stock 타입, 현재가 > SMA200(일봉), 주가 범위, 최소 거래량
# 월봉 MA10 돌파 / 월봉 거래량 300% 조건은 yfinance로 2차 검증
def run_tv_scanner(min_price, max_price, min_vol_m):
    try:
        count, data = (
            Query()
            .set_markets("korea")
            .select('name', 'close', 'volume', 'change', 'SMA200', 'price_52_week_high')
            .where(
                col('type') == 'stock',
                col('volume') > min_vol_m,
                col('close') >= min_price,
                col('close') <= max_price,
                col('close') > col('SMA200'),      # 일봉 200일선 위에 있는 종목
            )
            .limit(500)
            .get_scanner_data()
        )
        return data
    except Exception as e:
        st.error(f"TradingView 스캐너 오류: {e}")
        return pd.DataFrame()

# ── yfinance: 월봉 조건 검증 ────────────────────────────────────
def check_monthly_conditions(code_6, vol_ratio_pct, ma200_excl_pct):
    """
    Returns: (pass_ma10, pass_vol, current_close, ma10_val, avg_vol, curr_vol, sma200)
    pass_ma10  : 현재 월봉 close > MA10 AND 전월 close <= 전월 MA10  (돌파)
    pass_vol   : 현재 월봉 거래량 >= 10봉 평균 거래량 × vol_ratio_pct/100
    sma200_ok  : 현재가가 200일선보다 ma200_excl_pct% 이상 높지 않음 (제외 조건 통과)
    """
    ticker = f"{code_6}.KS"
    try:
        # 월봉 15개월치 (MA10 계산 + 전월 비교용)
        df_m = yf.download(ticker, period="15mo", interval="1mo",
                           auto_adjust=True, progress=False)
        if df_m is None or len(df_m) < 11:
            return False, False, False, None, None, None, None

        # 멀티인덱스 정리
        if isinstance(df_m.columns, pd.MultiIndex):
            df_m.columns = df_m.columns.get_level_values(0)

        df_m = df_m.dropna(subset=['Close', 'Volume'])
        if len(df_m) < 11:
            return False, False, False, None, None, None, None

        # MA10 계산 (월봉)
        df_m['MA10'] = df_m['Close'].rolling(10).mean()

        # 현재 캔들(0봉) = 마지막 행, 전월 = 마지막에서 2번째
        curr = df_m.iloc[-1]
        prev = df_m.iloc[-2]

        curr_close = float(curr['Close'])
        curr_ma10  = float(curr['MA10']) if not pd.isna(curr['MA10']) else None
        prev_close = float(prev['Close'])
        prev_ma10  = float(prev['MA10']) if not pd.isna(prev['MA10']) else None

        if curr_ma10 is None or prev_ma10 is None:
            return False, False, False, None, None, None, None

        # 돌파 조건: 현재 close > MA10 AND 전월 close <= 전월 MA10
        pass_ma10 = (curr_close > curr_ma10) and (prev_close <= prev_ma10)

        # 거래량 조건: 최근 10봉 평균 거래량의 vol_ratio_pct% 이상
        recent_vols = df_m['Volume'].iloc[-11:-1]   # 직전 10봉 평균
        avg_vol_10  = float(recent_vols.mean())
        curr_vol    = float(curr['Volume'])
        pass_vol    = curr_vol >= avg_vol_10 * (vol_ratio_pct / 100)

        # 일봉 200일선 대비 현재가 400% 초과 여부
        df_d = yf.download(ticker, period="300d", interval="1d",
                           auto_adjust=True, progress=False)
        sma200_ok = True
        sma200_val = None
        if df_d is not None and len(df_d) >= 200:
            if isinstance(df_d.columns, pd.MultiIndex):
                df_d.columns = df_d.columns.get_level_values(0)
            df_d = df_d.dropna(subset=['Close'])
            sma200_val = float(df_d['Close'].rolling(200).mean().iloc[-1])
            # 현재가가 SMA200의 (1 + ma200_excl_pct/100) 배 이상이면 제외
            sma200_ok = curr_close < sma200_val * (1 + ma200_excl_pct / 100)

        return pass_ma10, pass_vol, sma200_ok, curr_ma10, avg_vol_10, curr_vol, sma200_val

    except Exception:
        return False, False, False, None, None, None, None

# ── 차트 URL ─────────────────────────────────────────────────────
def get_chart_url(ticker_raw):
    symbol = ticker_raw if ":" in str(ticker_raw) else f"KRX:{ticker_raw}"
    return f"https://www.tradingview.com/chart/?symbol={symbol}"

# ── 메인 실행 ────────────────────────────────────────────────────
if st.button("🔍 종목 검색 시작", use_container_width=True):
    if min_price >= max_price:
        st.error("⚠️ 최소 금액이 최대 금액보다 작아야 합니다.")
    else:
        # STEP 1: KRX 종목 정보
        with st.spinner("📋 KRX 종목 정보 로딩 중..."):
            name_map, exclude_set = load_krx_name_map()

        # STEP 2: TradingView 1차 스캔
        with st.spinner("🔍 TradingView 1차 후보 추출 중..."):
            data = run_tv_scanner(min_price, max_price, min_vol_m)

        if data is None or data.empty:
            st.warning("⚠️ TradingView에서 조건에 맞는 종목이 없습니다.")
        else:
            # 종목코드 추출
            data['종목코드'] = (
                data['name']
                .apply(lambda x: str(x).split(':')[-1])
                .str.zfill(6)
            )

            # ETF·스팩·우선주 제외
            before_etf = len(data)
            data = data[~data['종목코드'].isin(exclude_set)]

            # 한글 종목명 매핑
            data['종목명'] = data['종목코드'].map(name_map)
            data['종목명'] = data.apply(
                lambda r: name_map.get(str(r['name']).split(':')[-1].zfill(6), str(r['name']).split(':')[-1])
                if pd.isna(r['종목명']) else r['종목명'],
                axis=1
            )

            # ETF 이름 2차 필터
            etf_pattern = r'ETF|ETN|KODEX|TIGER|RISE|ACE|KBSTAR|HANARO|ARIRANG|SOL|KOSEF'
            data = data[data['종목명'].notna()]
            data = data[~data['종목명'].str.contains(etf_pattern, case=False, na=False)]

            after_etf = len(data)
            st.info(f"📋 1차 후보: {before_etf}개 → ETF·스팩 제외 후: {after_etf}개 → 월봉 조건 검증 시작...")

            # STEP 3: yfinance 월봉 2차 검증
            progress_bar = st.progress(0)
            status_text  = st.empty()

            results = []
            total = len(data)

            for i, (_, row) in enumerate(data.iterrows()):
                code   = row['종목코드']
                name   = row['종목명']
                status_text.text(f"🔄 [{i+1}/{total}] {name}({code}) 월봉 검증 중...")
                progress_bar.progress((i + 1) / total)

                pass_ma10, pass_vol, sma200_ok, ma10_val, avg_vol, curr_vol, sma200_val = \
                    check_monthly_conditions(code, vol_ratio, ma200_exclude_ratio)

                # 모든 조건 통과
                if pass_ma10 and pass_vol and sma200_ok:
                    results.append({
                        '종목명':        name,
                        '종목코드':      code,
                        '현재가(원)':    row['close'],
                        '거래량':        row['volume'],
                        '등락률(%)':     row['change'],
                        '200일선':       row.get('SMA200', None),
                        '52주 신고가':   row.get('price_52_week_high', None),
                        '월봉MA10':      round(ma10_val, 0) if ma10_val else None,
                        '월봉평균거래량': int(avg_vol) if avg_vol else None,
                        '월봉거래량':    int(curr_vol) if curr_vol else None,
                        '거래량배수(x)': round(curr_vol / avg_vol, 2) if avg_vol and avg_vol > 0 else None,
                        'name_raw':      row['name'],
                    })

            progress_bar.empty()
            status_text.empty()

            if not results:
                st.warning("⚠️ 모든 조건을 만족하는 종목이 없습니다. 조건을 완화해 보세요.")
            else:
                st.success(f"✅ 최종 {len(results)}개 종목 발견!")

                result_df = pd.DataFrame(results)

                # 테이블 표시
                display_cols = ['종목명', '종목코드', '현재가(원)', '거래량', '등락률(%)',
                                '200일선', '월봉MA10', '월봉평균거래량', '월봉거래량', '거래량배수(x)', '52주 신고가']
                display_cols = [c for c in display_cols if c in result_df.columns]
                display = result_df[display_cols].copy()

                fmt = {
                    '현재가(원)':    '{:,.0f}',
                    '거래량':        '{:,.0f}',
                    '등락률(%)':     '{:+.2f}',
                    '200일선':       '{:,.0f}',
                    '월봉MA10':      '{:,.0f}',
                    '월봉평균거래량': '{:,.0f}',
                    '월봉거래량':    '{:,.0f}',
                    '거래량배수(x)': '{:.2f}x',
                    '52주 신고가':   '{:,.0f}',
                }
                st.dataframe(
                    display.style.format(fmt, na_rep="-"),
                    use_container_width=True,
                    hide_index=True
                )

                # 차트 바로가기 버튼
                st.subheader("📊 트레이딩뷰 차트 바로가기")
                cols_ui = st.columns(5)
                for i, row in enumerate(results):
                    url   = get_chart_url(row['name_raw'])
                    label = row['종목명']
                    with cols_ui[i % 5]:
                        st.link_button(f"📈 {label}", url, use_container_width=True)

st.divider()
st.caption("본 프로그램은 TradingView·KRX·Yahoo Finance 공개 데이터를 활용하며 투자 권유를 목적으로 하지 않습니다.")
