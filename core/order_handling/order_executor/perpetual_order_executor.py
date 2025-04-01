from typing import Optional, Dict, Any, Union
import logging
import time
import ccxt
import asyncio
from decimal import Decimal

from core.order_handling.perpetual_order import PerpetualOrder, PerpetualOrderSide, PerpetualOrderType, \
    PerpetualOrderStatus, MarginType, PositionSide
from core.order_handling.exceptions import OrderExecutionFailedError
from config.trading_mode import TradingMode


def _convert_to_ccxt_side(side: PerpetualOrderSide) -> str:
    """
    将内部订单方向转换为CCXT API需要的方向

    参数:
        side: 内部订单方向枚举

    返回:
        CCXT API使用的订单方向字符串
    """
    if side in [PerpetualOrderSide.BUY_OPEN, PerpetualOrderSide.SELL_CLOSE]:
        return 'buy'
    elif side in [PerpetualOrderSide.SELL_OPEN, PerpetualOrderSide.BUY_CLOSE]:
        return 'sell'
    else:
        raise ValueError(f"不支持的订单方向: {side}")


def _create_order_from_response(
        response: Dict[str, Any],
) -> PerpetualOrder:
    """
    从交易所响应创建订单对象

    参数:
        response: 交易所API响应
    返回:
        创建的订单对象
    """
    order_id = response.get('id')
    timestamp = response.get('timestamp', int(time.time() * 1000))

    """解析永续合约订单响应，包含合约特有字段。"""
    status = response.get("status")
    if status is None:
        status = PerpetualOrderStatus.OPEN
    return PerpetualOrder(
        identifier=response.get("id", ""),
        status=PerpetualOrderStatus(status),
        order_type=PerpetualOrderType(response.get("type", "unknown").lower()),
        side=PerpetualOrderSide(response.get("side", "unknown").lower()),
        price=0.0 if not response.get("price", 0.0) else float(response.get("price", 0.0)),
        average=response.get("average", None),
        amount=0.0 if not response.get("amount", 0.0) else float(response.get("amount", 0.0)),
        filled=0.0 if not response.get("filled", 0.0) else float(response.get("filled", 0.0)),
        remaining=0.0 if not response.get("remaining", 0.0) else float(response.get("remaining", 0.0)),
        timestamp=0 if not response.get("timestamp", 0) else int(response.get("timestamp", 0)),
        datetime=response.get("datetime", None),
        last_trade_timestamp=response.get("lastTradeTimestamp", None),
        symbol=response.get("symbol", ""),
        time_in_force=response.get("timeInForce", None),
        trades=response.get("trades", []),
        fee=response.get("fee", None),
        cost=response.get("cost", None),
        contracts=0.0,
        contract_size=0.0,
        leverage=0.0,
        margin_type=MarginType.CROSS,
        position_side=PositionSide.LONG,
        info={
            "leverage": response.get("info", {}).get("lever"),
            "marginMode": response.get("info", {}).get("tdMode"),
        }
    )


def _create_simulated_order(
        side: PerpetualOrderSide,
        trading_pair: str,
        amount: float,
        price: float,
        order_type: PerpetualOrderType
) -> PerpetualOrder:
    """
    创建模拟订单（用于回测）

    参数:
        side: 订单方向
        trading_pair: 交易对
        amount: 交易数量
        price: 订单价格
        order_type: 订单类型

    返回:
        创建的模拟订单对象
    """
    order_id = f"sim_{int(time.time() * 1000)}_{trading_pair}_{side.name}"
    timestamp = int(time.time() * 1000)

    return PerpetualOrder(
        identifier=order_id,
        symbol=trading_pair,
        side=side,
        amount=amount,
        price=price,
        order_type=order_type,
        timestamp=timestamp,
    )


def _get_order_params(side: PerpetualOrderSide) -> Dict[str, Any]:
    """
    获取特定交易所的额外订单参数

    参数:
        side: 订单方向

    返回:
        交易所特定的订单参数字典
    """
    params = {}

    # 根据订单方向设置开仓/平仓参数
    if side in [PerpetualOrderSide.BUY_OPEN, PerpetualOrderSide.SELL_OPEN]:
        params['positionSide'] = 'LONG' if side == PerpetualOrderSide.BUY_OPEN else 'SHORT'
        params['reduceOnly'] = False
    else:  # 平仓订单
        params['positionSide'] = 'LONG' if side == PerpetualOrderSide.SELL_CLOSE else 'SHORT'
        params['reduceOnly'] = True

    return params


class PerpetualOrderExecutor:
    """
    永续合约订单执行器，负责与交易所API交互执行订单
    基于ccxt库实现订单接口
    """

    def __init__(
            self,
            exchange: ccxt.Exchange,
            trading_mode: TradingMode,
            max_retries: int = 3,
            retry_delay: float = 1.0
    ):
        """
        初始化订单执行器
        
        参数:
            exchange: ccxt交易所实例
            trading_mode: 交易模式（实盘/回测）
            max_retries: 订单执行失败时的最大重试次数
            retry_delay: 重试间隔时间（秒）
        """
        self.logger = logging.getLogger(self.__class__.__name__)
        self.exchange = exchange
        self.trading_mode = trading_mode
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        
        # 确保交易所支持永续合约
        if not hasattr(self.exchange, 'has') or not self.exchange.has.get('future'):
            self.logger.error(f"交易所 {self.exchange.id} 不支持永续合约交易")
            raise ValueError(f"交易所 {self.exchange.id} 不支持永续合约交易")

    async def execute_limit_order(
            self,
            side: PerpetualOrderSide,
            trading_pair: str,
            amount: float,
            price: float
    ) -> Optional[PerpetualOrder]:
        """
        执行限价单
        
        参数:
            side: 订单方向（买入开仓/卖出平仓等）
            trading_pair: 交易对
            amount: 交易数量（合约张数）
            price: 限价单价格
            
        返回:
            成功创建的订单对象，失败则返回None
        """
        self.logger.info(f"执行限价单: {side.name} {amount} {trading_pair} @ {price}")
        
        # 回测模式下模拟订单执行
        if self.trading_mode == TradingMode.BACKTEST:
            return _create_simulated_order(side, trading_pair, amount, price, PerpetualOrderType.LIMIT)
        
        # 实盘模式下执行真实订单
        retry_count = 0
        while retry_count < self.max_retries:
            try:
                # 转换为交易所API需要的参数
                ccxt_side = _convert_to_ccxt_side(side)
                #ccxt_params = _get_order_params(side)
                
                # 执行限价单
                response = await self.exchange.create_order(
                    symbol=trading_pair,
                    type='limit',
                    side=ccxt_side,
                    amount=amount,
                    price=price,
                )
                
                # 创建订单对象
                order = _create_order_from_response(response)
                self.logger.info(f"限价单创建成功: {order.identifier}")
                return order
                
            except ccxt.NetworkError as e:
                retry_count += 1
                self.logger.warning(f"网络错误，尝试重新执行订单 ({retry_count}/{self.max_retries}): {e}")
                await asyncio.sleep(self.retry_delay)
                
            except ccxt.ExchangeError as e:
                self.logger.error(f"交易所错误: {e}")
                raise OrderExecutionFailedError(f"交易所API错误: {e}")
                
            except Exception as e:
                self.logger.error(f"执行限价单时发生未知错误: {e}", exc_info=True)
                raise OrderExecutionFailedError(f"执行限价单失败: {e}")
        
        self.logger.error(f"达到最大重试次数，限价单执行失败")
        raise OrderExecutionFailedError("达到最大重试次数，限价单执行失败")

    async def execute_market_order(
            self,
            side: PerpetualOrderSide,
            trading_pair: str,
            amount: float,
            current_price: float
    ) -> Optional[PerpetualOrder]:
        """
        执行市价单
        
        参数:
            side: 订单方向（买入开仓/卖出平仓等）
            trading_pair: 交易对
            amount: 交易数量（合约张数）
            current_price: 当前市场价格（用于回测或记录）
            
        返回:
            成功创建的订单对象，失败则返回None
        """
        self.logger.info(f"执行市价单: {side.name} {amount} {trading_pair}")

        # 回测模式下模拟订单执行
        if self.trading_mode == TradingMode.BACKTEST:
            return _create_simulated_order(side, trading_pair, amount, current_price, PerpetualOrderType.MARKET)
        
        # 实盘模式下执行真实订单
        retry_count = 0
        while retry_count < self.max_retries:
            try:
                # 转换为交易所API需要的参数
                ccxt_side = _convert_to_ccxt_side(side)
                ccxt_params = _get_order_params(side)
                
                # 执行市价单
                response = await self.exchange.create_order(
                    symbol=trading_pair,
                    type='market',
                    side=ccxt_side,
                    amount=amount,
                    price=current_price,
                )
                
                # 创建订单对象
                order = _create_order_from_response(response, side, trading_pair, amount, current_price, PerpetualOrderType.MARKET)
                self.logger.info(f"市价单创建成功: {order.identifier}")
                return order
                
            except ccxt.NetworkError as e:
                retry_count += 1
                self.logger.warning(f"网络错误，尝试重新执行订单 ({retry_count}/{self.max_retries}): {e}")
                await asyncio.sleep(self.retry_delay)
                
            except ccxt.ExchangeError as e:
                self.logger.error(f"交易所错误: {e}")
                raise OrderExecutionFailedError(f"交易所API错误: {e}")
                
            except Exception as e:
                self.logger.error(f"执行市价单时发生未知错误: {e}", exc_info=True)
                raise OrderExecutionFailedError(f"执行市价单失败: {e}")
        
        self.logger.error(f"达到最大重试次数，市价单执行失败")
        raise OrderExecutionFailedError("达到最大重试次数，市价单执行失败")

    async def cancel_order(self, order: PerpetualOrder) -> bool:
        """
        取消订单
        
        参数:
            order: 要取消的订单对象
            
        返回:
            取消是否成功
        """
        self.logger.info(f"取消订单: {order.identifier}")
        
        # 回测模式下模拟订单取消
        if self.trading_mode == TradingMode.BACKTEST:
            order.status = PerpetualOrderStatus.CANCELED
            return True
        
        # 实盘模式下执行真实订单取消
        retry_count = 0
        while retry_count < self.max_retries:
            try:
                # 执行订单取消
                await self.exchange.cancel_order(
                    id=order.identifier,
                    symbol=order.symbol
                )
                
                order.status = PerpetualOrderStatus.CANCELED
                self.logger.info(f"订单取消成功: {order.identifier}")
                return True
                
            except ccxt.OrderNotFound:
                self.logger.warning(f"订单不存在或已经被取消: {order.identifier}")
                order.status = PerpetualOrderStatus.CANCELED
                return True
                
            except ccxt.NetworkError as e:
                retry_count += 1
                self.logger.warning(f"网络错误，尝试重新取消订单 ({retry_count}/{self.max_retries}): {e}")
                await asyncio.sleep(self.retry_delay)
                
            except ccxt.ExchangeError as e:
                self.logger.error(f"取消订单时发生交易所错误: {e}")
                raise OrderExecutionFailedError(f"取消订单时发生交易所API错误: {e}")
                
            except Exception as e:
                self.logger.error(f"取消订单时发生未知错误: {e}", exc_info=True)
                raise OrderExecutionFailedError(f"取消订单失败: {e}")
        
        self.logger.error(f"达到最大重试次数，订单取消失败: {order.identifier}")
        return False

    async def cancel_orders(self, orders: list[PerpetualOrder]) -> dict[str, bool]:
        """
        批量取消订单
        
        参数:
            orders: 要取消的订单对象列表
            
        返回:
            字典，键为订单ID，值为取消是否成功
        """
        if not orders:
            return {}

        self.logger.info(f"批量取消订单，数量: {len(orders)}")
        results = {}

        # 回测模式下批量模拟订单取消
        if self.trading_mode == TradingMode.BACKTEST:
            for order in orders:
                order.status = PerpetualOrderStatus.CANCELED
                results[order.identifier] = True
            return results
        
        # 检查交易所是否支持批量取消订单
        supports_cancel_orders = hasattr(self.exchange, 'has') and self.exchange.has.get('cancelOrders', False)
        
        if supports_cancel_orders:
            self.logger.info("使用交易所批量取消订单API")
            # 按交易对分组订单
            orders_by_symbol = {}
            for order in orders:
                symbol = order.symbol
                if symbol not in orders_by_symbol:
                    orders_by_symbol[symbol] = []
                orders_by_symbol[symbol].append(order)
            
            # 对每个交易对分别执行批量取消
            for symbol, symbol_orders in orders_by_symbol.items():
                try:
                    # 提取订单ID列表
                    order_ids = [order.identifier for order in symbol_orders]
                    
                    # 执行批量取消
                    cancel_results = await self.exchange.cancel_orders(order_ids, symbol)
                    
                    # 处理取消结果
                    canceled_ids = set()
                    for result in cancel_results:
                        if result.get('status') in ['canceled', 'closed']:
                            canceled_ids.add(result.get('id'))
                    
                    # 更新订单状态和结果
                    for order in symbol_orders:
                        if order.identifier in canceled_ids:
                            order.status = PerpetualOrderStatus.CANCELED
                            results[order.identifier] = True
                        else:
                            results[order.identifier] = False
                            
                except ccxt.NetworkError as e:
                    self.logger.error(f"批量取消订单时发生网络错误: {e}")
                    # 对该交易对的所有订单标记为取消失败
                    for order in symbol_orders:
                        results[order.identifier] = False
                        
                except ccxt.ExchangeError as e:
                    self.logger.error(f"批量取消订单时发生交易所错误: {e}")
                    # 对该交易对的所有订单标记为取消失败
                    for order in symbol_orders:
                        results[order.identifier] = False
                        
                except Exception as e:
                    self.logger.error(f"批量取消订单时发生未知错误: {e}", exc_info=True)
                    # 对该交易对的所有订单标记为取消失败
                    for order in symbol_orders:
                        results[order.identifier] = False
        else:
            # 交易所不支持批量取消，回退到单个取消
            self.logger.info("交易所不支持批量取消订单API，使用单个取消")
            tasks = [self.cancel_order(order) for order in orders]
            cancel_results = await asyncio.gather(*tasks, return_exceptions=True)

            # 处理取消结果
            for order, result in zip(orders, cancel_results):
                if isinstance(result, Exception):
                    self.logger.error(f"取消订单 {order.identifier} 失败: {result}")
                    results[order.identifier] = False
                else:
                    results[order.identifier] = result

        self.logger.info(f"批量取消订单完成，成功: {sum(results.values())}, 失败: {len(results) - sum(results.values())}")
        return results

