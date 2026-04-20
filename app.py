import os
import re
import zipfile
import asyncio
import aiohttp
import pandas as pd
import numpy as np
import yfinance as yf
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import xml.etree.ElementTree as ET
from datetime import datetime
import streamlit as st
import sqlite3
import hashlib
import json
import time
from typing import Dict, Tuple, Any, Optional

# ==========================================
# [HARNESS CORE] 0. Hardcore Local Caching Engine
# 외부 의존성 없는 C-Level I/O 최적화 계층
# ==========================================
class HardcoreSQLiteCache:
    def __init__(self, db_path: str = "dart_ruthless_cache.db", ttl_sec: int = 604800):
        self.db_path = db_path
        self.ttl_sec = ttl_sec # 기본값: 7일
        self._init_db()

    def _init_db(self):
        # WAL 모드: 읽기/쓰기 락(Lock) 충돌 원천 차단
        with sqlite3.connect(self.db_path, isolation_level=None) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS api_cache (
                    cache_key TEXT PRIMARY KEY,
                    response_payload TEXT,
                    timestamp REAL
                )
            """)
            # 부팅 시 만료된 악성 캐시 찌꺼기 즉시 소각
            conn.execute("DELETE FROM api_cache WHERE timestamp < ?", (time.time() - self.ttl_sec,))

    def _generate_key(self, url: str, params: dict) -> str:
        # API Key는 보안 및 키 일관성을 위해 해시에서 무자비하게 배제
        safe_params = {k: v for k, v in params.items() if k != 'crtfc_key'}
        param_str = json.dumps(safe_params, sort_keys=True)
        return hashlib.sha256(f"{url}|{param_str}".encode('utf-8')).hexdigest()

    def get(self, url: str, params: dict) -> Optional[dict]:
        key = self._generate_key(url, params)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT response_payload, timestamp FROM api_cache WHERE cache_key = ?", (key,))
            row = cursor.fetchone()
            
            if row:
                if time.time() - row[1] < self.ttl_sec:
                    return json.loads(row[0]) # Cache Hit
                else:
                    cursor.execute("DELETE FROM api_cache WHERE cache_key = ?", (key,)) # TTL Expired
            return None # Cache Miss

    def set(self, url: str, params: dict, response_data: dict):
        key = self._generate_key(url, params)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO api_cache (cache_key, response_payload, timestamp) VALUES (?, ?, ?)",
                (key, json.dumps(response_data), time.time())
            )

# ==========================================
# [HARNESS CORE] 1. DART Data Access Object
# UI(Streamlit)와 완전히 격리된 순수 데이터 계층
# ==========================================
class DartCoreClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.summary_url = "https://opendart.fss.or.kr/api/fnlttSinglAcnt.json"
        self.full_url = "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json"
        self.corp_code_url = "https://opendart.fss.or.kr/api/corpCode.xml"
        self.reprt_codes = {'11013': 1, '11012': 2, '11014': 3, '11011': 4}
        self.cache_dir = "dart_cache"
        os.makedirs(self.cache_dir, exist_ok=True)
        self.acc_nm_pattern = re.compile(r'[\sⅠⅡI1제기]')

        # 엔진 마운트
        self.cache_engine = HardcoreSQLiteCache()

    # 방어적 프로그래밍 + L1 캐싱 파이프라인 적용
    async def _request(self, session: aiohttp.ClientSession, url: str, params: dict) -> dict:
        # 1. L1 Cache Intercept (네트워크를 타기 전에 디스크에서 탈취)
        cached_payload = self.cache_engine.get(url, params)
        if cached_payload:
            # 캐시 히트: 네트워크 I/O 즉시 우회
            return cached_payload

        # 2. Cache Miss: 물리적 네트워크 통신 (타임아웃 15초 방어)
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as res:
                if res.status != 200:
                    return {}
                
                data = await res.json()
                
                # 3. 데이터 무결성 검증 후 L1 Cache에 적재 (에러 코드는 캐싱하지 않음)
                if data and data.get('status') == '000':
                    self.cache_engine.set(url, params, data)
                    
                return data
        except asyncio.TimeoutError:
            return {}
        except Exception:
            return {}

    async def get_corp_code(self, session: aiohttp.ClientSession, user_input: str) -> Tuple[str, str, str]:
        corp_zip_path = os.path.join(self.cache_dir, "CORPCODE.zip")
        if not os.path.exists(corp_zip_path):
            async with session.get(self.corp_code_url, params={'crtfc_key': self.api_key}) as res:
                if res.status != 200:
                    raise ConnectionError("DART API 기업코드 다운로드 실패")
                with open(corp_zip_path, 'wb') as f:
                    f.write(await res.read())

        is_digit = user_input.isdigit()
        search_target = user_input.zfill(6) if is_digit else user_input.replace(" ", "").lower()
        
        # 메모리 누수 방지: XML 파싱 후 이전 sibling 제거
        with zipfile.ZipFile(corp_zip_path) as z:
            with z.open('CORPCODE.xml') as f:
                context = ET.iterparse(f, events=('end',))
                for event, elem in context:
                    if elem.tag == 'list':
                        s_code = elem.findtext('stock_code')
                        if s_code and s_code.strip():
                            s_code = s_code.strip()
                            c_name = elem.findtext('corp_name').strip()
                            c_code = elem.findtext('corp_code')
                            
                            if (is_digit and s_code == search_target) or (not is_digit and c_name.replace(" ", "").lower() == search_target):
                                elem.clear()
                                return c_code, s_code, c_name
                        elem.clear() # RAM 최적화: 참조 해제
        raise ValueError("일치하는 상장사를 찾을 수 없다.")

    def _clean_val(self, v: Any) -> float:
        if not v or v == '-': return 0.0
        try: return float(str(v).replace(',', '').strip())
        except ValueError: return 0.0

    async def fetch_quarter(self, session: aiohttp.ClientSession, corp_code: str, year: int, reprt_code: str, q_num: int):
        params = {'crtfc_key': self.api_key, 'corp_code': corp_code, 'bsns_year': str(year), 'reprt_code': reprt_code}
        data = await self._request(session, self.summary_url, params)
        if not data or data.get('status') != '000': 
            return year, q_num, np.nan, np.nan, np.nan, np.nan, np.nan
        
        df_list = data.get('list', [])
        def _extract(t_div):
            r, o, ni, a, e = np.nan, np.nan, np.nan, np.nan, np.nan
            for i in df_list:
                if i.get('fs_div') != t_div: continue
                acc = self.acc_nm_pattern.sub('', i.get('account_nm', ''))
                amt = self._clean_val(i.get('thstrm_amount'))
                add_amt = self._clean_val(i.get('thstrm_add_amount'))
                
                is_is = ('매출' in acc or '영업' in acc or '순이익' in acc) and '총' not in acc and '외' not in acc and '포괄' not in acc and '지배' not in acc
                
                val = amt
                if is_is:
                    if q_num == 3 and add_amt != 0: val = add_amt
                    elif q_num == 2 and add_amt != 0 and abs(add_amt) > abs(amt): val = add_amt
                
                if ('매출' in acc or '영업수익' in acc) and pd.isna(r): r = val
                elif '영업' in acc and ('이익' in acc or '손익' in acc or '손실' in acc) and pd.isna(o): o = val
                elif '순이익' in acc and pd.isna(ni): ni = val
                elif '자산총계' in acc and pd.isna(a): a = val
                elif '자본총계' in acc and pd.isna(e): e = val
            return r, o, ni, a, e

        rev, op, ni, ast, eqt = _extract('CFS')
        if pd.isna(rev) or pd.isna(op): 
            rev, op, ni, ast, eqt = _extract('OFS')
        return year, q_num, rev, op, ni, ast, eqt

    async def fetch_all_data(self, user_input: str, years: int):
        # 포트 고갈 방지 및 동시성 제어
        connector = aiohttp.TCPConnector(limit=20, limit_per_host=10)
        async with aiohttp.ClientSession(connector=connector) as session:
            corp_code, stock_code, corp_name = await self.get_corp_code(session, user_input)
            
            curr_year = datetime.now().year
            start_year = curr_year - years
            
            tasks = [
                self.fetch_quarter(session, corp_code, y, r_code, q)
                for y in range(start_year, curr_year + 1)
                for r_code, q in self.reprt_codes.items()
            ]
            
            results = await asyncio.gather(*tasks)
            df = pd.DataFrame(results, columns=['Year', 'Quarter', 'Rev_Cum', 'Op_Cum', 'NI_Cum', 'Assets', 'Equity'])
            
            # DataFrame 최적화 연산
            df.set_index(['Year', 'Quarter'], inplace=True)
            idx = pd.MultiIndex.from_product([range(start_year, curr_year + 1), [1, 2, 3, 4]], names=['Year', 'Quarter'])
            df = df.reindex(idx)
            
            # Groupby apply 대신 벡터화된 인덱싱으로 성능 향상
            for y in range(start_year, curr_year + 1):
                mask = df.index.get_level_values('Year') == y
                df.loc[mask, ['Rev_Cum', 'Op_Cum', 'NI_Cum']] = df.loc[mask, ['Rev_Cum', 'Op_Cum', 'NI_Cum']].interpolate(limit_direction='both')
                df.loc[mask, ['Assets', 'Equity']] = df.loc[mask, ['Assets', 'Equity']].ffill().bfill()
                    
            df = df.reset_index().dropna(subset=['Rev_Cum'])
            if df.empty: raise ValueError("DART 재무 데이터가 없다.")
            
            # 병목이었던 to_datetime 루프 제거. 벡터화 적용.
            df['Date'] = pd.to_datetime(df['Year'].astype(str) + '-' + (df['Quarter'] * 3).astype(str) + '-01') + pd.offsets.MonthEnd(0)
            df.set_index('Date', inplace=True)
            
            df['Rev'] = df.groupby('Year')['Rev_Cum'].diff().fillna(df['Rev_Cum'])
            df['Op'] = df.groupby('Year')['Op_Cum'].diff().fillna(df['Op_Cum'])
            df['NI'] = df.groupby('Year')['NI_Cum'].diff().fillna(df['NI_Cum'])
            
            df = df[df['Rev'] > 0].copy()
            df['OPM'] = (df['Op'] / df['Rev']) * 100
            df['YoY'] = df['Rev'].pct_change(periods=4) * 100 

            # 영업이익률(OPM)은 이미 % 단위이므로 변화량은 %p(포인트)로 계산(diff)한다.
            df['Rev_QoQ'] = df['Rev'].pct_change(periods=1) * 100
            df['OPM_QoQ_pp'] = df['OPM'].diff(periods=1)
            
            df['Rev_TTM'] = df['Rev'].rolling(window=4, min_periods=1).sum()
            df['NI_TTM'] = df['NI'].rolling(window=4, min_periods=1).sum()
            
            # 분모 0에 의한 NaN 처리 방어코드
            df['NI_Margin'] = np.where(df['Rev_TTM'] > 0, (df['NI_TTM'] / df['Rev_TTM']) * 100, 0) 
            df['Asset_Turnover'] = np.where(df['Assets'] > 0, df['Rev_TTM'] / df['Assets'], 0) 
            df['Leverage'] = np.where(df['Equity'] > 0, df['Assets'] / df['Equity'], 0) 
            df['ROE'] = np.where(df['Equity'] > 0, (df['NI_TTM'] / df['Equity']) * 100, 0) 
            
            display_start_year = start_year + 1
            stock = yf.download(f"{stock_code}.KS", start=f"{display_start_year}-01-01", progress=False)
            if stock.empty: stock = yf.download(f"{stock_code}.KQ", start=f"{display_start_year}-01-01", progress=False)
            if isinstance(stock.columns, pd.MultiIndex): stock.columns = stock.columns.get_level_values(0)
            
            return df[df['Year'] >= display_start_year], stock, corp_name, stock_code, corp_code

# ==========================================
# [HARNESS CORE] 2. Deterministic Inference Engine
# 상태 비저장(Stateless) 순수 함수. UI 로직 완전 배제.
# ==========================================
class RuthlessInferenceEngine:
    @staticmethod
    def analyze_dupont(df: pd.DataFrame) -> Dict[str, Any]:
        if len(df) < 4:
            return {"status": "error", "message": "데이터 부족. 추론 불가."}
            
        latest = df.iloc[-1]
        prev_year = df.iloc[-5] if len(df) >= 5 else df.iloc[0]
        
        roe = latest['ROE']
        margin = latest['NI_Margin']
        turnover = latest['Asset_Turnover']
        leverage = latest['Leverage']
        
        margin_trend = margin - prev_year['NI_Margin']
        turnover_trend = turnover - prev_year['Asset_Turnover']
        leverage_trend = leverage - prev_year['Leverage']

        # 종합 팩트 폭격
        synthesis = {}
        if roe > 15 and leverage < 2.0 and margin > 10:
            synthesis = {"level": "success", "title": f"[초우량 등급] 종합 ROE {roe:.1f}%", "desc": "부채(레버리지)에 의존하지 않고, 압도적인 마진과 자산 효율성으로 만들어낸 진짜 수익이다. 시스템 병목이 없는 완벽한 아키텍처다."}
        elif roe > 10 and leverage > 2.5:
            synthesis = {"level": "warning", "title": f"[주의 요망] 종합 ROE {roe:.1f}%", "desc": "겉보기엔 준수해 보이나, 속 빈 강정이다. 이익률이나 회전율의 결함을 빚(부채)으로 가리고 있다."}
        elif roe < 5 or margin < 0:
            synthesis = {"level": "error", "title": f"[투자 부적격] 종합 ROE {roe:.1f}%", "desc": "자본 비용(은행 이자)조차 못 건지는 상태다. 아키텍처가 무너진 스파게티 코드 같은 기업이다. 피하라."}
        else:
            synthesis = {"level": "info", "title": f"[무난/관망] 종합 ROE {roe:.1f}%", "desc": "치명적인 메모리 누수(악성 부채/재고)는 없으나, 시장을 압도할 만한 퍼포먼스도 보이지 않는 평범한 상태다."}

        # 세부 분석
        details = []
        if margin > 15:
            details.append(f"🟢 **압도적 마진율**: 순이익률 `{margin:.1f}%`. 강력한 독점적 해자(Moat).")
        elif margin_trend < -3:
            details.append(f"🔴 **수익성 훼손**: 순이익률 전년비 `{margin_trend:+.1f}%p` 급감. 판관비 통제 실패.")
        else:
            details.append(f"⚪ **마진율 평이**: 순이익률 `{margin:.1f}%`. 무난한 수익성 방어 중.")

        if turnover_trend < -0.1 and turnover < 0.5:
            details.append(f"🔴 **자산 비효율 경고**: 자산회전율 `{turnover:.2f}배` 우하향. 악성 재고 및 설비 유휴 가능성.")
        elif turnover > 1.0:
            details.append(f"🟢 **극한의 인프라 효율**: 자산회전율 `{turnover:.2f}배`. 1원의 유휴 자본도 없는 상태.")
        else:
            details.append(f"⚪ **자산 효율성 평이**: 자산회전율 `{turnover:.2f}배`. 정상 가동 중.")

        if leverage > 2.5:
            details.append(f"🔴 **부채 영끌 주의보**: 재무레버리지 `{leverage:.1f}배` 위험 수위. 빚으로 쌓은 모래성.")
        elif leverage_trend > 0.5 and roe > 10:
            details.append(f"🟡 **레버리지 주도 성장**: 부채 비중 최근 급증. 이자 보상 비율 점검 요망.")
        else:
            details.append(f"🟢 **재무 건전성 방어**: 재무레버리지 `{leverage:.1f}배`. 탄탄한 자본 구조.")

        return {
            "status": "success",
            "synthesis": synthesis,
            "details": details
        }

# ==========================================
# [PRESENTATION] Streamlit UI Layer
# 하네스 코어를 호출하고 화면에 뿌리기만 하는 바보(Dumb) 계층
# ==========================================
st.set_page_config(page_title="DART Financial Dashboard", layout="wide")

# Streamlit의 Event Loop 충돌을 막기 위한 안전한 Async Wrapper
def run_async_safe(coroutine):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        # Streamlit 스레드 내부에서 이미 이벤트 루프가 도는 경우를 대비한 꼼수가 아닌 정공법
        return asyncio.run_coroutine_threadsafe(coroutine, loop).result()
    else:
        return asyncio.run(coroutine)

@st.cache_data(ttl=86400, show_spinner=False)
def load_data(api_key, query, years):
    client = DartCoreClient(api_key)
    return run_async_safe(client.fetch_all_data(query, years))

# [UI 렌더링 함수 교체] app.py 하단의 render_dashboard 함수 전체를 덮어씌워라.

def render_dashboard():
    st.title("📊 DART 분석 대시보드 (Optimized Architecture)")
    
    default_api_key = ""
    try:
        if hasattr(st, "secrets") and "DART_API_KEY" in st.secrets:
            default_api_key = st.secrets["DART_API_KEY"]
    except Exception:
        pass

    with st.sidebar:
        st.header("설정")
        with st.form(key='search_form'):
            api_key = st.text_input("DART API Key", value=default_api_key, type="password")
            query = st.text_input("종목명 또는 코드", "심텍")
            years = st.slider("조회 기간(년)", 3, 20, 10)
            fetch_btn = st.form_submit_button("데이터 조회", use_container_width=True)

    if fetch_btn:
        if not api_key:
            st.error("API Key 누락. 인증 없는 요청은 네트워크 I/O 낭비다.")
            return
        
        st.cache_data.clear()
        
        with st.spinner("데이터 패치 및 연산 중..."):
            try:
                df, stock, corp_name, stock_code, corp_code = load_data(api_key, query, years)
                st.session_state.update({'df': df, 'stock': stock, 'corp_name': corp_name, 'stock_code': stock_code})
            except Exception as e:
                st.error(f"런타임 에러: {e}")
                return

    if 'df' in st.session_state:
        df = st.session_state['df']
        stock = st.session_state['stock']
        corp_name = st.session_state['corp_name']
        stock_code = st.session_state['stock_code']
        
        st.subheader(f"{corp_name} ({stock_code}) - {years}년 재무 및 주가 추이")
        
        fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.06, 
                            row_heights=[0.5, 0.25, 0.25], 
                            specs=[[{"secondary_y": True}], [{"secondary_y": True}], [{"secondary_y": True}]])

        # Row 1: 주가 및 실적
        fig.add_trace(go.Scatter(x=stock.index, y=stock['Close'], name="주가", line=dict(color="#2c3e50", width=1.5)), row=1, col=1, secondary_y=False)
        fig.add_trace(go.Bar(x=df.index, y=df['Rev']/1e8, name="분기 매출(억)", marker_color="rgba(231, 76, 60, 0.35)", marker_line_width=0), row=1, col=1, secondary_y=True)
        fig.add_trace(go.Scatter(x=df.index, y=df['Op']/1e8, name="분기 영업이익(억)", mode="lines+markers", marker=dict(symbol='square', size=6), line=dict(color="#f39c12", width=2)), row=1, col=1, secondary_y=True)

        # Row 2: YoY 지표
        fig.add_trace(go.Bar(x=df.index, y=df['YoY'], name="매출 YoY(%)", marker_color="rgba(52, 73, 94, 0.6)", marker_line_width=0), row=2, col=1, secondary_y=False)
        fig.add_trace(go.Scatter(x=df.index, y=df['OPM'], name="영업이익률(%)", mode="lines+markers", marker=dict(size=6), line=dict(color="#27ae60", width=2)), row=2, col=1, secondary_y=True)

        # Row 3: QoQ 지표
        fig.add_trace(go.Bar(x=df.index, y=df['Rev_QoQ'], name="매출 QoQ(%)", marker_color="rgba(142, 68, 173, 0.5)", marker_line_width=0), row=3, col=1, secondary_y=False)
        # [Architect Fix] OPM_QoQ_pp(%p)를 제거하고 YoY와 동일한 OPM(%) 절대값 트레이스로 갈아 끼웠다. 
        # 범례(Legend)가 두 개씩 뜨는 UI 공해를 막기 위해 showlegend=False 처리 완료.
        fig.add_trace(go.Scatter(x=df.index, y=df['OPM'], name="영업이익률(%)", mode="lines+markers", marker=dict(size=6), line=dict(color="#27ae60", width=2), showlegend=False), row=3, col=1, secondary_y=True)

        fig.update_layout(
            height=750, hovermode="x unified", template="plotly_white", font=dict(color="#2c3e50"), 
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, font=dict(color="#2c3e50")),
            margin=dict(l=40, r=40, t=60, b=40)
        )
        
        fig.update_yaxes(title_text="주가 (원)", tickformat=",", secondary_y=False, row=1, col=1, showgrid=False, rangemode="tozero")
        fig.update_yaxes(title_text="분기 실적 (억원)", tickformat=",", secondary_y=True, row=1, col=1, showgrid=True, gridcolor='rgba(0,0,0,0.1)', rangemode="tozero")
        
        fig.update_yaxes(title_text="매출 YoY (%)", secondary_y=False, row=2, col=1, showgrid=True, gridcolor='rgba(0,0,0,0.1)', zeroline=True, zerolinecolor='black')
        fig.update_yaxes(title_text="영업이익률 (%)", secondary_y=True, row=2, col=1, showgrid=False)
        
        fig.update_yaxes(title_text="매출 QoQ (%)", secondary_y=False, row=3, col=1, showgrid=True, gridcolor='rgba(0,0,0,0.1)', zeroline=True, zerolinecolor='black')
        # [Architect Fix] Y축 라벨 역시 OPM 증감에서 영업이익률로 동기화했다.
        fig.update_yaxes(title_text="영업이익률 (%)", secondary_y=True, row=3, col=1, showgrid=False)
        
        fig.update_xaxes(showgrid=True, gridcolor='rgba(0,0,0,0.1)', dtick="M12", tickformat="%Y년")

        st.plotly_chart(fig, use_container_width=True)

        st.divider()
        st.subheader("💡 듀퐁 분석 (ROE 분해 트리)")
        st.markdown("**ROE (자기자본이익률)** = **순이익률** (마진) × **총자산회전율** (자산 효율성) × **재무레버리지** (부채 활용도) *(※ TTM 후행 4분기 합산 기준)*")
        
        fig_dupont = make_subplots(rows=2, cols=2, shared_xaxes=True, vertical_spacing=0.1,
                                   subplot_titles=("TTM 분기 ROE (%)", "TTM 순이익률 (%)", "TTM 총자산회전율 (배수)", "기말 재무레버리지 (배수)"))

        fig_dupont.add_trace(go.Scatter(x=df.index, y=df['ROE'], fill='tozeroy', name="ROE", line=dict(color="#8e44ad", width=2)), row=1, col=1)
        fig_dupont.add_trace(go.Scatter(x=df.index, y=df['NI_Margin'], fill='tozeroy', name="순이익률", line=dict(color="#2980b9", width=2)), row=1, col=2)
        fig_dupont.add_trace(go.Scatter(x=df.index, y=df['Asset_Turnover'], fill='tozeroy', name="총자산회전율", line=dict(color="#d35400", width=2)), row=2, col=1)
        fig_dupont.add_trace(go.Scatter(x=df.index, y=df['Leverage'], fill='tozeroy', name="재무레버리지", line=dict(color="#c0392b", width=2)), row=2, col=2)

        fig_dupont.update_layout(height=500, showlegend=False, hovermode="x unified", template="plotly_white", margin=dict(l=40, r=40, t=40, b=40))
        fig_dupont.update_xaxes(showgrid=True, gridcolor='rgba(0,0,0,0.05)', dtick="M12", tickformat="%Y년")
        fig_dupont.update_yaxes(showgrid=True, gridcolor='rgba(0,0,0,0.05)', zeroline=True, zerolinecolor='black')

        st.plotly_chart(fig_dupont, use_container_width=True)

        st.divider()
        st.subheader("🧠 무자비한 AI 추론 결과")
        
        inference_result = RuthlessInferenceEngine.analyze_dupont(df)
        
        if inference_result['status'] == 'error':
            st.error(inference_result['message'])
        else:
            synth = inference_result['synthesis']
            getattr(st, synth['level'])(f"**{synth['title']}**\n{synth['desc']}")
            
            st.markdown("---")
            st.markdown("**🔍 세부 모듈 스캔 결과**")
            for detail in inference_result['details']:
                st.markdown(detail)

if __name__ == "__main__":
    render_dashboard()