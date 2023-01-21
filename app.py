import json
import csv
import requests
from flask import Flask, render_template, request, jsonify
from pybit import usdt_perpetual
import pandas as pd
import numpy as np
import config
from datetime import datetime

app = Flask(__name__)

###############################################################################

session_testnet = usdt_perpetual.HTTP(
    endpoint='https://api-testnet.bybit.com',
    api_key=config.API_KEY_TESTING,
    api_secret=config.API_SECRET_TESTING
)

in_long_position = False
in_short_position = False

# Create an empty dataframe with columns for trade data
trade_data = pd.DataFrame(
    columns=['Time', 'Position', 'Symbol', 'Side', 'Quantity', 'Price', 'Stop Loss'])

# in execute_order function
current_time = datetime.now()


def execute_order(symbol, position, side, qty, stop_loss):
    try:
        order = session_testnet.place_active_order(symbol=symbol, side=side, order_type='Market',
                                                   qty=qty, time_in_force='GoodTillCancel',
                                                   reduce_only=False, close_on_trigger=False)
        print('=========================')
        print(f'{side.upper()} ENTRY executed for :',
              order['result']['symbol'])
        print('|- Entry Price:', order['result']['price'])
        print('|- Entry quantity :', order['result']['qty'])
        print('=========================')
        trade_data.loc[len(trade_data)] = [current_time, position, order['result']
        ['symbol'], side, qty, order['result']['price'], stop_loss]
        print(trade_data)
        trade_data.to_csv('trades.csv', index=False)
        return True
    except Exception as e:
        print(e)
        return False


def close_position(symbol, position, side, qty, stop_loss):
    try:
        close = session_testnet.place_active_order(symbol=symbol, side=side, order_type='Market',
                                                   qty=qty, time_in_force='GoodTillCancel',
                                                   reduce_only=True, close_on_trigger=False)
        print('=========================')
        print(f'{side.upper()} EXIT executed for :', close['result']['symbol'])
        print('|- Exit Price:', close['result']['price'])
        print('|- Exit quantity :', close['result']['qty'])
        print('=========================')
        trade_data.loc[len(trade_data)] = [current_time, position, close['result']
        ['symbol'], side, qty, close['result']['price'], stop_loss]
        print(trade_data)
        trade_data.to_csv('trades.csv', index=False)
        return True
    except Exception as e:
        print(e)
        return False


def buy_alert(data, stop_loss):
    global in_long_position, in_short_position

    if in_short_position:
        in_short_position = close_position(
            data['symbol'], 'Short', 'Buy', data['qty'], stop_loss)
    if not in_long_position:
        in_long_position = execute_order(
            data['symbol'], 'Long', 'Buy', data['qty'], stop_loss)


def sell_alert(data, stop_loss):
    global in_long_position, in_short_position

    if in_long_position:
        in_long_position = close_position(
            data['symbol'], 'Long', 'Sell', data['qty'], stop_loss)
    if not in_short_position:
        in_short_position = execute_order(
            data['symbol'], 'Short', 'Sell', data['qty'], stop_loss)


def long_sl_sell(data, stop_loss):
    global in_long_position

    if in_long_position:
        close_position(data['symbol'], 'Long', 'Sell', data['qty'], stop_loss)
        in_long_position == "False"


def short_sl_buy(data, stop_loss):
    global in_short_position

    if in_short_position:
        close_position(data['symbol'], 'Short', 'Buy', data['qty'], stop_loss)
        in_short_position == "False"


@app.route('/')
def index():
    return {'message': 'Server is running!'}


@app.route('/webhook', methods=['POST'])
def webhook():
    # data = request.form.to_dict()
    data = json.loads(request.data)
    side = data['side']
    quantity = data['qty']
    symbol = data['symbol']
    long_sl = data['long_sl']
    short_sl = data['short_sl']

    print('Tradingview Alert Received:')
    print('Current Status :')
    print('In Long:', in_long_position)
    print('In Short:', in_short_position)
    print(data)

    if long_sl == 'False' and short_sl == 'False':
        if side == 'Buy':
            buy_alert(data, stop_loss='False')
            return {
                "status": "success",
                "message": "Bybit Webhook Received - BUY ALERT EXECUTED"
            }
        elif side == 'Sell':
            sell_alert(data, stop_loss='False')
            return {
                "status": "success",
                "message": "Bybit Webhook Received - SELL ALERT EXECUTED"
            }

    if long_sl == 'True':
        long_sl_sell(data, stop_loss='True')
        return {
            "status": "success",
            "message": "Bybit Webhook Received - LONG STOPLOSS EXECUTED"
        }
    if short_sl == 'True':
        short_sl_buy(data, stop_loss='True')
        return {
            "status": "success",
            "message": "Bybit Webhook Received - SHORT STOPLOSS EXECUTED"
        }


if __name__ == '__main__':
    app.run(debug=True, port=4000)
