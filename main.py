import json
import sys
from pathlib import Path
from urllib.parse import urlencode

from PyQt5.QtCore import QObject, Qt, QUrl, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QColor
from PyQt5.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PyQt5.QtWebChannel import QWebChannel
from PyQt5.QtWebEngineWidgets import QWebEnginePage, QWebEngineSettings, QWebEngineView
from PyQt5.QtWebSockets import QWebSocket
from PyQt5.QtNetwork import QAbstractSocket
from PyQt5.QtWidgets import QApplication, QMainWindow

HTML_FILE = Path(__file__).with_name("chart.html")
BINANCE_REST_URL = "https://api.binance.com/api/v3/klines"
VALID_INTERVALS = {"1m", "5m", "15m", "1h"}


class DebugWebPage(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, message, line_number, source_id):
        print(f"[JS:{level}] {source_id}:{line_number} {message}")


class ChartBridge(QObject):
    historicalDataReady = pyqtSignal(str)
    candleUpdated = pyqtSignal(str)
    livePriceChanged = pyqtSignal(str, float, float, float)
    marketChanged = pyqtSignal(str, str)
    loadingStateChanged = pyqtSignal(bool, str)
    statusMessage = pyqtSignal(str, bool)

    def __init__(self):
        super().__init__()
        self.symbol = "BTCUSDT"
        self.interval = "5m"
        self._network = QNetworkAccessManager(self)
        self._network.finished.connect(self._handle_historical_reply)
        self._socket = QWebSocket()
        self._socket.textMessageReceived.connect(self._handle_socket_message)
        self._socket.connected.connect(self._handle_socket_connected)
        self._socket.disconnected.connect(self._handle_socket_disconnected)
        self._socket.error.connect(self._handle_socket_error)
        self._pending_request = None
        self._page_ready = False

    @pyqtSlot()
    def pageReady(self):
        self._page_ready = True
        self.marketChanged.emit(self.symbol, self.interval)
        self.reloadMarket(self.symbol, self.interval)

    @pyqtSlot(str, str)
    def reloadMarket(self, symbol, interval):
        clean_symbol = "".join(ch for ch in symbol.upper().strip() if ch.isalnum())
        clean_interval = interval.strip().lower()

        if not clean_symbol:
            self.statusMessage.emit("Enter a valid Binance symbol like BTCUSDT.", True)
            return

        if clean_interval not in VALID_INTERVALS:
            self.statusMessage.emit("Only 1m, 5m, 15m, and 1h intervals are supported.", True)
            return

        self.symbol = clean_symbol
        self.interval = clean_interval
        self.marketChanged.emit(self.symbol, self.interval)
        self.loadingStateChanged.emit(True, f"Loading {self.symbol} {self.interval} candles...")
        self.statusMessage.emit("", False)
        self._stop_socket()
        self._fetch_historical_data()

    def _fetch_historical_data(self):
        if self._pending_request is not None:
            try:
                self._pending_request.abort()
                self._pending_request.deleteLater()
            except RuntimeError:
                pass
            self._pending_request = None

        params = urlencode(
            {
                "symbol": self.symbol,
                "interval": self.interval,
                "limit": 500,
            }
        )
        request = QNetworkRequest(QUrl(f"{BINANCE_REST_URL}?{params}"))
        request.setRawHeader(b"User-Agent", b"PyQt5-TradingView-Style-Chart")
        self._pending_request = self._network.get(request)

    def _handle_historical_reply(self, reply: QNetworkReply):
        if reply is not self._pending_request:
            reply.deleteLater()
            return

        self._pending_request = None

        if reply.error() != QNetworkReply.NoError:
            self.loadingStateChanged.emit(False, "")
            self.statusMessage.emit(
                f"Failed to load Binance candles: {reply.errorString()}",
                True,
            )
            reply.deleteLater()
            return

        try:
            raw_payload = bytes(reply.readAll()).decode("utf-8")
            klines = json.loads(raw_payload)
            candles = [self._kline_to_candle(item) for item in klines]
        except Exception as exc:
            self.loadingStateChanged.emit(False, "")
            self.statusMessage.emit(f"Invalid historical data: {exc}", True)
            reply.deleteLater()
            return

        if not candles:
            self.loadingStateChanged.emit(False, "")
            self.statusMessage.emit("Binance returned no candles for that market.", True)
            reply.deleteLater()
            return

        self.historicalDataReady.emit(json.dumps(candles))
        last_candle = candles[-1]
        self._emit_price_update(last_candle)
        self.loadingStateChanged.emit(False, "")
        self._start_socket()
        reply.deleteLater()

    def _start_socket(self):
        self._stop_socket()
        stream = f"{self.symbol.lower()}@kline_{self.interval}"
        self._socket.open(QUrl(f"wss://stream.binance.com:9443/ws/{stream}"))

    def _stop_socket(self):
        if self._socket.state() != QAbstractSocket.UnconnectedState:
            self._socket.abort()

    def _handle_socket_connected(self):
        self.statusMessage.emit(f"Live stream connected: {self.symbol} {self.interval}", False)

    def _handle_socket_disconnected(self):
        if self._page_ready:
            self.statusMessage.emit("Live stream disconnected. Reloading market data reconnects it.", False)

    def _handle_socket_error(self, _error):
        self.statusMessage.emit(f"WebSocket error: {self._socket.errorString()}", True)

    def _handle_socket_message(self, message: str):
        try:
            payload = json.loads(message)
            candle_data = payload["k"]
            candle = {
                "time": int(candle_data["t"] // 1000),
                "open": float(candle_data["o"]),
                "high": float(candle_data["h"]),
                "low": float(candle_data["l"]),
                "close": float(candle_data["c"]),
                "volume": float(candle_data["v"]),
            }
        except Exception as exc:
            self.statusMessage.emit(f"Bad live update: {exc}", True)
            return

        self.candleUpdated.emit(json.dumps(candle))
        self._emit_price_update(candle)

    def _emit_price_update(self, candle):
        change = candle["close"] - candle["open"]
        percent = (change / candle["open"] * 100.0) if candle["open"] else 0.0
        self.livePriceChanged.emit(self.symbol, candle["close"], change, percent)

    @staticmethod
    def _kline_to_candle(item):
        return {
            "time": int(item[0] // 1000),
            "open": float(item[1]),
            "high": float(item[2]),
            "low": float(item[3]),
            "close": float(item[4]),
            "volume": float(item[5]),
        }


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Live Crypto Candlestick Chart")
        self.resize(1520, 920)

        self.web_view = QWebEngineView(self)
        self.page = DebugWebPage(self.web_view)
        self.web_view.setPage(self.page)
        self.web_view.setStyleSheet("background-color: #0b1220;")
        settings = self.web_view.settings()
        settings.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
        settings.setAttribute(QWebEngineSettings.JavascriptEnabled, True)
        settings.setAttribute(QWebEngineSettings.JavascriptCanOpenWindows, False)
        settings.setAttribute(QWebEngineSettings.ShowScrollBars, False)

        self.bridge = ChartBridge()
        self.channel = QWebChannel(self.web_view.page())
        self.channel.registerObject("bridge", self.bridge)
        self.web_view.page().setWebChannel(self.channel)

        self.setCentralWidget(self.web_view)
        self.setStyleSheet("QMainWindow { background-color: #0b1220; }")
        self._load_html()

    def _load_html(self):
        if not HTML_FILE.exists():
            raise FileNotFoundError(f"Missing HTML file: {HTML_FILE}")

        self.web_view.load(QUrl.fromLocalFile(str(HTML_FILE)))


def main():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QApplication(sys.argv)
    app.setApplicationName("Crypto Chart Desktop")
    app.setStyle("Fusion")
    palette = app.palette()
    palette.setColor(palette.Window, QColor("#0b1220"))
    palette.setColor(palette.WindowText, QColor("#e5edf5"))
    app.setPalette(palette)

    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
