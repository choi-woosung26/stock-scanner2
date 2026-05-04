import streamlit as st
from tradingview_screener import Query, col
import pandas as pd
import yfinance as yf
import requests
import io
import plotly.graph_objects as go
from plotly.subplots import make_subplots

st.set_page_config(page_title="주식 스캐너 v3", page_icon="📈", layout="wide")

st.title("📈 한국 주식 종목 검색기 v3")
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

min_vol_m = st.sidebar.number_input(
    "📦 최소 거래량 (하한선)",
    value=100000, step=10000,
    help="월봉 평균 거래량 300% 조건에 더해 최소 거래량 하한선"
)

vol_ratio = st.sidebar.slider(
    "📊 월봉 평균 대비 거래량 배수 이상 (%)",
    min_value=100, max_value=2000, value=300, step=50,
    help="10봉 평균 거래량의 몇 % 이상인 종목을 검색할지 설정합니다."
)

ma200_exclude_ratio = st.sidebar.slider(
    "❌ 200일선 대비 현재가 제외 기준 (%)",
    min_value=30, max_value=1000, value=100, step=50,
    help="현재가가 200일선보다 이 비율 이상 높으면 제외합니다."
)

st.sidebar.markdown("💰 **주가 범위 (원)**")
min_price = st.sidebar.number_input("최소 금액", value=2000, step=500, min_value=0)
max_price = st.sidebar.number_input("최대 금액", value=30000, step=1000, min_value=0)

# ── KRX 종목 정보 로딩 ─────────────────────────────────────────
@st.cache_data(ttl=3600)
def load_krx_name_map():
    """KRX 공식 데이터포털 API로 종목 정보 로딩"""
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


# ── 재무 데이터 (영업이익 · 부채비율) ─────────────────────────────
@st.cache_data(ttl=3600, show_spinner=False)
def get_financial_history(code_6: str):
    """
    yfinance로 분기별 영업이익 및 부채비율(총부채/자기자본×100) 조회.
    최근 6개 분기 데이터를 반환합니다.
    반환: (op_series, debt_series)  각각 pd.Series (index=분기문자열)
    """
    for suffix in ['.KS', '.KQ']:
        ticker_str = f"{code_6}{suffix}"
        try:
            tk = yf.Ticker(ticker_str)

            # ── 영업이익 ──────────────────────────────────────
            inc = tk.quarterly_income_stmt
            op_series = pd.Series(dtype=float)
            if inc is not None and not inc.empty:
                for label in ['Operating Income', 'EBIT', 'Operating Revenue']:
                    if label in inc.index:
                        raw = inc.loc[label].dropna()
                        if not raw.empty:
                            raw.index = pd.to_datetime(raw.index)
                            raw = raw.sort_index()
                            op_series = raw.tail(6) / 1e8  # 억 원 단위
                            op_series.index = [d.strftime('%Y.%m') for d in op_series.index]
                            break

            # ── 부채비율 ──────────────────────────────────────
            bal = tk.quarterly_balance_sheet
            debt_series = pd.Series(dtype=float)
            if bal is not None and not bal.empty:
                total_liab = None
                equity = None

                for label in ['Total Liabilities Net Minority Interest', 'Total Liabilities']:
                    if label in bal.index:
                        total_liab = bal.loc[label].dropna()
                        break
                for label in ['Stockholders Equity', 'Total Equity Gross Minority Interest',
                               'Common Stock Equity']:
                    if label in bal.index:
                        equity = bal.loc[label].dropna()
                        break

                if total_liab is not None and equity is not None:
                    total_liab.index = pd.to_datetime(total_liab.index)
                    equity.index = pd.to_datetime(equity.index)
                    common_idx = total_liab.index.intersection(equity.index).sort_values()
                    if len(common_idx) > 0:
                        ratio = (total_liab[common_idx] / equity[common_idx] * 100).dropna()
                        ratio = ratio.tail(6)
                        ratio.index = [d.strftime('%Y.%m') for d in ratio.index]
                        debt_series = ratio

            if not op_series.empty or not debt_series.empty:
                return op_series, debt_series

        except Exception:
            continue

    return pd.Series(dtype=float), pd.Series(dtype=float)


# ── 재무 그래프 렌더링 ────────────────────────────────────────────
def render_financial_chart(name: str, code: str, op_series: pd.Series, debt_series: pd.Series):
    """영업이익(억원)과 부채비율(%) 듀얼 축 차트"""
    has_op   = not op_series.empty
    has_debt = not debt_series.empty

    if not has_op and not has_debt:
        st.warning(f"**{name}** — 재무 데이터를 가져올 수 없습니다.")
        return

    fig = make_subplots(
        rows=1, cols=1,
        specs=[[{"secondary_y": True}]],
    )

    # 영업이익 (막대)
    if has_op:
        colors = ['#EF4444' if v < 0 else '#3B82F6' for v in op_series.values]
        fig.add_trace(
            go.Bar(
                x=op_series.index.tolist(),
                y=op_series.values.tolist(),
                name="영업이익 (억원)",
                marker_color=colors,
                opacity=0.85,
                text=[f"{v:,.0f}" for v in op_series.values],
                textposition='outside',
            ),
            secondary_y=False,
        )

    # 부채비율 (꺾은선)
    if has_debt:
        fig.add_trace(
            go.Scatter(
                x=debt_series.index.tolist(),
                y=debt_series.values.tolist(),
                name="부채비율 (%)",
                mode='lines+markers+text',
                line=dict(color='#F59E0B', width=2.5),
                marker=dict(size=8),
                text=[f"{v:.1f}%" for v in debt_series.values],
                textposition='top center',
            ),
            secondary_y=True,
        )

    fig.update_layout(
        title=dict(text=f"📊 {name} ({code}) — 분기별 재무 추이", font=dict(size=15)),
        height=380,
        legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='right', x=1),
        plot_bgcolor='rgba(0,0,0,0)',
        paper_bgcolor='rgba(0,0,0,0)',
        margin=dict(t=60, b=40, l=50, r=50),
        hovermode='x unified',
        bargap=0.35,
    )
    fig.update_xaxes(showgrid=False, title_text="분기")
    fig.update_yaxes(title_text="영업이익 (억원)", secondary_y=False,
                     showgrid=True, gridcolor='rgba(128,128,128,0.15)')
    fig.update_yaxes(title_text="부채비율 (%)", secondary_y=True,
                     showgrid=False)

    st.plotly_chart(fig, use_container_width=True, config={'displayModeBar': False})


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
                status_text.text(f"🔄 [{i+1}/{total}] {name}({code}) 월봉 검증 중...")
                progress_bar.progress((i + 1) / total)

                pass_ma10, pass_vol, sma200_ok, pass_inverse, ma10_val, avg_vol, curr_vol, sma200_val = \
                    check_monthly_conditions(code, vol_ratio, ma200_exclude_ratio)

                if pass_ma10 and pass_vol and sma200_ok and pass_inverse:
                    results.append({
                        '종목명':         name,
                        '종목코드':       code,
                        '현재가(원)':     row['close'],
                        '거래량':         row['volume'],
                        '등락률(%)':      row['change'],
                        '200일선':        row.get('SMA200', None),
                        '52주 신고가':    row.get('price_52_week_high', None),
                        '월봉MA10':       round(ma10_val, 0) if ma10_val else None,
                        '월봉평균거래량':  int(avg_vol) if avg_vol else None,
                        '월봉거래량':     int(curr_vol) if curr_vol else None,
                        '거래량배수(x)':  round(curr_vol / avg_vol, 2) if avg_vol and avg_vol > 0 else None,
                        'name_raw':       row['name'],
                    })

            progress_bar.empty()
            status_text.empty()

            if not results:
                st.warning("⚠️ 모든 조건을 만족하는 종목이 없습니다. 조건을 완화해 보세요.")
            else:
                st.success(f"✅ 최종 {len(results)}개 종목 발견!")
                result_df = pd.DataFrame(results)

                # ── 결과 테이블 ────────────────────────────────
                display_cols = ['종목명', '종목코드', '현재가(원)', '거래량', '등락률(%)',
                                '200일선', '월봉MA10', '월봉평균거래량', '월봉거래량',
                                '거래량배수(x)', '52주 신고가']
                display_cols = [c for c in display_cols if c in result_df.columns]
                display = result_df[display_cols].copy()

                fmt = {
                    '현재가(원)':     '{:,.0f}',
                    '거래량':         '{:,.0f}',
                    '등락률(%)':      '{:+.2f}',
                    '200일선':        '{:,.0f}',
                    '월봉MA10':       '{:,.0f}',
                    '월봉평균거래량':  '{:,.0f}',
                    '월봉거래량':     '{:,.0f}',
                    '거래량배수(x)':  '{:.2f}x',
                    '52주 신고가':    '{:,.0f}',
                }
                st.dataframe(
                    display.style.format(fmt, na_rep="-"),
                    use_container_width=True,
                    hide_index=True
                )

                # ── TradingView 바로가기 ───────────────────────
                st.subheader("📊 트레이딩뷰 차트 바로가기")
                cols_ui = st.columns(5)
                for i, row in enumerate(results):
                    url   = get_chart_url(row['name_raw'])
                    label = row['종목명']
                    with cols_ui[i % 5]:
                        st.link_button(f"📈 {label}", url, use_container_width=True)

                # ── 재무 그래프 섹션 ──────────────────────────
                st.divider()
                st.subheader("📉 종목별 분기 재무 추이 (영업이익 · 부채비율)")
                st.caption("yfinance 분기별 재무제표 기준 | 영업이익: 억 원 단위 | 부채비율 = 총부채 ÷ 자기자본 × 100")

                # 탭 생성 (최대 20개 탭)
                tab_labels = [f"{r['종목명']}" for r in results[:20]]
                tabs = st.tabs(tab_labels)

                for tab, row in zip(tabs, results[:20]):
                    with tab:
                        code = row['종목코드']
                        name = row['종목명']

                        with st.spinner(f"{name} 재무 데이터 조회 중..."):
                            op_series, debt_series = get_financial_history(code)

                        col1, col2 = st.columns(2)

                        # 최신값 요약 지표
                        with col1:
                            if not op_series.empty:
                                latest_op = op_series.iloc[-1]
                                delta_op  = op_series.iloc[-1] - op_series.iloc[-2] if len(op_series) >= 2 else None
                                st.metric(
                                    "최근 분기 영업이익",
                                    f"{latest_op:,.0f} 억원",
                                    delta=f"{delta_op:+,.0f} 억원" if delta_op is not None else None,
                                    delta_color="normal"
                                )
                            else:
                                st.metric("최근 분기 영업이익", "데이터 없음")

                        with col2:
                            if not debt_series.empty:
                                latest_debt = debt_series.iloc[-1]
                                delta_debt  = debt_series.iloc[-1] - debt_series.iloc[-2] if len(debt_series) >= 2 else None
                                st.metric(
                                    "최근 분기 부채비율",
                                    f"{latest_debt:.1f}%",
                                    delta=f"{delta_debt:+.1f}%" if delta_debt is not None else None,
                                    delta_color="inverse"   # 부채비율은 올라가면 나쁨
                                )
                            else:
                                st.metric("최근 분기 부채비율", "데이터 없음")

                        # 듀얼 축 차트
                        render_financial_chart(name, code, op_series, debt_series)

                        # 데이터 테이블 (토글)
                        with st.expander("📋 원본 수치 보기"):
                            fin_df = pd.DataFrame({
                                '분기':      op_series.index.tolist() if not op_series.empty else debt_series.index.tolist(),
                                '영업이익(억원)': op_series.values.tolist() if not op_series.empty else [None]*len(debt_series),
                                '부채비율(%)':   debt_series.reindex(
                                    op_series.index if not op_series.empty else debt_series.index
                                ).values.tolist() if not debt_series.empty else [None]*len(op_series),
                            })
                            st.dataframe(
                                fin_df.style.format({
                                    '영업이익(억원)': lambda v: f"{v:,.0f}" if v is not None else "-",
                                    '부채비율(%)':   lambda v: f"{v:.1f}%" if v is not None else "-",
                                }, na_rep="-"),
                                use_container_width=True,
                                hide_index=True
                            )

st.divider()
st.caption("본 프로그램은 TradingView·KRX·Yahoo Finance 공개 데이터를 활용하며 투자 권유를 목적으로 하지 않습니다.")

