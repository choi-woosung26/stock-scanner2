import streamlit as st
import streamlit.components.v1
from tradingview_screener import Query, col
import pandas as pd
import yfinance as yf
import requests
import io
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

st.set_page_config(page_title="주식 스캐너 v4", page_icon="📈", layout="wide")

st.title("📈 한국 주식 종목 검색기")
st.markdown("""
**검색 조건**
- 📅 월봉 현재 캔들(0봉)에서 **MA10(10개월 이평선) 돌파**
- 📦 월봉 **10봉 평균 거래량의 300% 이상** 거래량
- ❌ 일봉 200일선보다 현재가가 **400% 이상 높으면 제외**
- 🚫 ETF · 스팩 · 우선주 · 거래정지 · 투자경고 · 관리종목 자동 제외
- 🔄 월봉 **MA10 < MA20** 또는 **MA20 < MA30** 역배열 조건 중 하나 충족
""")

# ── 사이드바 설정 ────────────────────────────────────────────────
st.sidebar.header("🔍 검색 설정")

min_vol_m = st.sidebar.number_input(
    "📦 최소 거래량 (하한선)",
    value=50000, step=10000,
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

max_workers = st.sidebar.slider(
    "⚡ 병렬 처리 수 (workers)",
    min_value=5, max_value=30, value=15, step=5,
    help="동시에 검증할 종목 수. 높을수록 빠르지만 네트워크 부하 증가."
)

# ── KRX 종목 정보 + 제재종목 로딩 ──────────────────────────────────
@st.cache_data(ttl=3600)
def load_krx_data():
    """
    KRX 공식 데이터포털 API로 종목 정보 로딩.
    반환: (name_map, exclude_set, sanction_codes)
      - name_map      : {코드6자리: 종목명}
      - exclude_set   : ETF·스팩·우선주 등 제외 코드 집합
      - sanction_codes: 거래정지·투자경고·관리종목 코드 집합
    """
    name_map     = {}
    exclude_set  = set()
    sanction_codes = set()

    # ── 1) 기본 종목 목록 (종목명·코드) ──────────────────────────
    try:
        url = "https://kind.krx.co.kr/corpgeneral/corpList.do"
        params  = {"method": "download", "searchType": "13"}
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, params=params, headers=headers, timeout=10)
        response.encoding = 'euc-kr'
        df = pd.read_html(io.StringIO(response.text))[0]
        df.columns = df.columns.str.strip()

        code_col = next((c for c in df.columns if '종목코드' in c or '코드' in c), None)
        name_col = next((c for c in df.columns if '회사명' in c or '종목명' in c or '기업명' in c), None)

        if code_col and name_col:
            df[code_col] = df[code_col].astype(str).str.zfill(6)
            name_map = dict(zip(df[code_col], df[name_col]))

            for _, row in df.iterrows():
                code = str(row[code_col]).zfill(6)
                name = str(row[name_col])

                # 우선주 제외 (코드 끝자리가 0이 아님)
                if not code.endswith('0'):
                    exclude_set.add(code)
                    continue

                exclude_keywords = ['스팩', 'SPAC', '리츠', 'REIT', '인프라', '환기',
                                     '수익증권', 'ETF', 'ETN', 'ELW']
                if any(kw in name.upper() for kw in exclude_keywords):
                    exclude_set.add(code)

    except Exception as e:
        st.warning(f"KRX 종목 목록 로딩 실패 ({e}). 이름 없이 진행합니다.")

    # ── 2) 투자유의 종목 (거래정지·투자경고·투자위험·관리종목 등) ──
    #    KRX 이상급등 + 투자유의 종목 목록
    sanction_urls = [
        # 관리종목
        {
            "url": "https://kind.krx.co.kr/investwarning/managementissue.do",
            "params": {"method": "searchManagementIssueSub", "marketType": "0"},
        },
        # 투자경고
        {
            "url": "https://kind.krx.co.kr/investwarning/investwarning.do",
            "params": {"method": "searchInvestWarningSub", "marketType": "0"},
        },
        # 거래정지
        {
            "url": "https://kind.krx.co.kr/investwarning/tradesuspend.do",
            "params": {"method": "searchTradeSuspendSub", "marketType": "0"},
        },
        # 불성실공시
        {
            "url": "https://kind.krx.co.kr/investwarning/unfaithfuldisclosure.do",
            "params": {"method": "searchUnfaithfulDisclosureSub", "marketType": "0"},
        },
    ]

    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://kind.krx.co.kr/"}

    for item in sanction_urls:
        try:
            resp = requests.get(item["url"], params=item["params"],
                                headers=headers, timeout=10)
            resp.encoding = 'euc-kr'
            tables = pd.read_html(io.StringIO(resp.text))
            if not tables:
                continue
            tbl = tables[0]
            tbl.columns = tbl.columns.str.strip()

            code_col = next(
                (c for c in tbl.columns if '종목코드' in c or '단축코드' in c or '코드' in c),
                None
            )
            if code_col is None:
                # 종목명으로 역매핑 시도
                name_col2 = next((c for c in tbl.columns if '종목명' in c or '회사명' in c), None)
                if name_col2:
                    rev_map = {v: k for k, v in name_map.items()}
                    for nm in tbl[name_col2].dropna():
                        cd = rev_map.get(str(nm).strip())
                        if cd:
                            sanction_codes.add(cd)
                continue

            tbl[code_col] = tbl[code_col].astype(str).str.zfill(6)
            for cd in tbl[code_col]:
                sanction_codes.add(cd)

        except Exception:
            continue

    return name_map, exclude_set, sanction_codes


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
                equity     = None

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
                    equity.index     = pd.to_datetime(equity.index)
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


# ── 재무 그래프 렌더링 (Chart.js HTML) ───────────────────────────
def render_financial_chart(name: str, code: str, op_series: pd.Series, debt_series: pd.Series):
    has_op   = not op_series.empty
    has_debt = not debt_series.empty

    if not has_op and not has_debt:
        st.warning(f"{name} — 재무 데이터를 가져올 수 없습니다.")
        return

    if has_op and has_debt:
        all_idx = sorted(set(op_series.index) | set(debt_series.index))
    elif has_op:
        all_idx = list(op_series.index)
    else:
        all_idx = list(debt_series.index)

    quarters  = all_idx
    op_vals   = [round(float(op_series[q]),  1) if (has_op   and q in op_series.index)   else None for q in quarters]
    debt_vals = [round(float(debt_series[q]),1) if (has_debt and q in debt_series.index) else None for q in quarters]

    op_colors = []
    for v in op_vals:
        if v is None:
            op_colors.append('#3a9e5f')
        elif v < 0:
            op_colors.append('#c0392b')
        else:
            op_colors.append('#3a9e5f')

    chart_id = f"chart_{code}"

    html = f"""
<div style="background:linear-gradient(160deg,#d4edda 0%,#e8f5e9 40%,#f0faf1 100%);
            border-radius:12px;padding:24px 28px 20px;font-family:'Malgun Gothic',sans-serif;
            position:relative;overflow:hidden;">
  <div style="position:absolute;top:0;left:0;right:0;height:50px;
              background:linear-gradient(180deg,rgba(255,255,255,0.5) 0%,transparent 100%);
              border-radius:12px 12px 60% 60%/12px 12px 28px 28px;"></div>
  <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;position:relative;">
    <div style="width:13px;height:13px;background:#2d6a3f;border-radius:2px;"></div>
    <span style="font-size:15px;font-weight:700;color:#1a3a24;">{name} ({code}) — 분기별 재무 추이</span>
  </div>

  <div style="display:flex;justify-content:space-between;font-size:11px;font-weight:700;
              margin-bottom:2px;padding:0 4px;position:relative;">
    <span style="color:#2d7a4a;">억원</span>
    <span style="color:#b05010;">%</span>
  </div>

  <div style="position:relative;height:320px;">
    <canvas id="{chart_id}" role="img"
      aria-label="{name} 분기별 영업이익과 부채비율 막대 차트">
      영업이익: {op_vals} / 부채비율: {debt_vals}
    </canvas>
  </div>

  <div style="display:flex;justify-content:center;gap:24px;margin-top:12px;font-size:12px;color:#444;">
    <span style="display:flex;align-items:center;gap:5px;">
      <span style="width:14px;height:11px;background:#3a9e5f;border-radius:2px;display:inline-block;"></span>
      영업이익 (억원)
    </span>
    <span style="display:flex;align-items:center;gap:5px;">
      <span style="width:14px;height:11px;background:#e07010;border-radius:2px;display:inline-block;"></span>
      부채비율 (%)
    </span>
  </div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script>
(function() {{
  const quarters  = {json.dumps(quarters)};
  const opVals    = {json.dumps(op_vals)};
  const debtVals  = {json.dumps(debt_vals)};
  const opColors  = {json.dumps(op_colors)};

  const ctx = document.getElementById('{chart_id}');
  if (!ctx) return;

  new Chart(ctx, {{
    data: {{
      labels: quarters,
      datasets: [
        {{
          type: 'bar',
          label: '영업이익',
          data: opVals,
          backgroundColor: opColors,
          borderRadius: 4,
          borderSkipped: false,
          borderWidth: 0,
          yAxisID: 'yLeft',
          order: 1,
        }},
        {{
          type: 'bar',
          label: '부채비율',
          data: debtVals,
          backgroundColor: '#e07010',
          borderRadius: 4,
          borderSkipped: 'bottom',
          borderWidth: 0,
          yAxisID: 'yRight',
          order: 2,
        }}
      ]
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{
          mode: 'index',
          intersect: false,
          callbacks: {{
            label: ctx => ctx.datasetIndex === 0
              ? '영업이익: ' + (ctx.raw !== null ? ctx.raw.toLocaleString() + '억원' : '-')
              : '부채비율: ' + (ctx.raw !== null ? ctx.raw.toFixed(1) + '%' : '-')
          }}
        }}
      }},
      scales: {{
        x: {{
          grid: {{ display: false }},
          ticks: {{
            color: '#fff',
            font: {{ size: 11, weight: '600' }},
            maxRotation: 0,
            autoSkip: false,
            padding: 4,
          }},
          border: {{ display: false }},
        }},
        yLeft: {{
          type: 'linear',
          position: 'left',
          ticks: {{
            color: '#2d7a4a',
            font: {{ size: 11 }},
            callback: v => v.toLocaleString(),
          }},
          grid: {{
            color: ctx => ctx.tick.value === 0 ? '#000000' : 'rgba(180,200,180,0.35)',
            lineWidth: ctx => ctx.tick.value === 0 ? 2 : 1,
          }},
          border: {{ display: false }},
          title: {{ display: false }},
        }},
        yRight: {{
          type: 'linear',
          position: 'right',
          min: 0,
          ticks: {{
            color: '#b05010',
            font: {{ size: 11 }},
            callback: v => v + '%',
          }},
          grid: {{ display: false }},
          border: {{ display: false }},
          title: {{ display: false }},
        }}
      }},
      layout: {{ padding: {{ top: 24, bottom: 0 }} }},
    }},
    plugins: [{{
      id: 'customDraw_{code}',
      afterDatasetsDraw(chart) {{
        const ctx = chart.ctx;
        const meta0 = chart.getDatasetMeta(0);
        const meta1 = chart.getDatasetMeta(1);
        const yLeft = chart.scales.yLeft;

        ctx.save();

        const zeroY = yLeft.getPixelForValue(0);
        ctx.beginPath();
        ctx.moveTo(chart.chartArea.left, zeroY);
        ctx.lineTo(chart.chartArea.right, zeroY);
        ctx.strokeStyle = '#000000';
        ctx.lineWidth = 2;
        ctx.stroke();

        ctx.beginPath();
        ctx.moveTo(chart.chartArea.left, chart.chartArea.top);
        ctx.lineTo(chart.chartArea.left, chart.chartArea.bottom);
        ctx.strokeStyle = '#000000';
        ctx.lineWidth = 1.5;
        ctx.stroke();

        ctx.beginPath();
        ctx.moveTo(chart.chartArea.right, chart.chartArea.top);
        ctx.lineTo(chart.chartArea.right, chart.chartArea.bottom);
        ctx.strokeStyle = '#000000';
        ctx.lineWidth = 1.5;
        ctx.stroke();

        ctx.font = "bold 10px 'Malgun Gothic', sans-serif";
        ctx.textAlign = 'center';

        opVals.forEach((val, i) => {{
          if (val === null) return;
          const el = meta0.data[i];
          ctx.fillStyle = val < 0 ? '#8a1a10' : '#1a5c30';
          const y = val < 0 ? el.y + 14 : el.y - 7;
          ctx.fillText(val.toLocaleString(), el.x, y);
        }});

        debtVals.forEach((val, i) => {{
          if (val === null) return;
          const el = meta1.data[i];
          ctx.fillStyle = '#8a3d00';
          ctx.fillText(val.toFixed(1) + '%', el.x, el.y - 7);
        }});

        const xScale = chart.scales.x;
        const yBottom = chart.chartArea.bottom;
        ctx.fillStyle = '#555555';
        ctx.fillRect(chart.chartArea.left, yBottom, chart.chartArea.width, 28);
        ctx.font = "bold 11px 'Malgun Gothic', sans-serif";
        ctx.fillStyle = '#ffffff';
        quarters.forEach((q, i) => {{
          const x = xScale.getPixelForValue(i);
          ctx.fillText(q, x, yBottom + 19);
        }});

        ctx.restore();
      }}
    }}]
  }});
}})();
</script>
"""
    st.components.v1.html(html, height=430, scrolling=False)


# ── TradingView 전체 종목 스캔 (페이지네이션) ──────────────────────
def run_tv_scanner_full():
    """
    주가·거래량 조건 없이 한국 전체 주식을 수집.
    TradingView 최대 1500개 한도를 활용하고,
    1500개가 채워지면 offset으로 추가 수집을 시도합니다.
    """
    all_rows = []
    offset   = 0
    batch    = 1500

    while True:
        try:
            result = (
                Query()
                .set_markets("korea")
                .select('name', 'close', 'volume', 'change', 'SMA200', 'price_52_week_high')
                .where(
                    col('type') == 'stock',
                )
                .offset(offset)
                .limit(batch)
                .get_scanner_data()
            )
            # 결과가 None이거나 언패킹 불가능한 경우 → 마지막 페이지 도달
            if result is None:
                break
            count, data = result
            if data is None or data.empty:
                break
            all_rows.append(data)
            fetched = len(data)
            # 가져온 수가 batch보다 적으면 마지막 페이지
            if fetched < batch:
                break
            offset += fetched
            time.sleep(0.5)    # 서버 부하 방지
        except TypeError:
            # get_scanner_data()가 None 반환 시 언패킹 오류 → 정상 종료
            break
        except Exception as e:
            st.warning(f"TradingView 수집 중단 (offset={offset}): {e}")
            break

    if not all_rows:
        return pd.DataFrame()
    return pd.concat(all_rows, ignore_index=True)


# ── 주가·거래량 1차 필터 (TradingView 결과에서 즉시 적용) ────────────
def apply_price_volume_filter(data: pd.DataFrame, min_price, max_price, min_vol_m) -> pd.DataFrame:
    mask = (
        (data['close'] >= min_price) &
        (data['close'] <= max_price) &
        (data['volume'] > min_vol_m)
    )
    return data[mask].copy()


# ── yfinance 월봉 조건 검증 (단일 종목) ──────────────────────────
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


# ── 병렬 월봉 검증 래퍼 ─────────────────────────────────────────
def check_one(row_tuple, vol_ratio_pct, ma200_excl_pct):
    """ThreadPoolExecutor에서 호출되는 단일 종목 검증 함수."""
    idx, row = row_tuple
    code = row['종목코드']
    result = check_monthly_conditions(code, vol_ratio_pct, ma200_excl_pct)
    return idx, row, result


# ── 차트 URL ─────────────────────────────────────────────────────
def get_chart_url(ticker_raw):
    symbol = ticker_raw if ":" in str(ticker_raw) else f"KRX:{ticker_raw}"
    return f"https://www.tradingview.com/chart/?symbol={symbol}"


# ── 메인 실행 ────────────────────────────────────────────────────
if st.button("🔍 종목 검색 시작", use_container_width=True):
    if min_price >= max_price:
        st.error("⚠️ 최소 금액이 최대 금액보다 작아야 합니다.")
    else:
        # ── STEP 1: KRX 종목 정보 + 제재종목 로딩 ─────────────────
        with st.spinner("📋 KRX 종목 정보 및 제재종목 로딩 중..."):
            name_map, exclude_set, sanction_codes = load_krx_data()

        all_excluded = exclude_set | sanction_codes
        st.info(f"🚫 사전 제외 목록: ETF·스팩·우선주 {len(exclude_set)}개 + "
                f"제재종목(거래정지·경고·관리 등) {len(sanction_codes)}개 = 총 {len(all_excluded)}개")

        # ── STEP 2: TradingView 전체 스캔 ─────────────────────────
        with st.spinner("🔍 TradingView 전체 종목 수집 중 (최대 1500개+)..."):
            data = run_tv_scanner_full()

        if data is None or data.empty:
            st.warning("⚠️ TradingView에서 종목을 가져오지 못했습니다.")
        else:
            total_tv = len(data)

            # ── STEP 3: 종목코드 추출 ──────────────────────────────
            data['종목코드'] = (
                data['name']
                .apply(lambda x: str(x).split(':')[-1])
                .str.zfill(6)
            )

            # ── STEP 4: 주가·거래량 필터 (즉시, TV 데이터 기반) ────
            data = apply_price_volume_filter(data, min_price, max_price, min_vol_m)
            after_price = len(data)

            # ── STEP 5: 제재종목 + ETF·스팩 제외 ──────────────────
            data = data[~data['종목코드'].isin(all_excluded)]
            after_sanction = len(data)

            # ── STEP 6: 종목명 매핑 + ETF 패턴 추가 제거 ──────────
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

            st.info(
                f"📊 수집: {total_tv}개 "
                f"→ 주가·거래량 필터: {after_price}개 "
                f"→ 제재·ETF 제외: {after_sanction}개 "
                f"→ ETF패턴 추가제거: {after_etf}개 "
                f"→ **월봉 조건 검증 시작** (병렬 {max_workers}workers)"
            )

            # ── STEP 7: 병렬 월봉 검증 ────────────────────────────
            progress_bar = st.progress(0)
            status_text  = st.empty()
            results      = []
            total        = len(data)
            done_count   = 0

            rows_list = list(data.iterrows())

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(check_one, row_tuple, vol_ratio, ma200_exclude_ratio): row_tuple
                    for row_tuple in rows_list
                }

                for future in as_completed(futures):
                    done_count += 1
                    progress_bar.progress(done_count / total)

                    try:
                        idx, row, (pass_ma10, pass_vol, sma200_ok, pass_inverse,
                                   ma10_val, avg_vol, curr_vol, sma200_val) = future.result()
                    except Exception:
                        status_text.text(f"⚡ [{done_count}/{total}] 검증 중...")
                        continue

                    code = row['종목코드']
                    name = row['종목명']
                    status_text.text(f"⚡ [{done_count}/{total}] {name}({code}) 검증 완료")

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

            # ── STEP 8: 결과 출력 ──────────────────────────────────
            if not results:
                st.warning("⚠️ 모든 조건을 만족하는 종목이 없습니다. 조건을 완화해 보세요.")
            else:
                st.success(f"✅ 최종 {len(results)}개 종목 발견!")
                result_df = pd.DataFrame(results)

                # 결과 테이블
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

                # TradingView 바로가기
                st.subheader("📊 트레이딩뷰 차트 바로가기")
                cols_ui = st.columns(5)
                for i, row in enumerate(results):
                    url   = get_chart_url(row['name_raw'])
                    label = row['종목명']
                    with cols_ui[i % 5]:
                        st.link_button(f"📈 {label}", url, use_container_width=True)

                # 재무 그래프 섹션
                st.divider()
                st.subheader("📉 종목별 분기 재무 추이 (영업이익 · 부채비율)")
                st.caption("yfinance 분기별 재무제표 기준 | 영업이익: 억 원 단위 | 부채비율 = 총부채 ÷ 자기자본 × 100")

                tab_labels = [f"{r['종목명']}" for r in results[:20]]
                tabs = st.tabs(tab_labels)

                for tab, row in zip(tabs, results[:20]):
                    with tab:
                        code = row['종목코드']
                        name = row['종목명']

                        with st.spinner(f"{name} 재무 데이터 조회 중..."):
                            op_series, debt_series = get_financial_history(code)

                        col1, col2 = st.columns(2)

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
                                    delta_color="inverse"
                                )
                            else:
                                st.metric("최근 분기 부채비율", "데이터 없음")

                        render_financial_chart(name, code, op_series, debt_series)

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
