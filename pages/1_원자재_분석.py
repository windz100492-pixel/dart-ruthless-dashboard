import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
import numpy as np

# 페이지 설정
st.set_page_config(page_title="원자재 대시보드", page_icon="🛢️", layout="wide")

# ==========================================
# [HARNESS CORE] 원자재 데이터 카테고리 딕셔너리
# ==========================================
COMMODITY_MAP = {
    "👑 귀금속": {
        "금 (Gold)": "GC=F", 
        "은 (Silver)": "SI=F", 
        "백금 (Platinum)": "PL=F", 
        "팔라듐 (Palladium)": "PA=F"
    },
    "⚡ 에너지": {
        "WTI 원유": "CL=F", 
        "브렌트 원유": "BZ=F", 
        "천연가스": "NG=F", 
        "가솔린": "RB=F"
    },
    "🏭 비철금속": {
        "구리 (Copper)": "HG=F", 
        "알루미늄": "ALI=F"
    },
    "🌾 곡물": {
        "옥수수 (Corn)": "ZC=F", 
        "대두 (Soybean)": "ZS=F", 
        "소맥 (Wheat)": "KE=F", 
        "대두박": "ZM=F"
    },
    "☕ 소프트": {
        "코코아 (Cocoa)": "CC=F", 
        "커피 (Coffee)": "KC=F", 
        "설탕 (Sugar)": "SB=F", 
        "면화 (Cotton)": "CT=F"
    }
}

# ==========================================
# [HARNESS CORE] 데이터 액세스 계층 (강력한 예외 방어)
# ==========================================
@st.cache_data(ttl=3600, show_spinner=False)
def load_all_commodities(period="2y"):
    data_store = {}
    for category, items in COMMODITY_MAP.items():
        data_store[category] = {}
        for name, sym in items.items():
            try:
                df = yf.download(sym, period=period, progress=False)
                if df.empty: continue
                
                # yfinance MultiIndex 평탄화 방어
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                
                data_store[category][name] = df.dropna()
            except Exception:
                continue # 특정 티커가 터져도 전체 앱이 죽지 않도록 방어
    return data_store

# ==========================================
# [STATE MANAGEMENT] 상태 관리 콜백 함수
# ==========================================
def change_selected_commodity(new_commodity, new_category):
    st.session_state['selected_commodity'] = new_commodity
    st.session_state['selected_category'] = new_category

# 초기 상태 주입 (앱 최초 실행 시 '금'을 기본 화면으로 세팅)
if 'selected_commodity' not in st.session_state:
    st.session_state['selected_commodity'] = "금 (Gold)"
    st.session_state['selected_category'] = "👑 귀금속"

# ==========================================
# [PRESENTATION] 뷰 렌더링 계층
# ==========================================
def render_dashboard():
    with st.spinner("글로벌 원자재 매크로 지표 동기화 중... (최초 로딩 시 5~10초 소요)"):
        data_store = load_all_commodities()

    if not data_store:
        st.error("네트워크 I/O 에러: 데이터를 불러올 수 없습니다.")
        return

    selected_comm = st.session_state['selected_commodity']
    selected_cat = st.session_state['selected_category']
    
    # 만약 불러온 데이터에 선택된 종목이 없으면(yfinance 오류 등) 안전하게 첫 번째 종목으로 강제 스위칭
    if selected_comm not in data_store.get(selected_cat, {}):
        selected_cat = list(data_store.keys())[0]
        selected_comm = list(data_store[selected_cat].keys())[0]

    df_main = data_store[selected_cat][selected_comm]
    
    # 1. 상단 메인 뷰 (Master Chart)
    col_header_1, col_header_2 = st.columns([1, 4])
    
    with col_header_1:
        st.caption(f"{selected_cat}")
        st.header(selected_comm)
        
        # 핵심 지표 계산
        latest_close = df_main['Close'].iloc[-1]
        prev_close = df_main['Close'].iloc[-2]
        change_val = latest_close - prev_close
        change_pct = (change_val / prev_close) * 100
        
        # 색상 로직
        color = "#e74c3c" if change_val < 0 else "#2ecc71"
        arrow = "↓" if change_val < 0 else "↑"
        
        st.markdown(f"<h2 style='margin-bottom:0px; padding-bottom:0px;'>${latest_close:,.2f}</h2>", unsafe_allow_html=True)
        st.markdown(f"<span style='color:{color}; font-weight:bold;'>{change_pct:+.2f}% ({change_val:+.2f})</span>", unsafe_allow_html=True)

    with col_header_2:
        # Plotly 메인 차트 렌더링 (이동평균선 포함)
        fig = go.Figure()
        
        fig.add_trace(go.Candlestick(
            x=df_main.index, open=df_main['Open'], high=df_main['High'], low=df_main['Low'], close=df_main['Close'],
            increasing_line_color='#ef5350', decreasing_line_color='#2980b9', name="시세"
        ))
        
        # 이동평균선(MA) 덧그리기
        ma20 = df_main['Close'].rolling(20).mean()
        ma50 = df_main['Close'].rolling(50).mean()
        fig.add_trace(go.Scatter(x=df_main.index, y=ma20, line=dict(color='#f39c12', width=1.5), name='MA20'))
        fig.add_trace(go.Scatter(x=df_main.index, y=ma50, line=dict(color='#9b59b6', width=1.5), name='MA50'))

        # [Architect Fix] 범례(Legend)를 좌측 상단으로 이동시켜 우측 상단의 툴바(Modebar)와 겹치는 버그를 박살 낸다.
        fig.update_layout(
            height=400, margin=dict(l=0, r=0, t=30, b=0),
            template="plotly_white", hovermode="x unified",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0)
        )
        
        # [Architect Fix] 쓰레기 슬라이더와 주말 공백 무자비하게 절제
        fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])], rangeslider_visible=False)
        st.plotly_chart(fig, use_container_width=True)

    st.markdown("---")

    # 2. 하단 목록 뷰 (Detail Cards)
    st.subheader("종목별 실시간 시세 (클릭하여 차트 전환)")
    
    # 3열(Column) 그리드 레이아웃 생성
    cols = st.columns(3)
    col_idx = 0
    
    for category, items in data_store.items():
        with cols[col_idx % 3]:
            # Streamlit 1.30+ 기능인 컨테이너 보더(Border)를 사용하여 카드 형태 구현
            with st.container(border=True):
                st.markdown(f"**{category}**")
                
                # 카드 내부의 각 종목들을 버튼 형태의 리스트로 나열
                for name, df in items.items():
                    if df.empty: continue
                    
                    last_px = df['Close'].iloc[-1]
                    prev_px = df['Close'].iloc[-2]
                    chg_pct = ((last_px - prev_px) / prev_px) * 100
                    
                    # 버튼과 텍스트를 한 줄에 예쁘게 배치하기 위한 내부 컬럼
                    btn_col, val_col = st.columns([0.6, 0.4])
                    
                    with btn_col:
                        # [Architect Logic] 버튼 클릭 시 상태(st.session_state)를 즉시 변경하는 콜백 바인딩
                        is_selected = (name == selected_comm)
                        btn_type = "primary" if is_selected else "secondary"
                        st.button(name, key=f"btn_{name}", type=btn_type, use_container_width=True, 
                                  on_click=change_selected_commodity, args=(name, category))
                        
                    with val_col:
                        color = "red" if chg_pct < 0 else "green"
                        st.markdown(f"<div style='text-align:right; font-size:14px;'><b>${last_px:,.2f}</b><br><span style='color:{color};'>{chg_pct:+.2f}%</span></div>", unsafe_allow_html=True)
        
        col_idx += 1

if __name__ == "__main__":
    render_dashboard()