import logging, asyncio
from typing import Optional
from core.order_handling.perpetual_order import PerpetualOrder, PerpetualOrderType, PerpetualOrderSide, PerpetualOrderStatus
from core.services.exchange_interface import ExchangeInterface
from core.services.exceptions import DataFetchError
from .order_execution_strategy_interface import OrderExecutionStrategyInterface
from ..exceptions import OrderExecutionFailedError

class LiveOrderExecutionStrategy(OrderExecutionStrategyInterface):
    def __init__(
        self, 
        exchange_service: ExchangeInterface, 
        max_retries: int = 3, 
        retry_delay: int = 1, 
        max_slippage: float = 0.01
    ) -> None:
        self.exchange_service = exchange_service
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.max_slippage = max_slippage
        self.logger = logging.getLogger(self.__class__.__name__)

    async def execute_market_order(
        self, 
        order_side: PerpetualOrderSide, 
        pair: str, 
        amount: float,
        price: float
    ) -> Optional[PerpetualOrder]:
        for attempt in range(self.max_retries):
            try:
                raw_order = await self.exchange_service.place_order(pair, PerpetualOrderType.MARKET.value.lower(), order_side.name.lower(), amount, price)
                order_result = await self._parse_order_result(raw_order)
                
                if order_result.status == PerpetualOrderStatus.CLOSED:
                    return order_result  # Order fully filled

                elif order_result.status == PerpetualOrderStatus.OPEN:
                    await self._handle_partial_fill(order_result, pair)

                await asyncio.sleep(self.retry_delay)
                self.logger.info(f"Retrying order. Attempt {attempt + 1}/{self.max_retries}.")
                price = await self._adjust_price(order_side, price, attempt)

            except Exception as e:
                self.logger.error(f"Attempt {attempt + 1} failed with error: {str(e)}")
                await asyncio.sleep(self.retry_delay)

        raise OrderExecutionFailedError("Failed to execute Market order after maximum retries.", order_side, PerpetualOrderType.MARKET, pair, amount, price)
    
    async def execute_limit_order(
        self, 
        order_side: PerpetualOrderSide, 
        pair: str, 
        amount: float,
        price: float
    ) -> Optional[PerpetualOrder]:
        try:
            raw_order = await self.exchange_service.place_order(pair, PerpetualOrderType.LIMIT.value.lower(), order_side.name.lower(), amount, price)
            order_result = await self._parse_order_result(raw_order)
            return order_result
        
        except DataFetchError as e:
            self.logger.error(f"DataFetchError during order execution for {pair} - {e}")
            raise OrderExecutionFailedError(f"Failed to execute Limit order on {pair}: {e}", order_side, PerpetualOrderType.LIMIT, pair, amount, price)

        except Exception as e:
            self.logger.error(f"Unexpected error in execute_limit_order: {e}")
            raise OrderExecutionFailedError(f"Unexpected error during order execution: {e}", order_side, PerpetualOrderType.LIMIT, pair, amount, price)

    async def get_order(
        self, 
        order_id: str,
        pair: str
    ) -> Optional[PerpetualOrder]:
        try:
            raw_order = await self.exchange_service.fetch_order(order_id, pair)
            order_result = await self._parse_order_result(raw_order)
            return order_result

        except DataFetchError as e:
            raise e

        except Exception as e:
            raise DataFetchError(f"Unexpected error during order status retrieval: {str(e)}")

    async def _parse_order_result(
        self, 
        raw_order_result: dict
    ) -> PerpetualOrder:
        """
        Parses the raw order response from the exchange into an Order object.

        Args:
            raw_order_result: The raw response from the exchange.

        Returns:
            An Order object with standardized fields.
        """
        return PerpetualOrder(
            identifier=raw_order_result.get("id", ""),
            status=PerpetualOrderStatus(raw_order_result.get("status", "unknown").lower()),
            order_type=PerpetualOrderType(raw_order_result.get("type", "unknown").lower()),
            side=PerpetualOrderSide(raw_order_result.get("side", "unknown").lower()),
            price=raw_order_result.get("price", 0.0),
            average=raw_order_result.get("average", None),
            amount=raw_order_result.get("amount", 0.0),
            filled=raw_order_result.get("filled", 0.0),
            remaining=raw_order_result.get("remaining", 0.0),
            timestamp=raw_order_result.get("timestamp", 0),
            datetime=raw_order_result.get("datetime", None),
            last_trade_timestamp=raw_order_result.get("lastTradeTimestamp", None),
            symbol=raw_order_result.get("symbol", ""),
            time_in_force=raw_order_result.get("timeInForce", None),
            trades=raw_order_result.get("trades", []),
            fee=raw_order_result.get("fee", None),
            cost=raw_order_result.get("cost", None),
            info=raw_order_result.get("info", raw_order_result),
        )

    async def _adjust_price(
        self, 
        order_side: PerpetualOrderSide, 
        price: float, 
        attempt: int
    ) -> float:
        adjustment = self.max_slippage / self.max_retries * attempt
        return price * (1 + adjustment) if order_side == PerpetualOrderSide.BUY_OPEN else price * (1 - adjustment)
    
    async def _handle_partial_fill(
        self, 
        order: PerpetualOrder,
        pair: str,
    ) -> Optional[dict]:
        self.logger.info(f"Order partially filled with {order.filled}. Attempting to cancel and retry full quantity.")

        if not await self._retry_cancel_order(order.identifier, pair):
            self.logger.error(f"Unable to cancel partially filled order {order.identifier} after retries.")

    async def _retry_cancel_order(
        self, 
        order_id: str, 
        pair: str
    ) -> bool:
        for cancel_attempt in range(self.max_retries):
            try:
                cancel_result = await self.exchange_service.cancel_order(order_id, pair)

                if cancel_result['status'] == 'canceled':
                    self.logger.info(f"Successfully canceled order {order_id}.")
                    return True

                self.logger.warning(f"Cancel attempt {cancel_attempt + 1} for order {order_id} failed.")

            except Exception as e:
                self.logger.warning(f"Error during cancel attempt {cancel_attempt + 1} for order {order_id}: {str(e)}")

            await asyncio.sleep(self.retry_delay)
        return False