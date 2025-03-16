from abc import ABC, abstractmethod
from typing import Optional
from core.order_handling.perpetual_order import PerpetualOrder, PerpetualOrderSide

class OrderExecutionStrategyInterface(ABC):
    @abstractmethod
    async def execute_market_order(
        self, 
        order_side: PerpetualOrderSide,
        pair: str, 
        quantity: float,
        price: float
    ) -> Optional[PerpetualOrder]:
        pass

    @abstractmethod
    async def execute_limit_order(
        self, 
        order_side: PerpetualOrderSide,
        pair: str, 
        quantity: float, 
        price: float
    ) -> Optional[PerpetualOrder]:
        pass

    @abstractmethod
    async def get_order(
        self, 
        order_id: str,
        pair: str
    ) -> Optional[PerpetualOrder]:
        pass

    @abstractmethod
    async def get_funding_rate(self, pair: str) -> float:
        pass