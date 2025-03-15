from typing import Dict, List, Optional, Tuple, Union
import logging
from config.trading_mode import TradingMode
from core.order_handling.exceptions import OrderExecutionFailedError
from core.order_handling.execution_strategy.order_execution_strategy_interface import OrderExecutionStrategyInterface
from core.order_handling.perpetual_order import PerpetualOrder, PerpetualOrderSide, PerpetualOrderType, PerpetualOrderStatus
from core.order_handling.perpetual_order_book import PerpetualOrderBook
from core.order_handling.perpetual_balance_tracker import PerpetualBalanceTracker
from core.grid_management.perpetual_grid_manager import PerpetualGridManager
from core.validation.perpetual_order_validator import PerpetualOrderValidator
from core.validation.perpetual_exceptions import (
    InsufficientMarginError,
    InvalidContractQuantityError,
    MarginRatioError
)
from core.grid_management.grid_level import GridLevel
from core.bot_management.event_bus import EventBus
from core.services.perpetual_exchange_service import PerpetualExchangeService

class PerpetualOrderManager:
    """永续合约U本位订单管理器，负责处理合约订单的创建、执行和状态跟踪"""

    def __init__(
        self,
        exchange_service: PerpetualExchangeService,
        grid_manager: PerpetualGridManager,
        trading_mode: TradingMode,
        trading_pair: str,
        order_execution_strategy: OrderExecutionStrategyInterface,
        order_book: PerpetualOrderBook,
        balance_tracker: PerpetualBalanceTracker,
        order_validator: PerpetualOrderValidator,
        event_bus: EventBus,
        min_order_value: float = 10.0,  # 最小订单价值（以USDT计）
    ):
        self.trading_mode = trading_mode
        self.trading_pair = trading_pair
        self.order_execution_strategy = order_execution_strategy
        self.exchange_service = exchange_service
        self.grid_manager = grid_manager
        self.order_book = order_book
        self.balance_tracker = balance_tracker
        self.order_validator = order_validator
        self.event_bus = event_bus
        self.min_order_value = min_order_value
        self.logger = logging.getLogger(self.__class__.__name__)

    async def create_limit_order(
        self,
        symbol: str,
        side: PerpetualOrderSide,
        price: float,
        quantity: float,
        grid_level: Optional[GridLevel] = None,
        reduce_only: bool = False,
        time_in_force: str = 'GTC'
    ) -> Optional[PerpetualOrder]:
        """创建永续合约限价单

        Args:
            symbol: 交易对
            side: 订单方向（开多、开空、平多、平空）
            price: 限价单价格
            quantity: 合约数量
            grid_level: 关联的网格层级（可选）
            reduce_only: 是否仅允许减仓
            time_in_force: 订单有效期类型

        Returns:
            创建的订单对象，如果创建失败则返回None

        Raises:
            InsufficientMarginError: 保证金不足
            InvalidContractQuantityError: 合约数量无效
            MarginRatioError: 保证金率不足
        """
        try:
            # 验证并调整订单数量
            margin_balance = await self.balance_tracker.get_available_margin(symbol)
            
            if side in [PerpetualOrderSide.OPEN_LONG, PerpetualOrderSide.OPEN_SHORT]:
                adjusted_quantity = self.order_validator.adjust_and_validate_open_long(
                    margin_balance=margin_balance,
                    order_quantity=quantity,
                    price=price,
                    leverage=self.leverage
                ) if side == PerpetualOrderSide.OPEN_LONG else \
                self.order_validator.adjust_and_validate_open_short(
                    margin_balance=margin_balance,
                    order_quantity=quantity,
                    price=price,
                    leverage=self.leverage
                )
            else:
                # 获取当前持仓量
                position = await self.balance_tracker.get_position(symbol, side)
                adjusted_quantity = self.order_validator.adjust_and_validate_close_long(
                    long_position=position,
                    order_quantity=quantity
                ) if side == PerpetualOrderSide.CLOSE_LONG else \
                self.order_validator.adjust_and_validate_close_short(
                    short_position=position,
                    order_quantity=quantity
                )

            # 创建订单
            order = await self.exchange_service.create_limit_order(
                symbol=symbol,
                side=side,
                price=price,
                quantity=adjusted_quantity,
                reduce_only=reduce_only,
                time_in_force=time_in_force,
                leverage=self.leverage
            )

            if order:
                self.order_book.add_order(order, grid_level)
                self.logger.info(
                    f"Created {side.value} limit order: {order.identifier} at {price} "
                    f"for {adjusted_quantity} contracts"
                )
                return order

        except (InsufficientMarginError, InvalidContractQuantityError, MarginRatioError) as e:
            self.logger.error(f"Failed to create limit order: {str(e)}")
            raise
        except Exception as e:
            self.logger.error(f"Unexpected error creating limit order: {str(e)}")

        return None

    async def create_market_order(
        self,
        symbol: str,
        side: PerpetualOrderSide,
        quantity: float,
        reduce_only: bool = False
    ) -> Optional[PerpetualOrder]:
        """创建永续合约市价单

        Args:
            symbol: 交易对
            side: 订单方向（开多、开空、平多、平空）
            quantity: 合约数量
            reduce_only: 是否仅允许减仓

        Returns:
            创建的订单对象，如果创建失败则返回None
        """
        try:
            # 获取当前市场价格用于验证
            current_price = await self.exchange_service.get_market_price(symbol)
            
            # 验证并调整订单数量
            margin_balance = await self.balance_tracker.get_available_margin(symbol)
            
            if side in [PerpetualOrderSide.OPEN_LONG, PerpetualOrderSide.OPEN_SHORT]:
                adjusted_quantity = self.order_validator.adjust_and_validate_open_long(
                    margin_balance=margin_balance,
                    order_quantity=quantity,
                    price=current_price,
                    leverage=self.leverage
                ) if side == PerpetualOrderSide.OPEN_LONG else \
                self.order_validator.adjust_and_validate_open_short(
                    margin_balance=margin_balance,
                    order_quantity=quantity,
                    price=current_price,
                    leverage=self.leverage
                )
            else:
                position = await self.balance_tracker.get_position(symbol, side)
                adjusted_quantity = self.order_validator.adjust_and_validate_close_long(
                    long_position=position,
                    order_quantity=quantity
                ) if side == PerpetualOrderSide.CLOSE_LONG else \
                self.order_validator.adjust_and_validate_close_short(
                    short_position=position,
                    order_quantity=quantity
                )

            # 创建市价单
            order = await self.exchange_service.create_market_order(
                symbol=symbol,
                side=side,
                quantity=adjusted_quantity,
                reduce_only=reduce_only,
                leverage=self.leverage
            )

            if order:
                self.order_book.add_order(order)
                self.logger.info(
                    f"Created {side.value} market order: {order.identifier} "
                    f"for {adjusted_quantity} contracts"
                )
                return order

        except (InsufficientMarginError, InvalidContractQuantityError, MarginRatioError) as e:
            self.logger.error(f"Failed to create market order: {str(e)}")
            raise
        except Exception as e:
            self.logger.error(f"Unexpected error creating market order: {str(e)}")

        return None

    async def create_stop_loss_order(
        self,
        symbol: str,
        side: PerpetualOrderSide,
        stop_price: float,
        quantity: float,
        is_market: bool = True,
        limit_price: Optional[float] = None
    ) -> Optional[PerpetualOrder]:
        """创建止损单

        Args:
            symbol: 交易对
            side: 订单方向（通常是平多或平空）
            stop_price: 触发价格
            quantity: 合约数量
            is_market: 是否为市价止损单
            limit_price: 限价止损单的限价（仅当is_market=False时有效）

        Returns:
            创建的止损单对象，如果创建失败则返回None
        """
        try:
            order_type = PerpetualOrderType.STOP_MARKET if is_market else PerpetualOrderType.STOP_LIMIT

            # 验证持仓量
            position = await self.balance_tracker.get_position(symbol, side)
            adjusted_quantity = self.order_validator.adjust_and_validate_close_long(
                long_position=position,
                order_quantity=quantity
            ) if side == PerpetualOrderSide.CLOSE_LONG else \
            self.order_validator.adjust_and_validate_close_short(
                short_position=position,
                order_quantity=quantity
            )

            # 创建止损单
            order = await self.exchange_service.create_stop_loss_order(
                symbol=symbol,
                side=side,
                stop_price=stop_price,
                quantity=adjusted_quantity,
                is_market=is_market,
                limit_price=limit_price,
                leverage=self.leverage
            )

            if order:
                self.order_book.add_order(order)
                self.logger.info(
                    f"Created {order_type.value} stop loss order: {order.identifier} "
                    f"at {stop_price} for {adjusted_quantity} contracts"
                )
                return order

        except (InsufficientMarginError, InvalidContractQuantityError) as e:
            self.logger.error(f"Failed to create stop loss order: {str(e)}")
            raise
        except Exception as e:
            self.logger.error(f"Unexpected error creating stop loss order: {str(e)}")

        return None

    async def create_take_profit_order(
        self,
        symbol: str,
        side: PerpetualOrderSide,
        take_profit_price: float,
        quantity: float,
        is_market: bool = True,
        limit_price: Optional[float] = None
    ) -> Optional[PerpetualOrder]:
        """创建止盈单

        Args:
            symbol: 交易对
            side: 订单方向（通常是平多或平空）
            take_profit_price: 触发价格
            quantity: 合约数量
            is_market: 是否为市价止盈单
            limit_price: 限价止盈单的限价（仅当is_market=False时有效）

        Returns:
            创建的止盈单对象，如果创建失败则返回None
        """
        try:
            order_type = PerpetualOrderType.TAKE_PROFIT_MARKET if is_market \
                else PerpetualOrderType.TAKE_PROFIT_LIMIT

            # 验证持仓量
            position = await self.balance_tracker.get_position(symbol, side)
            adjusted_quantity = self.order_validator.adjust_and_validate_close_long(
                long_position=position,
                order_quantity=quantity
            ) if side == PerpetualOrderSide.CLOSE_LONG else \
            self.order_validator.adjust_and_validate_close_short(
                short_position=position,
                order_quantity=quantity
            )

            # 创建止盈单
            order = await self.exchange_service.create_take_profit_order(
                symbol=symbol,
                side=side,
                take_profit_price=take_profit_price,
                quantity=adjusted_quantity,
                is_market=is_market,
                limit_price=limit_price,
                leverage=self.leverage
            )

            if order:
                self.order_book.add_order(order)
                self.logger.info(
                    f"Created {order_type.value} take profit order: {order.identifier} "
                    f"at {take_profit_price} for {adjusted_quantity} contracts"
                )
                return order

        except (InsufficientMarginError, InvalidContractQuantityError) as e:
            self.logger.error(f"Failed to create take profit order: {str(e)}")
            raise
        except Exception as e:
            self.logger.error(f"Unexpected error creating take profit order: {str(e)}")

        return None

    async def create_trailing_stop_order(
        self,
        symbol: str,
        side: PerpetualOrderSide,
        callback_rate: float,
        quantity: float,
        activation_price: Optional[float] = None
    ) -> Optional[PerpetualOrder]:
        """创建追踪止损单

        Args:
            symbol: 交易对
            side: 订单方向（通常是平多或平空）
            callback_rate: 回调比例
            quantity: 合约数量
            activation_price: 激活价格（可选）

        Returns:
            创建的追踪止损单对象，如果创建失败则返回None
        """
        try:
            # 验证持仓量
            position = await self.balance_tracker.get_position(symbol, side)
            adjusted_quantity = self.order_validator.adjust_and_validate_close_long(
                long_position=position,
                order_quantity=quantity
            ) if side == PerpetualOrderSide.CLOSE_LONG else \
            self.order_validator.adjust_and_validate_close_short(
                short_position=position,
                order_quantity=quantity
            )

            # 创建追踪止损单
            order = await self.exchange_service.create_trailing_stop_order(
                symbol=symbol,
                side=side,
                callback_rate=callback_rate,
                quantity=adjusted_quantity,
                activation_price=activation_price,
                leverage=self.leverage
            )

            if order:
                self.order_book.add_order(order)
                self.logger.info(
                    f"Created trailing stop order: {order.identifier} with callback rate "
                    f"{callback_rate}% for {adjusted_quantity} contracts"
                )
                return order

        except (InsufficientMarginError, InvalidContractQuantityError) as e:
            self.logger.error(f"Failed to create trailing stop order: {str(e)}")
            raise
        except Exception as e:
            self.logger.error(f"Unexpected error creating trailing stop order: {str(e)}")

        return None

    async def perform_initial_purchase(
        self,
        current_price: float
    ) -> None:
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

        try:            # 执行市价单建仓
            buy_amount = max(initial_quantity/current_price, self.exchange_service.amount_precision)
            buy_order = await self.order_execution_strategy.execute_market_order(
                PerpetualOrderSide.BUY_OPEN,
                self.trading_pair,
                buy_amount,# 这里算出来的initial_quantity是总价值
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
            self.logger.error(f"Failed to perform initial purchase at current_price: {current_price} - error: {e}", exc_info=True)
            #await self.notification_handler.async_send_notification(NotificationType.ORDER_FAILED, error_details=f"Error while performing initial purchase. {e}")

    async def _simulate_fill(self, buy_order, timestamp):
        pass

    async def initialize_grid_orders(self, current_price: float):
        # 初始化买单（仅挂低于当前价的网格）
        for price in self.grid_manager.sorted_buy_grids:
            if price >= current_price:
                self.logger.info(f"Skipping grid level at price: {price} for BUY order: Above current price.")
                continue# 跳过高于当前价的网格
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

                except OrderExecutionFailedError as e:
                    self.logger.error(f"Failed to initialize buy order at grid level {price} - {str(e)}", exc_info=True)
                    #await self.notification_handler.async_send_notification(NotificationType.ORDER_FAILED, error_details=f"Error while placing initial buy order. {e}")

                except Exception as e:
                    self.logger.error(f"Unexpected error during buy order initialization at grid level {price}: {e}", exc_info=True)
                    #await self.notification_handler.async_send_notification(NotificationType.ERROR_OCCURRED, error_details=f"Error while placing initial buy order: {str(e)}")

        for price in self.grid_manager.sorted_sell_grids:
            if price <= current_price:
                self.logger.info(f"Skipping grid level at price: {price} for SELL order: Below or equal to current price.")
                continue

            grid_level = self.grid_manager.grid_levels[price]
            # total_balance_value = self.balance_tracker.get_total_balance_value(current_price)
            # order_quantity = self.grid_manager.get_order_size_for_grid_level(total_balance_value, current_price)
            if self.grid_manager.can_place_order(grid_level, PerpetualOrderSide.SELL_OPEN):
                try:
                    # adjusted_sell_order_quantity = self.order_validator.adjust_and_validate_sell_quantity(
                    #     crypto_balance=self.balance_tracker.crypto_balance,
                    #     order_quantity=order_quantity
                    # )

                    adjusted_sell_order_quantity = 1.0

                    self.logger.info(
                        f"Placing initial sell limit order at grid level {price} for {adjusted_sell_order_quantity} {self.trading_pair}.")
                    order = await self.order_execution_strategy.execute_limit_order(
                        PerpetualOrderSide.SELL_OPEN,
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

                except OrderExecutionFailedError as e:
                    self.logger.error(f"Failed to initialize sell order at grid level {price} - {str(e)}", exc_info=True)
                    #await self.notification_handler.async_send_notification(NotificationType.ORDER_FAILED, error_details=f"Error while placing initial sell order. {e}")
                except Exception as e:
                    self.logger.error(f"Unexpected error during sell order initialization at grid level {price}: {e}",
                                      exc_info=True)
                    #await self.notification_handler.async_send_notification(NotificationType.ERROR_OCCURRED,
                                                                            #error_details=f"Error while placing initial sell order: {str(e)}")

        pass


