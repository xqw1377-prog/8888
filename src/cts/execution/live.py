"""候选交易所私有接口。实盘授权为 HOLD 时拒绝任何网络调用。"""

from __future__ import annotations

from cts import LIVE_TRADING_AUTHORIZATION
from cts.errors import CtsError


class LiveExchangeAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def _refuse(self) -> None:
        self.calls += 1
        raise CtsError(
            "实盘下单未授权，未向交易所发送请求",
            reason_code="LIVE_TRADING_HOLD" if LIVE_TRADING_AUTHORIZATION == "HOLD" else "LIVE_VENUE_UNVERIFIED",
        )

    def submit(self, *args, **kwargs) -> None:
        self._refuse()

    def query(self, *args, **kwargs) -> None:
        self._refuse()

    def cancel(self, *args, **kwargs) -> None:
        self._refuse()

    def account_info(self, *args, **kwargs) -> None:
        self._refuse()
