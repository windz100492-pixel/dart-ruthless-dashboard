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
import requests
from bs4 import BeautifulSoup
import io
import traceback
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
import time

# ==========================================
# [HARNESS CORE] 0. Hardcore Local Caching Engine
# ==========================================
class HardcoreSQLiteCache:
    def __init__(self, db_path: str = "dart_ruthless_cache.db", ttl_sec: int = 604800):
        self.db_path = db_path
        self.ttl_sec = ttl_sec
        self._init_db()

    def _init_db(self):
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
            conn.execute("DELETE FROM api_cache WHERE timestamp < ?", (time.time() - self.ttl_sec,))

    def _generate_key(self, url: str, params: dict) -> str:
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
                    return json.loads(row[0])
                else:
                    cursor.execute("DELETE FROM api_cache WHERE cache_key = ?", (key,))
            return None

    def set(self, url: str, params: dict, response_data: dict):
        key = self._generate_key(url, params)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO api_cache (cache_key, response_payload, timestamp) VALUES (?, ?, ?)",
                (key, json.dumps(response_data), time.time())
            )

# ==========================================
# [HARNESS CORE] 1. DART Data Access Object
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
        self.cache_engine = HardcoreSQLiteCache()

    async def _request(self, session: aiohttp.ClientSession, url: str, params: dict) -> dict:
        cached_payload = self.cache_engine.get(url, params)
        if cached_payload: return cached_payload
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as res:
                if res.status != 200: return {}
                data = await res.json()
                if data and data.get('status') == '000':
                    self.cache_engine.set(url, params, data)
                return data
        except Exception:
            return {}

    async def get_corp_code(self, session: aiohttp.ClientSession, user_input: str) -> Tuple[str, str, str]:
        corp_zip_path = os.path.join(self.cache_dir, "CORPCODE.zip")
        if not os.path.exists(corp_zip_path):
            async with session.get(self.corp_code_url, params={'crtfc_key': self.api_key}) as res:
                if res.status != 200: raise ConnectionError("DART API 다운로드 실패")
                with open(corp_zip_path, 'wb') as f:
                    f.write(await res.read())

        is_digit = user_input.isdigit()
        search_target = user_input.zfill(6) if is_digit else user_input.replace(" ", "").lower()
        
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
                        elem.clear()
        raise ValueError("상장사를 찾을 수 없다.")

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
            
            df.set_index(['Year', 'Quarter'], inplace=True)
            idx = pd.MultiIndex.from_product([range(start_year, curr_year + 1), [1, 2, 3, 4]], names=['Year', 'Quarter'])
            df = df.reindex(idx)
            
            for y in range(start_year, curr_year + 1):
                mask = df.index.get_level_values('Year') == y
                df.loc[mask, ['Rev_Cum', 'Op_Cum', 'NI_Cum']] = df.loc[mask, ['Rev_Cum', 'Op_Cum', 'NI_Cum']].interpolate(limit_direction='both')
                df.loc[mask, ['Assets', 'Equity']] = df.loc[mask, ['Assets', 'Equity']].ffill().bfill()
                    
            df = df.reset_index().dropna(subset=['Rev_Cum'])
            if df.empty: raise ValueError("DART 재무 데이터가 없다.")
            
            df['Date'] = pd.to_datetime(df['Year'].astype(str) + '-' + (df['Quarter'] * 3).astype(str) + '-01') + pd.offsets.MonthEnd(0)
            df.set_index('Date', inplace=True)
            
            df['Rev'] = df.groupby('Year')['Rev_Cum'].diff().fillna(df['Rev_Cum'])
            df['Op'] = df.groupby('Year')['Op_Cum'].diff().fillna(df['Op_Cum'])
            df['NI'] = df.groupby('Year')['NI_Cum'].diff().fillna(df['NI_Cum'])
            
            df = df[df['Rev'] > 0].copy()
            df['OPM'] = (df['Op'] / df['Rev']) * 100
            df['YoY'] = df['Rev'].pct_change(periods=4) * 100 
            df['Rev_QoQ'] = df['Rev'].pct_change(periods=1) * 100
            
            df['Rev_TTM'] = df['Rev'].rolling(window=4, min_periods=1).sum()
            df['NI_TTM'] = df['NI'].rolling(window=4, min_periods=1).sum()
            
            df['NI_Margin'] = np.where(df['Rev_TTM'] > 0, (df['NI_TTM'] / df['Rev_TTM']) * 100, 0) 
            df['Asset_Turnover'] = np.where(df['Assets'] > 0, df['Rev_TTM'] / df['Assets'], 0) 
            df['Leverage'] = np.where(df['Equity'] > 0, df['Assets'] / df['Equity'], 0) 
            df['ROE'] = np.where(df['Equity'] > 0, (df['NI_TTM'] / df['Equity']) * 100, 0) 
            
            display_start_year = start_year + 1
            stock = yf.download(f"{stock_code}.KS", start=f"{display_start_year}-01-01", progress=False)
            if stock.empty: stock = yf.download(f"{stock_code}.KQ", start=f"{display_start_year}-01-01", progress=False)
            if isinstance(stock.columns, pd.MultiIndex): stock.columns = stock.columns.get_level_values(0)
            
            return df[df['Year'] >= display_start_year], stock, corp_name, stock_code, corp_code

class RuthlessInferenceEngine:
    @staticmethod
    def analyze_dupont(df: pd.DataFrame) -> Dict[str, Any]:
        if len(df) < 4: return {"status": "error", "message": "데이터 부족. 추론 불가."}
        latest = df.iloc[-1]
        prev_year = df.iloc[-5] if len(df) >= 5 else df.iloc[0]
        roe = latest['ROE']
        margin = latest['NI_Margin']
        turnover = latest['Asset_Turnover']
        leverage = latest['Leverage']
        margin_trend = margin - prev_year['NI_Margin']
        turnover_trend = turnover - prev_year['Asset_Turnover']
        leverage_trend = leverage - prev_year['Leverage']
        synthesis = {}
        if roe > 15 and leverage < 2.0 and margin > 10: synthesis = {"level": "success", "title": f"[초우량 등급] 종합 ROE {roe:.1f}%", "desc": "부채에 의존하지 않고, 압도적인 마진과 자산 효율성으로 만들어낸 진짜 수익이다."}
        elif roe > 10 and leverage > 2.5: synthesis = {"level": "warning", "title": f"[주의 요망] 종합 ROE {roe:.1f}%", "desc": "겉보기엔 준수해 보이나, 속 빈 강정이다. 이익률이나 회전율의 결함을 빚으로 가리고 있다."}
        elif roe < 5 or margin < 0: synthesis = {"level": "error", "title": f"[투자 부적격] 종합 ROE {roe:.1f}%", "desc": "자본 비용조차 못 건지는 상태다. 기업의 아키텍처가 무너졌다."}
        else: synthesis = {"level": "info", "title": f"[무난/관망] 종합 ROE {roe:.1f}%", "desc": "치명적인 누수는 없으나, 시장을 압도할 만한 퍼포먼스도 보이지 않는 평범한 상태다."}
        details = []
        if margin > 15: details.append(f"🟢 **압도적 마진율**: 순이익률 `{margin:.1f}%`.")
        elif margin_trend < -3: details.append(f"🔴 **수익성 훼손**: 순이익률 전년비 `{margin_trend:+.1f}%p` 급감.")
        else: details.append(f"⚪ **마진율 평이**: 순이익률 `{margin:.1f}%`.")
        if turnover_trend < -0.1 and turnover < 0.5: details.append(f"🔴 **자산 비효율 경고**: 자산회전율 `{turnover:.2f}배` 우하향.")
        elif turnover > 1.0: details.append(f"🟢 **극한의 인프라 효율**: 자산회전율 `{turnover:.2f}배`.")
        else: details.append(f"⚪ **자산 효율성 평이**: 자산회전율 `{turnover:.2f}배`.")
        if leverage > 2.5: details.append(f"🔴 **부채 영끌 주의보**: 재무레버리지 `{leverage:.1f}배` 위험 수위.")
        elif leverage_trend > 0.5 and roe > 10: details.append(f"🟡 **레버리지 주도 성장**: 부채 비중 최근 급증.")
        else: details.append(f"🟢 **재무 건전성 방어**: 재무레버리지 `{leverage:.1f}배`.")
        return {"status": "success", "synthesis": synthesis, "details": details}

# ==========================================
# [DATA LAYER] 셀레니움 기반 WiseReport 동기화 엔진 (V24 - The Time Lord)
# ==========================================
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
import time
import re
import pandas as pd
from bs4 import BeautifulSoup
import traceback

# ==========================================
# [DATA LAYER] 1. 글로벌 셀레니움 드라이버 (캐싱 엔진)
# ==========================================
@st.cache_resource(show_spinner=False)
def get_global_driver():
    """
    서버가 켜질 때 최초 1회만 백그라운드 크롬을 부팅하고,
    이후 접속부터는 이 살아있는 크롬 브라우저를 계속 돌려쓴다. (속도 비약적 향상)
    """
    options = Options()
    options.add_argument('--headless')
    options.add_argument('--disable-gpu')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    return driver

# ==========================================
# [DATA LAYER] 셀레니움 초고속 스마트 폴링 엔진 (V26 - Light Speed Polling)
# ==========================================
@st.cache_data(ttl=3600, show_spinner=False)
def fetch_wisereport_consensus(stock_code, baseline_rev=1e12):
    debug = ["🌐 Selenium: Global Chrome 재사용 중... (V26 초고속 폴링)"]
    
    try:
        driver = get_global_driver()
    except Exception as e:
        debug.append(f"💥 브라우저 초기화 실패: {e}")
        return [], "[실패] 브라우저 초기화 실패", debug

    try:
        url = f"https://navercomp.wisereport.co.kr/v2/company/c1010001.aspx?cmp_cd={stock_code}&finGubun=MAIN&frq=1"
        driver.get(url)

        # 1. 무조건적인 time.sleep 삭제. 요소가 나타나면 즉시 클릭한다.
        try:
            tabs = WebDriverWait(driver, 3).until(
                EC.presence_of_all_elements_located((By.XPATH, "//a[contains(text(), '분기')]"))
            )
            for tab in tabs:
                if tab.is_displayed():
                    driver.execute_script("arguments[0].click();", tab)
                    debug.append("🖱️ '분기' 탭 즉시 클릭 완료. (스마트 폴링 가동)")
                    break
        except Exception:
            pass # 탭이 이미 클릭되어 있거나 못 찾아도 일단 파싱 돌격

        # 2. [Architect Fix] 스마트 폴링 (0.5초 간격으로 최대 10회 탐색)
        # 네이버 서버가 데이터를 뱉어내는 그 '0.X초'의 순간을 캐치하여 즉시 탈출한다.
        results = []
        for attempt in range(10):
            time.sleep(0.5)
            html = driver.page_source
            soup = BeautifulSoup(html, 'html.parser')

            target_table = None
            for table in soup.find_all('table'):
                text = table.get_text()
                if '매출액' in text and '영업이익' in text and ('(E)' in text or '(P)' in text):
                    months = set(re.findall(r'\d{4}[./](\d{2})', text))
                    if len(months) >= 2:
                        target_table = table
                        break
                        
            if not target_table: continue

            thead = target_table.find('thead')
            if not thead: continue

            header_texts = []
            for tr in thead.find_all('tr'):
                ths = tr.find_all(['th', 'td'])
                texts = [th.get_text(strip=True) for th in ths if re.search(r'\d{4}[./]\d{2}', th.get_text())]
                if len(texts) > len(header_texts):
                    header_texts = texts
                    
            est_indices = [(i, h) for i, h in enumerate(header_texts) if '(E)' in h or '(P)' in h]
            
            tbody = target_table.find('tbody')
            if not tbody: continue

            rev_tds, op_tds, op_announce_tds = None, None, None
            for row in tbody.find_all('tr'):
                cells = row.find_all(['th', 'td'])
                if not cells: continue
                
                row_name = cells[0].get_text(strip=True).replace(' ', '').replace('\xa0', '')
                tds = row.find_all('td')
                
                if '매출액' == row_name and rev_tds is None:
                    rev_tds = tds
                elif '영업이익(발표기준)' == row_name and op_announce_tds is None:
                    op_announce_tds = tds
                elif '영업이익' == row_name and op_tds is None:
                    op_tds = tds

            final_op_tds = op_announce_tds if op_announce_tds else op_tds
            if not rev_tds or not final_op_tds: continue
            
            offset_rev = len(rev_tds) - len(header_texts)
            offset_op = len(final_op_tds) - len(header_texts)

            # 임시 배열에 데이터를 담아본다
            temp_results = []
            for idx, col_name in est_indices:
                r_idx = idx + offset_rev
                o_idx = idx + offset_op
                
                if r_idx < 0 or r_idx >= len(rev_tds) or o_idx < 0 or o_idx >= len(final_op_tds):
                    continue
                    
                rv_str = rev_tds[r_idx].get_text(strip=True).replace(',', '')
                ov_str = final_op_tds[o_idx].get_text(strip=True).replace(',', '')
                
                try:
                    rv_f = float(rv_str) if rv_str and rv_str not in ['-', 'nan', 'NaN'] else 0.0
                    ov_f = float(ov_str) if ov_str and ov_str not in ['-', 'nan', 'NaN'] else 0.0
                except ValueError:
                    continue
                    
                if rv_f > 0:
                    # 연간/반기 데이터 필터링
                    if baseline_rev > 1000 and rv_f > baseline_rev * 2.5:
                        continue
                    temp_results.append((col_name, rv_f, ov_f))

            # 🎯 [핵심 로직] 추출된 결과가 2개 이상인가? = AJAX 로딩이 완전히 끝난 진짜 '분기 표'다!
            # 무의미한 대기를 찢어버리고 즉시 함수를 종료하며 탈출한다.
            if len(temp_results) >= 2:
                debug.append(f"⚡ 쾌속 렌더링 감지: {attempt+1}회차 (약 {(attempt+1)*0.5}초) 만에 스크래핑 완료!")
                return temp_results, f"성공 ({len(temp_results)}개 분기 로드)", debug
            
            # 실패했다면 (AJAX 로딩 중이라면) temp_results를 버리고 다음 0.5초 뒤를 노린다.
            results = temp_results

        # 10회(5초)를 다 돌았는데도 탈출하지 못했다면, 지금까지 얻은 거라도 던져준다.
        debug.append("⚠️ 쾌속 탈출 실패. (타임아웃)")
        return results, f"성공 ({len(results)}개 로드)", debug

    except Exception as e:
        debug.append(f"💥 치명적 에러: {traceback.format_exc()}")
        return [], f"[에러] {str(e)}", debug


# ==========================================
# [PRESENTATION] Streamlit UI Layer
# ==========================================
st.set_page_config(page_title="DART Financial Dashboard", layout="wide")

# ============================================================
# [Architect Fix] 실수로 누락했던 비동기 DART 데이터 로더 브릿지 복구
# ============================================================
def run_async_safe(coroutine):
    try: loop = asyncio.get_running_loop()
    except RuntimeError: loop = None
    if loop and loop.is_running(): return asyncio.run_coroutine_threadsafe(coroutine, loop).result()
    else: return asyncio.run(coroutine)

@st.cache_data(ttl=86400, show_spinner=False)
def load_data(api_key, query, years):
    client = DartCoreClient(api_key)
    return run_async_safe(client.fetch_all_data(query, years))
# ============================================================

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
            query = st.text_input("종목명 또는 코드", "삼성전자")
            years = st.slider("조회 기간(년)", 3, 20, 10)
            fetch_btn = st.form_submit_button("데이터 조회", use_container_width=True)

    # ==========================================
    # 2. 메인 데이터 패치 계층 (폼 제출 시 즉시 실행)
    # ==========================================
    if fetch_btn:
        if not api_key:
            st.error("API Key 누락. 인증 없는 요청은 네트워크 I/O 낭비다.")
            return
        st.cache_data.clear()
        with st.spinner("데이터 패치 및 연산 중..."):
            try:
                # DART 데이터 로드
                df, stock, corp_name, stock_code, corp_code = load_data(api_key, query, years)
                st.session_state.update({'df': df, 'stock': stock, 'corp_name': corp_name, 'stock_code': stock_code})

                # [Architect Fix] 완벽한 스케일 필터링을 위해 DART의 '직전 분기 실제 매출액'을 베이스라인으로 계산해 주입
                baseline_rev = df['Rev'].iloc[-1] / 1e8 if not df.empty else 1e12

                # FnGuide 컨센서스 전용 서버 스크래핑 엔진 가동
                naver_ests, msg, debug = fetch_wisereport_consensus(stock_code, baseline_rev)
                st.session_state['naver_estimates'] = naver_ests
                st.session_state['consensus_msg'] = msg
                st.session_state['naver_debug'] = debug

                for i, (_, rev, op) in enumerate(naver_ests):
                    st.session_state[f'e_rev_{i}'] = float(rev)
                    st.session_state[f'e_op_{i}'] = float(op)

            except Exception as e:
                st.error(f"런타임 에러: {e}")
                return

    with st.sidebar:
        st.markdown("---")
        st.subheader("🔮 다음 분기 컨센서스 (E)")

        debug_lines = st.session_state.get('naver_debug', [])
        if debug_lines:
            with st.expander("🔍 스크래핑 디버그 (텔레메트리)"):
                for line in debug_lines:
                    st.caption(line)

        naver_ests = st.session_state.get('naver_estimates', [])
        msg = st.session_state.get('consensus_msg', '')

        if naver_ests:
            periods_str = ' / '.join([e[0] for e in naver_ests])
            st.caption(f"✅ 자동 로드: **{periods_str}**")
        else:
            st.caption(f"⚠️ {msg or '컨센서스 없음. 수동 입력하세요.'}")

        n_est = st.slider("표시할 추정 분기 수", 1, 4, 3)

        estimates = []
        for i in range(n_est):
            auto_label = f" *(자동: {naver_ests[i][0]})*" if i < len(naver_ests) else " *(수동)*"
            st.markdown(f"**Q+{i+1}**{auto_label}")

            if f'e_rev_{i}' not in st.session_state:
                st.session_state[f'e_rev_{i}'] = float(naver_ests[i][1]) if i < len(naver_ests) else 0.0
            if f'e_op_{i}' not in st.session_state:
                st.session_state[f'e_op_{i}'] = float(naver_ests[i][2]) if i < len(naver_ests) else 0.0

            c1, c2 = st.columns(2)
            with c1:
                r = st.number_input("매출(억)", step=100.0, key=f"e_rev_{i}", label_visibility="collapsed")
            with c2:
                o = st.number_input("영업익(억)", step=10.0, key=f"e_op_{i}", label_visibility="collapsed")
            estimates.append((r, o))

    if 'df' in st.session_state:
        df = st.session_state['df'].copy() 
        stock = st.session_state['stock']
        corp_name = st.session_state['corp_name']
        stock_code = st.session_state['stock_code']
        
        st.subheader(f"{corp_name} ({stock_code}) - {years}년 재무 및 주가 추이")
        
        estimates_active = [(r, o) for r, o in estimates if r > 0 and o > 0]
        n_estimated = len(estimates_active)

        if n_estimated > 0:
            n_orig = len(df)
            last_date = pd.Timestamp(df.index[-1])

            for i, (e_rev, e_op) in enumerate(estimates_active):
                e_rev_won = e_rev * 1e8
                e_op_won = e_op * 1e8

                prev_rev_won = df['Rev'].iloc[-1] if i == 0 else estimates_active[i-1][0] * 1e8
                yr_idx = n_orig - 4 + i
                prev_yr_rev_won = df['Rev'].iloc[yr_idx] if 0 <= yr_idx < n_orig else np.nan

                e_yoy = ((e_rev_won - prev_yr_rev_won) / prev_yr_rev_won) * 100 if pd.notna(prev_yr_rev_won) and prev_yr_rev_won != 0 else 0
                e_qoq = ((e_rev_won - prev_rev_won) / prev_rev_won) * 100 if prev_rev_won != 0 else 0
                e_opm = (e_op_won / e_rev_won) * 100 if e_rev_won != 0 else 0

                next_date = (last_date + pd.DateOffset(months=3*(i+1))) + pd.offsets.MonthEnd(0)

                new_row = pd.DataFrame({
                    'Rev': [e_rev_won], 'Op': [e_op_won], 'YoY': [e_yoy],
                    'Rev_QoQ': [e_qoq], 'OPM': [e_opm],
                    'ROE': [df['ROE'].iloc[-1]], 'NI_Margin': [df['NI_Margin'].iloc[-1]],
                    'Asset_Turnover': [df['Asset_Turnover'].iloc[-1]],
                    'Leverage': [df['Leverage'].iloc[-1]]
                }, index=[next_date])
                
                df = pd.concat([df, new_row])

            df.index = pd.to_datetime(df.index)

        rev_colors = ["rgba(231, 76, 60, 0.35)"] * len(df)
        yoy_colors = ["rgba(52, 73, 94, 0.6)"] * len(df)
        qoq_colors = ["rgba(142, 68, 173, 0.5)"] * len(df)

        for k in range(1, n_estimated + 1):
            rev_colors[-k] = "rgba(231, 76, 60, 1.0)"
            yoy_colors[-k] = "rgba(52, 73, 94, 1.0)"
            qoq_colors[-k] = "rgba(142, 68, 173, 1.0)"

        fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.06, 
                            row_heights=[0.5, 0.25, 0.25], 
                            specs=[[{"secondary_y": True}], [{"secondary_y": True}], [{"secondary_y": True}]])

        fig.add_trace(go.Scatter(x=stock.index, y=stock['Close'], name="주가", line=dict(color="#2c3e50", width=1.5)), row=1, col=1, secondary_y=False)
        fig.add_trace(go.Bar(x=df.index, y=df['Rev']/1e8, name="분기 매출(억)", marker_color=rev_colors, marker_line_width=0), row=1, col=1, secondary_y=True)
        fig.add_trace(go.Scatter(x=df.index, y=df['Op']/1e8, name="분기 영업이익(억)", mode="lines+markers", marker=dict(symbol='square', size=6), line=dict(color="#f39c12", width=2)), row=1, col=1, secondary_y=True)

        fig.add_trace(go.Bar(x=df.index, y=df['YoY'], name="매출 YoY(%)", marker_color=yoy_colors, marker_line_width=0), row=2, col=1, secondary_y=False)
        fig.add_trace(go.Scatter(x=df.index, y=df['OPM'], name="영업이익률(%)", mode="lines+markers", marker=dict(size=6), line=dict(color="#27ae60", width=2)), row=2, col=1, secondary_y=True)

        fig.add_trace(go.Bar(x=df.index, y=df['Rev_QoQ'], name="매출 QoQ(%)", marker_color=qoq_colors, marker_line_width=0), row=3, col=1, secondary_y=False)
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
        fig.update_yaxes(title_text="영업이익률 (%)", secondary_y=True, row=3, col=1, showgrid=False)

        for k in range(1, n_estimated + 1):
            fig.add_annotation(
                x=df.index[-k], y=df['Rev'].iloc[-k] / 1e8,
                text="<b>E</b>", showarrow=True, arrowhead=2, arrowsize=1,
                ax=0, ay=-30, font=dict(color="#e74c3c", size=12),
                row=1, col=1, secondary_y=True,
                bgcolor="rgba(255,255,255,0.7)", bordercolor="#e74c3c"
            )

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
        
        inference_result = RuthlessInferenceEngine.analyze_dupont(st.session_state['df'])
        
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