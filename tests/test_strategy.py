import unittest

import pandas as pd

from strategy import (
    analyze_candles,
    create_position_from_signal,
    evaluate_open_position,
)


BUY_CLOSES = [
    1000, 986.74, 984.35, 970.26, 977.02, 984.74, 962.17, 976.57, 998.44,
    988.47, 1002.8, 993.21, 1013.18, 1035.96, 1046.28, 1041.8, 1034.07,
    1018.3, 1037.4, 1058.99, 1069.39, 1090.28, 1082.01, 1061.12, 1060.09,
    1085.04, 1089.79, 1106.64, 1114.11, 1109.7, 1109.08, 1096.15, 1090.67,
    1113.66, 1134.71, 1140.34, 1150.68, 1141.87, 1149.08, 1169.17, 1147.98,
    1171.75, 1155.39, 1132.68, 1108.82, 1104.06, 1116.01, 1103.07, 1125.23,
    1138.55, 1120.5, 1134.53, 1120.14, 1130.75, 1108.24, 1123.23, 1115.68,
    1125.1, 1103.59, 1103.58, 1097.9, 1080.22, 1076.34, 1099.95, 1076.73,
    1099.81, 1090.0, 1078.0, 1098.0, 1138.0, 1180.0,
]

SELL_CLOSES = [
    1000, 988.14, 980.77, 962.18, 964.93, 947.34, 954.33, 954.01, 950.94,
    941.31, 933.42, 949.14, 966.77, 976.85, 965.36, 953.69, 948.2, 963.23,
    980.94, 970.14, 975.48, 970.67, 968.89, 956.8, 965.66, 976.94, 994.52,
    993.89, 991.56, 997.41, 982.66, 997.8, 1008.52, 997.63, 993.7, 993.14,
    1007.22, 1014.93, 1004.5, 1022.54, 1039.74, 1054.05, 1053.76, 1052.69,
    1048.95, 1068.06, 1067.31, 1085.69, 1073.05, 1054.27, 1053.15, 1041.1,
    1031.37, 1012.81, 997.78, 995.43, 978.93, 970.78, 975.28, 965.68,
    958.14, 953.27, 943.81, 946.34, 957.41, 938.66, 940.12, 941.51,
    958.24, 966.4, 957.39, 962.03, 978.06, 996.12, 1006.82, 991.12, 986.0,
    985.14, 978.31, 988.66, 988.38, 1007.52, 987.84, 1000.15, 984.69,
    985.44, 971.39, 966.68, 971.14, 989.49, 971.12,
]


def build_df(closes, wiggle):
    rows = []
    timestamp = 0
    for close in closes:
        rows.append({
            "timestamp": timestamp,
            "open": close,
            "high": close + wiggle,
            "low": close - wiggle,
            "close": close,
            "volume": 1000,
        })
        timestamp += 1
    return pd.DataFrame(rows)


class StrategyTests(unittest.TestCase):
    def test_hold_when_data_is_insufficient(self):
        df = build_df([100 + i for i in range(10)], wiggle=50)
        result = analyze_candles(df, verbose=False)
        self.assertEqual(result["signal"], "hold")

    def test_buy_signal_on_confirmed_bullish_cross(self):
        df = build_df(BUY_CLOSES, wiggle=120)
        result = analyze_candles(df, verbose=False)
        self.assertEqual(result["signal"], "buy")

    def test_sell_signal_on_confirmed_bearish_cross(self):
        df = build_df(SELL_CLOSES, wiggle=120)
        result = analyze_candles(df, verbose=False)
        self.assertEqual(result["signal"], "sell")

    def test_hold_on_low_volatility_market(self):
        closes = [1000 + ((i % 2) * 0.05) for i in range(80)]
        df = build_df(closes, wiggle=0.02)
        result = analyze_candles(df, verbose=False)
        self.assertEqual(result["signal"], "hold")

    def test_long_position_closes_on_stop_loss(self):
        position = create_position_from_signal("buy", 100.0, 1)
        result = evaluate_open_position(position, 98.9, 2, verbose=False)
        self.assertEqual(result["action"], "close")
        self.assertEqual(result["closed_position"]["reason"], "stop loss acionado")

    def test_long_position_closes_on_take_profit(self):
        position = create_position_from_signal("buy", 100.0, 1)
        result = evaluate_open_position(position, 104.2, 2, verbose=False)
        self.assertEqual(result["action"], "close")
        self.assertEqual(result["closed_position"]["reason"], "take profit acionado")

    def test_long_position_closes_on_trailing_stop(self):
        position = create_position_from_signal("buy", 100.0, 1)
        holding = evaluate_open_position(position, 100.9, 2, verbose=False)
        self.assertEqual(holding["action"], "hold")

        closing = evaluate_open_position(holding["position"], 100.1, 3, verbose=False)
        self.assertEqual(closing["action"], "close")
        self.assertEqual(closing["closed_position"]["reason"], "trailing stop acionado")

    def test_short_position_closes_on_take_profit(self):
        position = create_position_from_signal("sell", 100.0, 1)
        result = evaluate_open_position(position, 95.8, 2, verbose=False)
        self.assertEqual(result["action"], "close")
        self.assertEqual(result["closed_position"]["reason"], "take profit acionado")


if __name__ == "__main__":
    unittest.main()
