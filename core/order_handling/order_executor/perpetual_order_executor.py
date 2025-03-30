from typing import Optional, Dict, Any, Union
import logging
import time
import ccxt
import asyncio
from decimal import Decimal

from core.order_handling.perpetual_order import PerpetualOrder, PerpetualOrderSide, PerpetualOrderType, PerpetualOrderStatus
from core.order_handling.exceptions import OrderExecutionFailedError, ExchangeAPIError
from core.services.perpetual_exchange_service import PerpetualExchangeService
from config.trading_mode import TradingMode


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
            return self._create_simulated_order(side, trading_pair, amount, price, PerpetualOrderType.LIMIT)
        
        # 实盘模式下执行真实订单
        retry_count = 0
        while retry_count < self.max_retries:
            try:
                # 转换为交易所API需要的参数
                ccxt_side = self._convert_to_ccxt_side(side)
                ccxt_params = self._get_order_params(side)
                
                # 执行限价单
                response = await self.exchange.create_order(
                    symbol=trading_pair,
                    type='limit',
                    side=ccxt_side,
                    amount=amount,
                    price=price,
                    params=ccxt_params
                )
                
                # 创建订单对象
                order = self._create_order_from_response(response, side, trading_pair, amount, price, PerpetualOrderType.LIMIT)
                self.logger.info(f"限价单创建成功: {order.identifier}")
                return order
                
            except ccxt.NetworkError as e:
                retry_count += 1
                self.logger.warning(f"网络错误，尝试重新执行订单 ({retry_count}/{self.max_retries}): {e}")
                await asyncio.sleep(self.retry_delay)
                
            except ccxt.ExchangeError as e:
                self.logger.error(f"交易所错误: {e}")
                raise ExchangeAPIError(f"交易所API错误: {e}")
                
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
            current_price: float = None
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
        
        # 如果未提供当前价格，尝试获取
        if current_price is None:
            current_price = await self.exchange_service.get_current_price(trading_pair)
            
        # 回测模式下模拟订单执行
        if self.trading_mode == TradingMode.BACKTEST:
            return self._create_simulated_order(side, trading_pair, amount, current_price, PerpetualOrderType.MARKET)
        
        # 实盘模式下执行真实订单
        retry_count = 0
        while retry_count < self.max_retries:
            try:
                # 转换为交易所API需要的参数
                ccxt_side = self._convert_to_ccxt_side(side)
                ccxt_params = self._get_order_params(side)
                
                # 执行市价单
                response = await self.exchange.create_order(
                    symbol=trading_pair,
                    type='market',
                    side=ccxt_side,
                    amount=amount,
                    params=ccxt_params
                )
                
                # 创建订单对象
                order = self._create_order_from_response(response, side, trading_pair, amount, current_price, PerpetualOrderType.MARKET)
                self.logger.info(f"市价单创建成功: {order.identifier}")
                return order
                
            except ccxt.NetworkError as e:
                retry_count += 1
                self.logger.warning(f"网络错误，尝试重新执行订单 ({retry_count}/{self.max_retries}): {e}")
                await asyncio.sleep(self.retry_delay)
                
            except ccxt.ExchangeError as e:
                self.logger.error(f"交易所错误: {e}")
                raise ExchangeAPIError(f"交易所API错误: {e}")
                
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
            order.status = PerpetualOrderStatus.CANCELLED
            return True
        
        # 实盘模式下执行真实订单取消
        retry_count = 0
        while retry_count < self.max_retries:
            try:
                # 执行订单取消
                await self.exchange.cancel_order(
                    id=order.identifier,
                    symbol=order.trading_pair
                )
                
                order.status = PerpetualOrderStatus.CANCELLED
                self.logger.info(f"订单取消成功: {order.identifier}")
                return True
                
            except ccxt.OrderNotFound:
                self.logger.warning(f"订单不存在或已经被取消: {order.identifier}")
                order.status = PerpetualOrderStatus.CANCELLED
                return True
                
            except ccxt.NetworkError as e:
                retry_count += 1
                self.logger.warning(f"网络错误，尝试重新取消订单 ({retry_count}/{self.max_retries}): {e}")
                await asyncio.sleep(self.retry_delay)
                
            except ccxt.ExchangeError as e:
                self.logger.error(f"取消订单时发生交易所错误: {e}")
                raise ExchangeAPIError(f"取消订单时发生交易所API错误: {e}")
                
            except Exception as e:
                self.logger.error(f"取消订单时发生未知错误: {e}", exc_info=True)
                raise OrderExecutionFailedError(f"取消订单失败: {e}")
        
        self.logger.error(f"达到最大重试次数，订单取消失败: {order.identifier}")
        return False

    def _convert_to_ccxt_side(self, side: PerpetualOrderSide) -> str:
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

    def _get_order_params(self, side: PerpetualOrderSide) -> Dict[str, Any]:
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

    def _create_order_from_response(
            self,
            response: Dict[str, Any],
            side: PerpetualOrderSide,
            trading_pair: str,
            amount: float,
            price: float,
            order_type: PerpetualOrderType
    ) -> PerpetualOrder:
        """
        从交易所响应创建订单对象
        
        参数:
            response: 交易所API响应
            side: 订单方向
            trading_pair: 交易对
            amount: 交易数量
            price: 订单价格
            order_type: 订单类型
            
        返回:
            创建的订单对象
        """
        order_id = response.get('id')
        timestamp = response.get('timestamp', int(time.time() * 1000))
        
        return PerpetualOrder(
            identifier=order_id,
            trading_pair=trading_pair,
            side=side,
            amount=amount,
            price=price,
            order_type=order_type,
            status=PerpetualOrderStatus.PENDING,
            timestamp=timestamp,
            exchange_order_id=order_id
        )

    def _create_simulated_order(
            self,
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
            trading_pair=trading_pair,
            side=side,
            amount=amount,
            price=price,
            order_type=order_type,
            status=PerpetualOrderStatus.PENDING if order_type == PerpetualOrderType.LIMIT else PerpetualOrderStatus.FILLED,
            timestamp=timestamp,
            exchange_order_id=order_id
        )