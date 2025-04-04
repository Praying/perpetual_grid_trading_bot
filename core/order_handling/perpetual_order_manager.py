import uuid
from typing import Dict, List, Optional, Tuple, Union
import logging

from ccxt.base.types import OrderRequest

from config.trading_mode import TradingMode
from core.bot_management.notification.notification_content import NotificationType
from core.bot_management.notification.notification_handler import NotificationHandler
from core.order_handling.exceptions import OrderExecutionFailedError
from core.order_handling.execution_strategy.order_execution_strategy_interface import OrderExecutionStrategyInterface
from core.order_handling.order_executor.perpetual_order_executor import PerpetualOrderExecutor
from core.order_handling.perpetual_order import PerpetualOrder, PerpetualOrderSide, PerpetualOrderType, \
    PerpetualOrderStatus
from core.order_handling.perpetual_order_book import PerpetualOrderBook
from core.order_handling.perpetual_balance_tracker import PerpetualBalanceTracker
from core.grid_management.perpetual_grid_manager import PerpetualGridManager
from core.validation.perpetual_order_validator import PerpetualOrderValidator
from core.grid_management.grid_level import GridLevel
from core.bot_management.event_bus import EventBus, Events
from core.services.perpetual_exchange_service import PerpetualExchangeService
from strategies.strategy_type import StrategyType


class PerpetualOrderManager:
    """永续合约U本位订单管理器，负责处理合约订单的创建、执行和状态跟踪"""

    def __init__(
            self,
            grid_manager: PerpetualGridManager,
            order_validator: PerpetualOrderValidator,
            balance_tracker: PerpetualBalanceTracker,
            order_book: PerpetualOrderBook,
            event_bus: EventBus,
            order_executor: PerpetualOrderExecutor,
            notification_handler: NotificationHandler,
            trading_mode: TradingMode,
            trading_pair: str,
            strategy_type: StrategyType,
            exchange_service: PerpetualExchangeService,
            min_order_value: float = 10.0,  # 最小订单价值（以USDT计）
    ):
        """
        初始化订单管理器

        参数:
            grid_manager: 网格策略管理器实例
            order_validator: 订单参数验证器（保证交易合法性）
            balance_tracker: 资产余额追踪器
            order_book: 订单簿实例
            event_bus: 事件总线（用于发布/订阅系统事件）
            order_executor: 订单执行策略接口（对接交易所）
            notification_handler: 通知处理器（用于发送报警/通知）
            trading_mode: 交易模式（实盘/回测）
            trading_pair: 交易对（如BTC/USDT）
            strategy_type: 策略类型（网格/马丁等）
        """
        self.logger = logging.getLogger(self.__class__.__name__)
        self.grid_manager = grid_manager
        self.order_validator = order_validator
        self.balance_tracker = balance_tracker
        self.order_book = order_book
        self.event_bus = event_bus
        self.order_executor = order_executor
        self.notification_handler = notification_handler  # 通知中心
        self.trading_mode = trading_mode
        self.trading_pair = trading_pair
        self.strategy_type: StrategyType = strategy_type  # 策略类型
        self.exchange_service = exchange_service
        self.min_order_value = min_order_value

        # 订阅订单状态变更事件
        self.event_bus.subscribe(Events.ORDER_FILLED, self._on_order_filled)
        self.event_bus.subscribe(Events.ORDER_CANCELLED, self._on_order_cancelled)

    async def _on_order_filled(
            self,
            order: PerpetualOrder
    ) -> None:
        """
        Handles filled orders and places paired orders as needed.
        订单成交事件处理（触发对冲单挂单）
        Args:
            order: The filled Order instance.
        """
        try:
            grid_level = self.order_book.get_grid_level_for_order(order)

            if not grid_level:  # 非网格订单不处理
                self.logger.warning(
                    f"Could not handle Order completion - No grid level found for the given filled order {order}")
                return

            await self._handle_order_completion(order, grid_level)

        except OrderExecutionFailedError as e:
            self.logger.error(f"Failed while handling filled order - {str(e)}", exc_info=True)
            await self.notification_handler.async_send_notification(NotificationType.ORDER_FAILED,
                                                                    error_details=f"Failed handling filled order. {e}")

        except Exception as e:
            self.logger.error(f"Error while handling filled order {order.identifier}: {e}", exc_info=True)
            await self.notification_handler.async_send_notification(NotificationType.ORDER_FAILED,
                                                                    error_details=f"Failed handling filled order. {e}")

    async def _on_order_cancelled(
            self,
            order: PerpetualOrder
    ) -> None:
        """
        Handles cancelled orders.

        Args:
            order: The cancelled Order instance.
        """
        ## 获取对应的GridLevel
        grid_level = self.order_book.get_grid_level_for_order(order)
        if not grid_level:
            self.logger.warning(
                f"Could not handle Order cancellation - No grid level found for the given cancelled order {order}")
            return
        grid_level.remove_order(order.identifier)
        self.order_book.remove_order(order.identifier)
        #await self.grid_manager.reset_grid(grid_level)
        ## TODO: place new limit Order
        await self.notification_handler.async_send_notification(NotificationType.ORDER_CANCELLED,
                                                                order_details=str(order))

    async def _handle_order_completion(
            self,
            order: PerpetualOrder,
            grid_level: GridLevel
    ) -> None:
        """
        处理订单（买入或卖出）的成交完成。

        参数:
            order: 已成交的订单实例。
            grid_level: 与已成交订单关联的网格层级。
        """
        # 根据买卖方向处理成交
        if order.side == PerpetualOrderSide.BUY_OPEN:
            await self._handle_buy_order_completion(order, grid_level)

        elif order.side == PerpetualOrderSide.BUY_CLOSE:
            await self._handle_sell_order_completion(order, grid_level)

    async def _handle_buy_order_completion(
            self,
            order: PerpetualOrder,
            grid_level: GridLevel
    ) -> None:
        """
        处理买入订单的完成。

        参数:
            order: 已完成的买入订单实例。
            grid_level: 与已完成买入订单关联的网格层级。
        """
        self.logger.info(f"Buy order completed {order.client_order_id()} at grid level {grid_level}.")
        # 标记网格层级完成状态
        self.grid_manager.complete_order(grid_level, PerpetualOrderSide.BUY_OPEN)
        # 更新订单逻辑
        await self.update_orders(grid_level.price)

    async def update_orders(self, price: float):
        """更新网格订单"""
        # 1. 取消所有未成交订单
        all_pending_orders = self.order_book.get_open_orders()
        await self.order_executor.cancel_orders(all_pending_orders)

        # 2. 获取新的候选价格并放置订单
        sell_price_list, buy_price_list = self.grid_manager.get_candidate_prices(price)

        # 处理买单
        buy_grid_level_list = [self.grid_manager.grid_levels[price] for price in buy_price_list]
        await self._place_grid_orders(PerpetualOrderSide.BUY_OPEN, buy_grid_level_list)

        # 处理卖单
        sell_grid_level_list = [self.grid_manager.grid_levels[price] for price in sell_price_list]
        await self._place_grid_orders(PerpetualOrderSide.BUY_CLOSE, sell_grid_level_list)


    def _create_order_request(self, grid_level: GridLevel, amount: float, side: PerpetualOrderSide) -> tuple[
        str, OrderRequest]:
        # OKX clientOrderId = self.safe_string_2(params, 'clOrdId', 'clientOrderId')
        clientOrderId = str(uuid.uuid4().hex)
        return clientOrderId, OrderRequest(symbol=self.trading_pair, type='limit', price=grid_level.price, amount=amount, side='buy' if side == PerpetualOrderSide.BUY_OPEN else 'sell', params={'clientOrderId': clientOrderId})


    async def _cancel_grid_orders(self, grid_level: GridLevel):
        await self.order_executor.cancel_orders(list(grid_level.orders.values()))

    def _get_or_create_paired_buy_level(self, sell_grid_level: GridLevel) -> Optional[GridLevel]:
        """
        Retrieves or creates a paired buy grid level for the given sell grid level.

        Args:
            sell_grid_level: The sell grid level to find a paired buy level for.

        Returns:
            The paired buy grid level, or None if a valid level cannot be found.
        """
        paired_buy_level = sell_grid_level.paired_buy_level

        if paired_buy_level and self.grid_manager.can_place_order(paired_buy_level, PerpetualOrderSide.BUY_OPEN):
            self.logger.info(f"Found valid paired buy level {paired_buy_level} for sell level {sell_grid_level}.")
            return paired_buy_level

        fallback_buy_level = self.grid_manager.get_grid_level_below(sell_grid_level)

        if fallback_buy_level:
            self.logger.info(f"Paired fallback buy level {fallback_buy_level} with sell level {sell_grid_level}.")
            return fallback_buy_level

        self.logger.warning(f"No valid fallback buy level found below sell level {sell_grid_level}.")
        return None

    async def _place_buy_order(
            self,
            sell_grid_level: GridLevel,
            buy_grid_level: GridLevel,
            quantity: float
    ) -> None:
        """
        在指定网格层级放置买入订单。

        参数:
            grid_level: 要放置买入订单的网格层级。
            quantity: 买入订单的交易数量。
        """
        # 数量验证与调整
        # adjusted_quantity = self.order_validator.adjust_and_validate_sell_quantity(self.balance_tracker.crypto_balance, quantity)
        adjusted_quantity = 1.0
        # 执行限价卖单
        buy_order = await self.order_executor.execute_limit_order(
            PerpetualOrderSide.BUY_OPEN,
            self.trading_pair,
            adjusted_quantity,
            buy_grid_level.price
        )
        if buy_order:
            # 建立网格层级配对关系
            self.grid_manager.pair_grid_levels(sell_grid_level, buy_grid_level, pairing_type="buy")
            # 更新订单簿与网格状态
            self.grid_manager.mark_order_pending(buy_grid_level, buy_order)
            self.order_book.add_order(buy_order, buy_grid_level)
            await self.notification_handler.async_send_notification(NotificationType.ORDER_PLACED, order_details=str(buy_order))
        else:
            self.logger.error(f"Failed to place buy order at grid level {buy_grid_level}.")

    async def _handle_sell_order_completion(
            self,
            order: PerpetualOrder,
            grid_level: GridLevel
    ) -> None:
        self.logger.info(f"Sell order completed {order.client_order_id()} at grid level {grid_level}.")
        self.grid_manager.complete_order(grid_level, PerpetualOrderSide.BUY_CLOSE)
        # 更新订单逻辑
        await self.update_orders(grid_level.price)

    async def perform_initial_purchase(self, current_price: float) -> None:
        """
        Handles the initial crypto purchase for grid trading strategy if required.
        执行初始建仓（网格策略可能需要基础仓位）
        Args:
            current_price: The current price of the trading pair.
        """
        # 计算初始买入量
        initial_quantity = self.grid_manager.get_initial_order_quantity(
            current_price=current_price
        )
        if initial_quantity <= 0:
            self.logger.warning("Initial purchase quantity is zero or negative. Skipping initial purchase.")
            return

        self.logger.info(f"Performing initial crypto purchase: {initial_quantity} at price {current_price}.")

        try:  # 执行市价单建仓
            buy_amount = max(initial_quantity / current_price, self.exchange_service.amount_precision)
            buy_order = await self.order_executor.execute_market_order(
                PerpetualOrderSide.BUY_OPEN,
                self.trading_pair,
                buy_amount,  # 这里算出来的initial_quantity是总价值
                current_price
            )
            self.logger.info(f"Initial crypto purchase completed. Order details: {buy_order}")
            self.order_book.add_order(buy_order)
            #await self.notification_handler.async_send_notification(NotificationType.ORDER_PLACED, order_details=f"Initial purchase done: {str(buy_order)}")

            if self.trading_mode == TradingMode.BACKTEST:
                await self._simulate_fill(buy_order, buy_order.timestamp)
            else:
                # Update fiat and crypto balance in LIVE & PAPER_TRADING modes without simulating it
                self.balance_tracker.update_after_initial_purchase(initial_order=buy_order)

        except OrderExecutionFailedError as e:
            self.logger.error(f"Failed while executing initial purchase - {str(e)}", exc_info=True)
            #await self.notification_handler.async_send_notification(NotificationType.ORDER_FAILED, error_details=f"Error while performing initial purchase. {e}")

        except Exception as e:
            self.logger.error(f"Failed to perform initial purchase at current_price: {current_price} - error: {e}",
                              exc_info=True)
            #await self.notification_handler.async_send_notification(NotificationType.ORDER_FAILED, error_details=f"Error while performing initial purchase. {e}")

    async def _simulate_fill(self, buy_order, timestamp):
        pass

    async def _place_grid_orders(self, side: PerpetualOrderSide, grid_level_list: List[GridLevel]) -> None:
        """
        在指定网格层级列表上放置订单

        参数:
            side: 订单方向（买入开仓或卖出平仓）
            grid_level_list: 网格层级列表
        """
        order_requests = []
        order_grid_map = {}
        for grid_level in grid_level_list:
            if self.grid_manager.can_place_order(grid_level, side):
                client_order_id, order_request = self._create_order_request(grid_level, 1.0, side)
                order_requests.append(order_request)
                order_grid_map[client_order_id] = grid_level
                self.logger.info(
                    f"Placing {'buy' if side == PerpetualOrderSide.BUY_OPEN else 'sell'} limit order {client_order_id} "
                    f"at grid level {grid_level} for {self.trading_pair}."
                )

        if len(order_requests) > 0:
            perpetual_orders = await self.order_executor.execute_limit_orders(self.trading_pair, order_requests)
            for order in perpetual_orders:
                corresponding_grid_level = order_grid_map[order.client_order_id()]
                self.grid_manager.mark_order_pending(corresponding_grid_level, order)
                self.order_book.add_order(order, corresponding_grid_level)
                corresponding_grid_level.add_order(order)
                self.logger.info(
                    f"Placed {'buy' if side == PerpetualOrderSide.BUY_OPEN else 'sell'} limit order {order.client_order_id()} "
                    f"at grid level {corresponding_grid_level} for {self.trading_pair}."
                )
    async def initialize_grid_orders(self, current_price: float):
        """初始化网格订单"""
        sell_price_list, buy_price_list = self.grid_manager.get_candidate_prices(current_price)

        # 处理买单
        buy_grid_level_list = [self.grid_manager.grid_levels[price] for price in buy_price_list]
        await self._place_grid_orders(PerpetualOrderSide.BUY_OPEN, buy_grid_level_list)

        # 处理卖单
        sell_grid_level_list = [self.grid_manager.grid_levels[price] for price in sell_price_list]
        await self._place_grid_orders(PerpetualOrderSide.BUY_CLOSE, sell_grid_level_list)
