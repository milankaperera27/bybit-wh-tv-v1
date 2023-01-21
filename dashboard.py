import streamlit as st
import streamlit.components.v1 as components
import config
import pandas as pd
import numpy as np
# import plotly.express as px
from pybit import usdt_perpetual

st.set_page_config(
    page_title="COG Trading Bot Dashboard",
    page_icon="✅",
    layout="wide",
)

st.title('COG Trading Bot Dashboard')
st.markdown('by Meddy and Smokey')

components.html("""
                <!-- TradingView Widget BEGIN -->
    <div class="tradingview-widget-container">
    <div class="tradingview-widget-container__widget"></div>
    <div class="tradingview-widget-copyright"><a href="https://www.tradingview.com/markets/" rel="noopener" target="_blank"><span class="blue-text">Markets today</span></a> by TradingView</div>
    <script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-tickers.js" async>
    {
    "symbols": [
        {
        "proName": "FOREXCOM:SPXUSD",
        "title": "S&P 500"
        },
        {
        "proName": "FOREXCOM:NSXUSD",
        "title": "US 100"
        },
        {
        "proName": "FX_IDC:EURUSD",
        "title": "EUR/USD"
        },
        {
        "proName": "BITSTAMP:BTCUSD",
        "title": "Bitcoin"
        },
        {
        "proName": "BITSTAMP:ETHUSD",
        "title": "Ethereum"
        }
    ],
    "colorTheme": "dark",
    "isTransparent": true,
    "showSymbolLogo": true,
    "locale": "en"
    }
    </script>
    </div>
    <!-- TradingView Widget END -->
                """)
# exchange validation

session_testnet = usdt_perpetual.HTTP(
    endpoint='https://api-testnet.bybit.com',
    api_key=config.API_KEY_TESTING,
    api_secret=config.API_SECRET_TESTING
)

# wallet balance
balance_usdt = session_testnet.get_wallet_balance(coin='USDT')
usdt_available_balance = balance_usdt['result']['USDT']['available_balance']
usdt_equity = balance_usdt['result']['USDT']['equity']
usdt_used_margin = balance_usdt['result']['USDT']['used_margin']
usdt_realised_pnl = balance_usdt['result']['USDT']['realised_pnl']
usdt_unrealised_pnl = balance_usdt['result']['USDT']['unrealised_pnl']

col1, col2, col3, col4 = st.columns(4)
with col1:
    st.write('Available Balance $ : ')
    st.write('Equity $ : ')
with col2:
    st.write(usdt_available_balance)
    st.write(usdt_equity)
with col3:
    st.write('Used Margin $ : ')
    st.write('Realized PnL $ : ')
    st.write('UnRealized PnL $ : ')
with col4:
    st.write(usdt_used_margin)
    st.write(usdt_realised_pnl)
    st.write(usdt_unrealised_pnl)


# exectued trades
def load_data():
    df = pd.read_csv("trades.csv")
    return df


trades2, trades3 = st.columns(2)
with trades2:
    st.subheader('Open Position Info')
    st.write('Current Position ')
    st.write('Symbol:')
    st.write('Quantity (ETH):')
    st.write('Unrealized PnL $')
with trades3:
    position = session_testnet.my_position(symbol='ETHUSDT')
    st.subheader(':dart:')
    st.write(position['result'][0]['side'])
    st.write(position['result'][0]['symbol'])
    st.write(position['result'][0]['size'])
    st.write(position['result'][0]['unrealised_pnl'])

st.subheader('Executed Trades')
trades = load_data()
st.dataframe(trades)

view_chart = st.radio('View TradingView Chart', ['False', 'True'])

if view_chart == 'True':
    components.html(
        """
        <div class="tradingview-widget-container">
        <div id="tradingview_1e834"></div>
        <div class="tradingview-widget-copyright"><a href="https://www.tradingview.com/symbols/ETHUSDT.P/?exchange=BYBIT" rel="noopener" target="_blank"><span class="blue-text">ETHUSDT.P chart</span></a> by TradingView</div>
        <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
        <script type="text/javascript">
        new TradingView.widget(
        {
        "width": 1280,
        "height": 960,
        "symbol": "BYBIT:ETHUSDT.P",
        "interval": "60",
        "timezone": "Etc/UTC",
        "theme": "dark",
        "style": "1",
        "locale": "en",
        "toolbar_bg": "#f1f3f6",
        "enable_publishing": false,
        "hide_side_toolbar": false,
        "details": true,
        "container_id": "tradingview_1e834"
        }
        );
        </script>
        </div>
        """,
        height=960,
    )