from typing import Dict, List, Optional, Tuple, Union
import logging
from config.trading_mode import TradingMode
from core.bot_management.notification.notification_content import NotificationType
from core.bot_management.notification.notification_handler import NotificationHandler
from core.order_handling.exceptions import OrderExecutionFailedError
from core.order_handling.execution_strategy.order_execution_strategy_interface import OrderExecutionStrategyInterface
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
            order_execution_strategy: OrderExecutionStrategyInterface,
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
            order_execution_strategy: 订单执行策略接口（对接交易所）
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
        self.order_execution_strategy = order_execution_strategy
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
        self.logger.info(f"Buy order completed at grid level {grid_level}.")
        # 标记网格层级完成状态
        self.grid_manager.complete_order(grid_level, PerpetualOrderSide.BUY_OPEN)
        # 获取配对卖单层级
        paired_sell_level = self.grid_manager.get_paired_sell_level(grid_level)

        if paired_sell_level and self.grid_manager.can_place_order(paired_sell_level, PerpetualOrderSide.BUY_CLOSE):
            # 挂对冲卖单
            await self._place_sell_order(grid_level, paired_sell_level, order.filled)
        else:
            self.logger.warning(
                f"No valid sell grid level found for buy grid level {grid_level}. Skipping sell order placement.")

    async def _place_sell_order(
            self,
            buy_grid_level: GridLevel,
            sell_grid_level: GridLevel,
            quantity: float
    ) -> None:
        """
        在指定网格层级放置卖出订单。

        参数:
            grid_level: 要放置卖出订单的网格层级。
            quantity: 卖出订单的交易数量。
        """
        # 数量验证与调整
        # adjusted_quantity = self.order_validator.adjust_and_validate_sell_quantity(self.balance_tracker.crypto_balance, quantity)
        adjusted_quantity = 0.1
        # 执行限价卖单
        sell_order = await self.order_execution_strategy.execute_limit_order(
            PerpetualOrderSide.BUY_CLOSE,
            self.trading_pair,
            adjusted_quantity,
            sell_grid_level.price
        )

        if sell_order:
            # 建立网格层级配对关系
            self.grid_manager.pair_grid_levels(buy_grid_level, sell_grid_level, pairing_type="sell")
            # 冻结加密货币余额
            # self.balance_tracker.reserve_funds_for_sell(sell_order.amount)
            # 更新订单簿与网格状态
            self.grid_manager.mark_order_pending(sell_grid_level, sell_order)
            self.order_book.add_order(sell_order, sell_grid_level)
            await self.notification_handler.async_send_notification(NotificationType.ORDER_PLACED, order_details=str(sell_order))
        else:
            self.logger.error(f"Failed to place sell order at grid level {sell_grid_level}.")

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
        pass

    async def _handle_sell_order_completion(
            self,
            order: PerpetualOrder,
            grid_level: GridLevel
    ) -> None:
        """
        处理卖出订单的完成。

        参数:
            order: 已完成的卖出订单实例。
            grid_level: 与已完成卖出订单关联的网格层级。
        """
        self.logger.info(f"Sell order completed at grid level {grid_level}.")
        # 标记网格层级完成状态
        self.grid_manager.complete_order(grid_level, PerpetualOrderSide.BUY_CLOSE)
        # 获取配对买单层级
        paired_buy_level = self._get_or_create_paired_buy_level(grid_level)

        if paired_buy_level:
            # 挂对冲买单
            await self._place_buy_order(grid_level, paired_buy_level, order.filled)
        else:
            self.logger.error(f"Failed to find or create a paired buy grid level for grid level {grid_level}.")

    def _get_or_create_paired_buy_level(self, sell_grid_level: GridLevel) -> Optional[GridLevel]:
        """
        检索或创建与指定卖出网格层级配对的买入网格层级。

        参数:
            sell_grid_level: 需要寻找配对买入层级的卖出网格层级。

        返回:
            配对的买入网格层级，如果找不到有效层级则返回 None。
        """
        paired_buy_level = sell_grid_level.paired_buy_level

        if paired_buy_level and self.grid_manager.can_place_order(paired_buy_level, PerpetualOrderSide.BUY_CLOSE):
            self.logger.info(f"Found valid paired buy level {paired_buy_level} for sell level {sell_grid_level}.")
            return paired_buy_level

        fallback_buy_level = self.grid_manager.get_grid_level_below(sell_grid_level)

        if fallback_buy_level:
            self.logger.info(f"Paired fallback buy level {fallback_buy_level} with sell level {sell_grid_level}.")
            return fallback_buy_level

        self.logger.warning(f"No valid fallback buy level found below sell level {sell_grid_level}.")
        return None

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
            buy_order = await self.order_execution_strategy.execute_market_order(
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

    async def initialize_grid_orders(self, current_price: float):
        # 初始化买单（仅挂低于当前价的网格）
        buy_order_nums = 0
        for price in reversed(self.grid_manager.sorted_buy_grids):
            if price >= current_price:
                self.logger.info(f"Skipping grid level at price: {price} for BUY order: Above current price.")
                continue  # 跳过高于当前价的网格
            # 获取网格层级对象
            grid_level = self.grid_manager.grid_levels[price]

            if self.grid_manager.can_place_order(grid_level, PerpetualOrderSide.BUY_OPEN):
                try:
                    # 执行限价买单 , TODO 这里需要计算正确的订单数量
                    adjusted_buy_order_quantity = 1.0
                    self.logger.info(
                        f"Placing initial buy limit order at grid level {price} for  {self.trading_pair}.")
                    order = await self.order_execution_strategy.execute_limit_order(
                        PerpetualOrderSide.BUY_OPEN,
                        self.trading_pair,
                        adjusted_buy_order_quantity,
                        price
                    )

                    if order is None:
                        self.logger.error(f"Failed to place buy order at {price}: No order returned.")
                        continue
                    # 更新网格状态
                    self.grid_manager.mark_order_pending(grid_level, order)
                    # 记录订单到订单簿
                    self.order_book.add_order(order, grid_level)
                    # 计算数量, 一次最多放置5个多单
                    buy_order_nums += 1
                    if buy_order_nums >= self.grid_manager.max_placed_orders:
                        self.logger.info(
                            f"Place buy order for {self.trading_pair} reach max limit {self.grid_manager.max_placed_orders}.")
                        break

                except OrderExecutionFailedError as e:
                    self.logger.error(f"Failed to initialize buy order at grid level {price} - {str(e)}", exc_info=True)
                    #await self.notification_handler.async_send_notification(NotificationType.ORDER_FAILED, error_details=f"Error while placing initial buy order. {e}")

                except Exception as e:
                    self.logger.error(f"Unexpected error during buy order initialization at grid level {price}: {e}",
                                      exc_info=True)
                    #await self.notification_handler.async_send_notification(NotificationType.ERROR_OCCURRED, error_details=f"Error while placing initial buy order: {str(e)}")

        buy_close_order_nums = 0
        for price in self.grid_manager.sorted_sell_grids:
            if price <= current_price:
                self.logger.info(
                    f"Skipping grid level at price: {price} for SELL order: Below or equal to current price.")
                continue

            grid_level = self.grid_manager.grid_levels[price]
            # total_balance_value = self.balance_tracker.get_total_balance_value(current_price)
            # order_quantity = self.grid_manager.get_order_size_for_grid_level(total_balance_value, current_price)
            if self.grid_manager.can_place_order(grid_level, PerpetualOrderSide.BUY_CLOSE):
                try:
                    # adjusted_sell_order_quantity = self.order_validator.adjust_and_validate_sell_quantity(
                    #     crypto_balance=self.balance_tracker.crypto_balance,
                    #     order_quantity=order_quantity
                    # )

                    adjusted_sell_order_quantity = 1.0

                    self.logger.info(
                        f"Placing initial sell limit order at grid level {price} for {adjusted_sell_order_quantity} {self.trading_pair}.")
                    order = await self.order_execution_strategy.execute_limit_order(
                        PerpetualOrderSide.BUY_CLOSE,
                        self.trading_pair,
                        adjusted_sell_order_quantity,
                        price
                    )

                    if order is None:
                        self.logger.error(f"Failed to place sell order at {price}: No order returned.")
                        continue

                    #self.balance_tracker.reserve_funds_for_sell(adjusted_sell_order_quantity)
                    self.grid_manager.mark_order_pending(grid_level, order)
                    self.order_book.add_order(order, grid_level)

                    buy_close_order_nums += 1
                    if buy_close_order_nums >= self.grid_manager.max_placed_orders:
                        self.logger.info(
                            f"Place buy close order for {self.trading_pair} reach max limit {self.grid_manager.max_placed_orders}.")
                        break

                except OrderExecutionFailedError as e:
                    self.logger.error(f"Failed to initialize sell order at grid level {price} - {str(e)}",
                                      exc_info=True)
                    #await self.notification_handler.async_send_notification(NotificationType.ORDER_FAILED, error_details=f"Error while placing initial sell order. {e}")
                except Exception as e:
                    self.logger.error(f"Unexpected error during sell order initialization at grid level {price}: {e}",
                                      exc_info=True)
                    #await self.notification_handler.async_send_notification(NotificationType.ERROR_OCCURRED,
                    #error_details=f"Error while placing initial sell order: {str(e)}")

        pass

