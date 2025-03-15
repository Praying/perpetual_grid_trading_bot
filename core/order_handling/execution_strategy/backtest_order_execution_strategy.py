import time
from typing import Optional
from .order_execution_strategy_interface import OrderExecutionStrategyInterface
from core.order_handling.perpetual_order import PerpetualOrder, PerpetualOrderSide, PerpetualOrderStatus, PerpetualOrderType

class BacktestOrderExecutionStrategy(OrderExecutionStrategyInterface):
    async def execute_market_order(
        self, 
        order_side: PerpetualOrderSide,
        pair: str, 
        quantity: float,
        price: float
    ) -> Optional[PerpetualOrder]:
        order_id = f"backtest-{int(time.time())}"
        timestamp = int(time.time() * 1000)
        return PerpetualOrder(
            identifier=order_id,
            status=PerpetualOrderStatus.OPEN,
            order_type=PerpetualOrderType.MARKET,
            side=order_side,
            price=price,
            average=price,
            amount=quantity,
            filled=quantity,
            remaining=0,
            timestamp=timestamp,
            datetime="111",
            last_trade_timestamp=1,
            symbol=pair,
            time_in_force="GTC"
        )
    
    async def execute_limit_order(
        self, 
        order_side: PerpetualOrderSide,
        pair: str, 
        quantity: float, 
        price: float
    ) -> Optional[PerpetualOrder]:
        order_id = f"backtest-{int(time.time())}"
        return PerpetualOrder(
            identifier=order_id,
            status=PerpetualOrderStatus.OPEN,
            order_type=PerpetualOrderType.LIMIT,
            side=order_side,
            price=price,
            average=price,
            amount=quantity,
            filled=0,
            remaining=quantity,
            timestamp=0,
            datetime="",
            last_trade_timestamp=1,
            symbol=pair,
            time_in_force="GTC"
        )
    
    async def get_order(
        self, 
        order_id: str,
        pair: str
    ) -> Optional[PerpetualOrder]:
        return PerpetualOrder(
            identifier=order_id,
            status=PerpetualOrderStatus.OPEN,
            order_type=PerpetualOrderType.LIMIT,
            side=PerpetualOrderSide.BUY_OPEN,
            price=100,
            average=100,
            amount=1,
            filled=1,
            remaining=0,
            timestamp=0,
            datetime="111",
            last_trade_timestamp=1,
            symbol=pair,
            time_in_force="GTC"
        )