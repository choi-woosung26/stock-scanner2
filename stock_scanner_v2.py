import streamlit as st
from tradingview_screener import Query, col
import pandas as pd
import yfinance as yf
import requests
import io
from pykrx import stock as krx_stock
import OpenDartReader
import datetime

st.set_page_config(page_title="주식 스캐너 v2", page_icon="📈", layout="wide")

st.title("📈 한국 주식 장기 역배열 검색기 v2")
st.markdown("""
**검색 조건**
- 📅 월봉 현재 캔들(0봉)에서 **MA10(10개월 이평선) 돌파**
- 📦 월봉 **10봉 평균 거래량의 300% 이상** 거래량
- ❌ 일봉 200일선보다 현재가가 **400% 이상 높으면 제외**
- 🚫 ETF · 스팩 · 우선주 자동 제외
- 🔄 월봉 **MA10 < MA20** 또는 **MA20 < MA30** 역배열 조건 중 하나 충족
""")

# ── 사이드바 설정 ────────────────────────────────────────────────
st.sidebar.header("🔍 검색 설정")

dart_api_key = st.sidebar.text_input(
    "🔑 DART API Key",
    type="password",
    help="https://opendart.fss.or.kr 에서 무료 발급. 없으면 재무 데이터가 생략됩니다."
)

min_vol_m = st.sidebar.number_input(
    "📦 최소 거래량 (하한선)",
    value=10000, step=10000,
    help="월봉 평균 거래량 300% 조건에 더해 최소 거래량 하한선"
)

vol_ratio = st.sidebar.slider(
    "📊 월봉 평균 대비 거래량 배수 이상 (%)",
    min_value=100, max_value=2000, value=300, step=50,
)

ma200_exclude_ratio = st.sidebar.slider(
    "❌ 200일선 대비 현재가 제외 기준 (%)",
    min_value=30, max_value=1000, value=100, step=50,
)

st.sidebar.markdown("💰 **주가 범위 (원)**")
min_price = st.sidebar.number_input("최소 금액", value=2000, step=500, min_value=0)
max_price = st.sidebar.number_input("최대 금액", value=30000, step=1000, min_value=0)

# ── KRX 종목 정보 로딩 ─────────────────────────────────────────
@st.cache_data(ttl=3600)
def load_krx_name_map():
    try:
        url = "https://kind.krx.co.kr/corpgeneral/corpList.do"
        params = {"method": "download", "searchType": "13"}
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, params=params, headers=headers, timeout=10)
        response.encoding = 'euc-kr'

        df = pd.read_html(io.StringIO(response.text))[0]
        df.columns = df.columns.str.strip()

        code_col = next((c for c in df.columns if '종목코드' in c or '코드' in c), None)
        name_col = next((c for c in df.columns if '회사명' in c or '종목명' in c or '기업명' in c), None)

        if code_col is None or name_col is None:
            return {}, set()

        df[code_col] = df[code_col].astype(str).str.zfill(6)
        name_map = dict(zip(df[code_col], df[name_col]))

        exclude_set = set()
        for _, row in df.iterrows():
            code = str(row[code_col]).zfill(6)
            name = str(row[name_col])
            if not code.endswith('0'):
                exclude_set.add(code)
                continue
            exclude_keywords = ['스팩', 'SPAC', '리츠', 'REIT', '인프라', '환기',
                                 '수익증권', 'ETF', 'ETN', 'ELW']
            if any(kw in name.upper() for kw in exclude_keywords):
                exclude_set.add(code)

        return name_map, exclude_set

    except Exception as e:
        st.warning(f"KRX 종목 정보 로딩 실패 ({e}). 이름 없이 진행합니다.")
        return {}, set()

# ── TradingView 1차 스캔 ─────────────────────────────────────────
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
                col('close') > col('SMA200'),
            )
            .limit(500)
            .get_scanner_data()
        )
        return data
    except Exception as e:
        st.error(f"TradingView 스캐너 오류: {e}")
        return pd.DataFrame()

# ── yfinance 월봉 조건 검증 ──────────────────────────────────────
def check_monthly_conditions(code_6, vol_ratio_pct, ma200_excl_pct):
    for suffix in ['.KS', '.KQ']:
        ticker = f"{code_6}{suffix}"
        try:
            df_m = yf.download(ticker, period="40mo", interval="1mo",
                               auto_adjust=True, progress=False)
            if df_m is None or len(df_m) < 11:
                continue

            if isinstance(df_m.columns, pd.MultiIndex):
                df_m.columns = df_m.columns.get_level_values(0)

            df_m = df_m.dropna(subset=['Close', 'Volume'])
            if len(df_m) < 11:
                continue

            df_m['MA10'] = df_m['Close'].rolling(10).mean()
            df_m['MA20'] = df_m['Close'].rolling(20).mean()
            df_m['MA30'] = df_m['Close'].rolling(30).mean()

            curr = df_m.iloc[-1]
            prev = df_m.iloc[-2]

            curr_close = float(curr['Close'])
            curr_ma10  = float(curr['MA10']) if not pd.isna(curr['MA10']) else None
            prev_close = float(prev['Close'])
            prev_ma10  = float(prev['MA10']) if not pd.isna(prev['MA10']) else None

            if curr_ma10 is None or prev_ma10 is None:
                continue

            pass_ma10 = (curr_close > curr_ma10) and (prev_close <= prev_ma10)

            curr_ma20 = float(curr['MA20']) if not pd.isna(curr['MA20']) else None
            curr_ma30 = float(curr['MA30']) if not pd.isna(curr['MA30']) else None

            inv_ma10_ma20 = (curr_ma10 is not None and curr_ma20 is not None
                             and curr_ma10 < curr_ma20)
            inv_ma20_ma30 = (curr_ma20 is not None and curr_ma30 is not None
                             and curr_ma20 < curr_ma30)
            pass_inverse = inv_ma10_ma20 or inv_ma20_ma30

            recent_vols = df_m['Volume'].iloc[-11:-1]
            avg_vol_10  = float(recent_vols.mean())
            curr_vol    = float(curr['Volume'])
            pass_vol    = curr_vol >= avg_vol_10 * (vol_ratio_pct / 100)

            df_d = yf.download(ticker, period="300d", interval="1d",
                               auto_adjust=True, progress=False)
            sma200_ok  = True
            sma200_val = None
            if df_d is not None and len(df_d) >= 200:
                if isinstance(df_d.columns, pd.MultiIndex):
                    df_d.columns = df_d.columns.get_level_values(0)
                df_d = df_d.dropna(subset=['Close'])
                sma200_val = float(df_d['Close'].rolling(200).mean().iloc[-1])
                sma200_ok  = curr_close < sma200_val * (1 + ma200_excl_pct / 100)

            return pass_ma10, pass_vol, sma200_ok, pass_inverse, curr_ma10, avg_vol_10, curr_vol, sma200_val

        except Exception:
            continue

    return False, False, False, False, None, None, None, None

# ── 외국인/기관 매매 (pykrx) ─────────────────────────────────────
@st.cache_data(ttl=1800)
def get_investor_data(code_6):
    """
    최근 20거래일 외국인·기관 순매수 합계 반환
    반환: (외국인_순매수합계, 기관_순매수합계)  단위: 주
    """
    try:
        today = datetime.date.today()
        # 영업일 기준 약 30일 전 (주말·공휴일 여유 포함)
        from_date = (today - datetime.timedelta(days=45)).strftime("%Y%m%d")
        to_date   = today.strftime("%Y%m%d")

        df = krx_stock.get_market_trading_volume_by_investor(
            from_date, to_date, code_6
        )
        if df is None or df.empty:
            return None, None

        # 컬럼명이 한글로 들어옴: '외국인', '기관합계' 등
        # 순매수 = 매수 - 매도  (pykrx는 순매수 컬럼을 직접 제공)
        foreign_col = next((c for c in df.columns if '외국인' in c), None)
        inst_col    = next((c for c in df.columns if '기관' in c), None)

        foreign_net = int(df[foreign_col].sum()) if foreign_col else None
        inst_net    = int(df[inst_col].sum())    if inst_col    else None

        return foreign_net, inst_net

    except Exception:
        return None, None

# ── 재무 데이터 (OpenDartReader) ─────────────────────────────────
@st.cache_data(ttl=86400)
def get_financial_data(code_6, dart_api_key):
    """
    최근 연간 보고서에서 영업이익·부채비율 반환
    반환: (영업이익_억원, 부채비율_%)
    """
    if not dart_api_key:
        return None, None
    try:
        dart = OpenDartReader.OpenDartReader(dart_api_key)

        # 최근 사업연도 재무제표 (연결 우선, 없으면 개별)
        year = datetime.date.today().year - 1  # 직전 연도 (공시 시차)
        fs = dart.finstate(code_6, year, reprt_code='11011')  # 11011=사업보고서

        if fs is None or fs.empty:
            return None, None

        # 영업이익
        op_row = fs[fs['account_nm'].str.contains('영업이익', na=False)]
        op_income = None
        if not op_row.empty:
            val = op_row.iloc[0]['thstrm_amount']
            val = str(val).replace(',', '').replace(' ', '')
            op_income = round(int(val) / 1e8, 1)  # 원 → 억원

        # 부채비율 = 부채총계 / 자본총계 × 100
        debt_row   = fs[fs['account_nm'].str.contains('부채총계', na=False)]
        equity_row = fs[fs['account_nm'].str.contains('자본총계', na=False)]
        debt_ratio = None
        if not debt_row.empty and not equity_row.empty:
            debt   = int(str(debt_row.iloc[0]['thstrm_amount']).replace(',', ''))
            equity = int(str(equity_row.iloc[0]['thstrm_amount']).replace(',', ''))
            if equity != 0:
                debt_ratio = round(debt / equity * 100, 1)

        return op_income, debt_ratio

    except Exception:
        return None, None

# ── 차트 URL ─────────────────────────────────────────────────────
def get_chart_url(ticker_raw):
    symbol = ticker_raw if ":" in str(ticker_raw) else f"KRX:{ticker_raw}"
    return f"https://www.tradingview.com/chart/?symbol={symbol}"

# ── 메인 실행 ────────────────────────────────────────────────────
if st.button("🔍 종목 검색 시작", use_container_width=True):
    if min_price >= max_price:
        st.error("⚠️ 최소 금액이 최대 금액보다 작아야 합니다.")
    else:
        with st.spinner("📋 KRX 종목 정보 로딩 중..."):
            name_map, exclude_set = load_krx_name_map()

        with st.spinner("🔍 TradingView 1차 후보 추출 중..."):
            data = run_tv_scanner(min_price, max_price, min_vol_m)

        if data is None or data.empty:
            st.warning("⚠️ TradingView에서 조건에 맞는 종목이 없습니다.")
        else:
            data['종목코드'] = (
                data['name']
                .apply(lambda x: str(x).split(':')[-1])
                .str.zfill(6)
            )

            before_etf = len(data)
            if exclude_set:
                data = data[~data['종목코드'].isin(exclude_set)]

            data['종목명'] = data['종목코드'].map(name_map)
            data['종목명'] = data.apply(
                lambda r: name_map.get(str(r['name']).split(':')[-1].zfill(6),
                                       str(r['name']).split(':')[-1])
                if pd.isna(r['종목명']) else r['종목명'], axis=1
            )

            etf_pattern = r'ETF|ETN|KODEX|TIGER|RISE|ACE|KBSTAR|HANARO|ARIRANG|SOL|KOSEF'
            data = data[data['종목명'].notna()]
            data = data[~data['종목명'].str.contains(etf_pattern, case=False, na=False)]

            after_etf = len(data)
            st.info(f"📋 1차 후보: {before_etf}개 → ETF·스팩 제외 후: {after_etf}개 → 월봉 조건 검증 시작...")

            progress_bar = st.progress(0)
            status_text  = st.empty()
            results = []
            total = len(data)

            for i, (_, row) in enumerate(data.iterrows()):
                code = row['종목코드']
                name = row['종목명']
                status_text.text(f"🔄 [{i+1}/{total}] {name}({code}) 검증 중...")
                progress_bar.progress((i + 1) / total)

                pass_ma10, pass_vol, sma200_ok, pass_inverse, ma10_val, avg_vol, curr_vol, sma200_val = \
                    check_monthly_conditions(code, vol_ratio, ma200_exclude_ratio)

                if pass_ma10 and pass_vol and sma200_ok and pass_inverse:

                    # ── 외국인/기관 매매 ──────────────────────────
                    foreign_net, inst_net = get_investor_data(code)

                    # ── 재무 데이터 ───────────────────────────────
                    op_income, debt_ratio = get_financial_data(code, dart_api_key)

                    results.append({
                        '종목명':           name,
                        '종목코드':         code,
                        '현재가(원)':       row['close'],
                        '거래량':           row['volume'],
                        '등락률(%)':        row['change'],
                        '200일선':          row.get('SMA200', None),
                        '52주 신고가':      row.get('price_52_week_high', None),
                        '월봉MA10':         round(ma10_val, 0) if ma10_val else None,
                        '월봉평균거래량':    int(avg_vol) if avg_vol else None,
                        '월봉거래량':       int(curr_vol) if curr_vol else None,
                        '거래량배수(x)':    round(curr_vol / avg_vol, 2) if avg_vol and avg_vol > 0 else None,
                        '외국인순매수(주)': foreign_net,
                        '기관순매수(주)':   inst_net,
                        '영업이익(억)':     op_income,
                        '부채비율(%)':      debt_ratio,
                        'name_raw':         row['name'],
                    })

            progress_bar.empty()
            status_text.empty()

            if not results:
                st.warning("⚠️ 모든 조건을 만족하는 종목이 없습니다. 조건을 완화해 보세요.")
            else:
                st.success(f"✅ 최종 {len(results)}개 종목 발견!")
                result_df = pd.DataFrame(results)

                display_cols = [
                    '종목명', '종목코드', '현재가(원)', '거래량', '등락률(%)',
                    '200일선', '월봉MA10', '월봉평균거래량', '월봉거래량', '거래량배수(x)',
                    '52주 신고가',
                    '외국인순매수(주)', '기관순매수(주)',   # 신규
                    '영업이익(억)', '부채비율(%)',           # 신규
                ]
                display_cols = [c for c in display_cols if c in result_df.columns]
                display = result_df[display_cols].copy()

                fmt = {
                    '현재가(원)':       '{:,.0f}',
                    '거래량':           '{:,.0f}',
                    '등락률(%)':        '{:+.2f}',
                    '200일선':          '{:,.0f}',
                    '월봉MA10':         '{:,.0f}',
                    '월봉평균거래량':    '{:,.0f}',
                    '월봉거래량':       '{:,.0f}',
                    '거래량배수(x)':    '{:.2f}x',
                    '52주 신고가':      '{:,.0f}',
                    '외국인순매수(주)': '{:+,.0f}',
                    '기관순매수(주)':   '{:+,.0f}',
                    '영업이익(억)':     '{:,.1f}',
                    '부채비율(%)':      '{:.1f}%',
                }

                # 외국인/기관 순매수 양수=매수(초록), 음수=매도(빨강) 색상 강조
                def highlight_investor(val):
                    if pd.isna(val):
                        return ''
                    return 'color: #16a34a' if val > 0 else 'color: #dc2626' if val < 0 else ''

                styled = (
                    display.style
                    .format(fmt, na_rep="-")
                    .applymap(highlight_investor,
                              subset=[c for c in ['외국인순매수(주)', '기관순매수(주)']
                                      if c in display.columns])
                )
                st.dataframe(styled, use_container_width=True, hide_index=True)

                st.subheader("📊 트레이딩뷰 차트 바로가기")
                cols_ui = st.columns(5)
                for i, row in enumerate(results):
                    url   = get_chart_url(row['name_raw'])
                    label = row['종목명']
                    with cols_ui[i % 5]:
                        st.link_button(f"📈 {label}", url, use_container_width=True)

st.divider()
st.caption("본 프로그램은 TradingView·KRX·Yahoo Finance·DART 공개 데이터를 활용하며 투자 권유를 목적으로 하지 않습니다.")
