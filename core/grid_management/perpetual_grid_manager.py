import logging
from typing import List, Optional, Tuple
import numpy as np
from config.config_manager import ConfigManager
from strategies.strategy_type import StrategyType
from strategies.spacing_type import SpacingType
from core.grid_management.grid_level import GridLevel, GridCycleState
from core.order_handling.perpetual_order import PerpetualOrder, PerpetualOrderSide


class PerpetualGridManager:
    def __init__(
        self, 
        config_manager: ConfigManager, 
        strategy_type: StrategyType,
        leverage: float = 1.0,  # 杠杆倍数
        margin_type: str = "cross",  # 保证金模式：isolated(逐仓) 或 cross(全仓)
        max_placed_orders: int = 5   # 最大放置的订单数量，默认值为5，即同时挂5个买单和5个卖单，减少保证金，提高资金利用率
    ):
        self.config_manager = config_manager
        self.strategy_type = strategy_type
        self.leverage = leverage
        self.margin_type = margin_type
        self.long_positions: dict[float, float] = {}  # 多仓位管理：价格 -> 数量
        self.short_positions: dict[float, float] = {}  # 空仓位管理：价格 -> 数量
        self.funding_rates: List[float] = []  # 资金费率历史
        self.margin_ratio: float = 0.01  # 维持保证金率
        self.logger = logging.getLogger(self.__class__.__name__)
        self.price_grids: List[float] = []
        self.central_price: float = 0.0
        self.reversion_price: float = 0.0
        self.sorted_buy_grids: List[float] = []
        self.sorted_sell_grids: List[float] = []
        self.grid_levels: dict[float, GridLevel] = {}
        self.max_placed_orders: int = max_placed_orders
        self.initialize_grids_and_levels()


    def pair_grid_levels(
            self,
            source_grid_level: GridLevel,
            target_grid_level: GridLevel,
            pairing_type: str
    ) -> None:
        """
        动态配对网格级别以进行买入或卖出。

        参数:
            source_grid_level: 发起配对的网格级别。
            target_grid_level: 被配对的网格级别。
            pairing_type: "buy" 或 "sell"，指定配对类型。
            """
        if pairing_type == "buy":
            # 将源网格级别的买入配对设置为目标网格级别
            source_grid_level.paired_buy_level = target_grid_level
            # 将目标网格级别的卖出配对设置为源网格级别
            target_grid_level.paired_sell_level = source_grid_level
            self.logger.info(
                f"Paired sell grid level {source_grid_level.price} with buy grid level {target_grid_level.price}.")

        elif pairing_type == "sell":
            # 将源网格级别的卖出配对设置为目标网格级别
            source_grid_level.paired_sell_level = target_grid_level
            # 将目标网格级别的买入配对设置为源网格级别
            target_grid_level.paired_buy_level = source_grid_level
            self.logger.info(
                f"Paired buy grid level {source_grid_level.price} with sell grid level {target_grid_level.price}.")

        else:
            raise ValueError(f"Invalid pairing type: {pairing_type}. Must be 'buy' or 'sell'.")

    def get_paired_sell_level(
            self,
            buy_grid_level: GridLevel
    ) -> Optional[GridLevel]:
        """
        根据策略类型为给定的买入网格级别确定配对的卖出网格级别。

        参数:
            buy_grid_level: 需要配对卖出级别的买入网格级别。

        返回:
            配对的卖出网格级别，如果不存在则返回 None。
        """
        if self.strategy_type == StrategyType.SIMPLE_GRID:
            self.logger.info(f"Looking for paired sell level for buy level at {buy_grid_level}")
            self.logger.info(f"Available sell grids: {self.sorted_sell_grids}")

            for sell_price in self.sorted_sell_grids:
                sell_level = self.grid_levels[sell_price]
                self.logger.info(f"Checking sell level {sell_price}, state: {sell_level.state}")

                if sell_level and not self.can_place_order(sell_level, PerpetualOrderSide.BUY_CLOSE):
                    self.logger.info(
                        f"Skipping sell level {sell_price} - cannot place order. State: {sell_level.state}")
                    continue

                if sell_price > buy_grid_level.price:
                    self.logger.info(f"Paired sell level found at {sell_price} for buy level {buy_grid_level}.")
                    return sell_level

            self.logger.warning(f"No suitable sell level found above {buy_grid_level}")
            return None

        elif self.strategy_type == StrategyType.HEDGED_GRID:
            self.logger.info(f"Available price grids: {self.price_grids}")
            sorted_prices = sorted(self.price_grids)
            current_index = sorted_prices.index(buy_grid_level.price)
            self.logger.info(f"Current index of buy level {buy_grid_level.price}: {current_index}")

            if current_index + 1 < len(sorted_prices):
                paired_sell_price = sorted_prices[current_index + 1]
                sell_level = self.grid_levels[paired_sell_price]
                self.logger.info(
                    f"Paired sell level for buy level {buy_grid_level.price} is at {paired_sell_price} (state: {sell_level.state})")
                return sell_level

            self.logger.warning(f"No suitable sell level found for buy grid level {buy_grid_level}")
            return None

        else:
            self.logger.error(f"Unsupported strategy type: {self.strategy_type}")
            return None

    def get_grid_level_below(self, grid_level: GridLevel) -> Optional[GridLevel]:
        """
        返回给定网格级别正下方的网格级别。

        参数:
            grid_level: 当前网格级别。

        返回:
            下方的网格级别，如果不存在则返回 None。
        """
        # 对网格价格进行排序
        sorted_levels = sorted(self.grid_levels.keys())
        # 获取当前网格级别的索引
        current_index = sorted_levels.index(grid_level.price)

        if current_index > 0:
            # 返回下方网格级别的价格
            lower_price = sorted_levels[current_index - 1]
            return self.grid_levels[lower_price]
        return None

    def get_grid_level_below_bound(self, grid_level: GridLevel) -> Optional[GridLevel]:
        """
        返回给定网格级别正下方距离为max_placed_orders的网格级别。

        参数:
            grid_level: 当前网格级别。

        返回:
            下方的网格级别，如果不存在则返回 None。
        """
        # 对网格价格进行排序
        sorted_levels = sorted(self.grid_levels.keys())
        # 获取当前网格级别的索引
        current_index = sorted_levels.index(grid_level.price)

        if current_index > 0 and current_index >= self.max_placed_orders:
            # 返回下方网格距离为max_placed_orders级别的价格
            lower_price = sorted_levels[current_index - self.max_placed_orders]
            return self.grid_levels[lower_price]
        return None

    def get_grid_level_up_bound(self, grid_level: GridLevel) -> Optional[GridLevel]:
        """
        返回给定网格级别上方距离为max_placed_orders的网格级别。

        参数:
            grid_level: 当前网格级别。

        返回:
            下方的网格级别，如果不存在则返回 None。
        """
        # 对网格价格进行排序
        sorted_levels = sorted(self.grid_levels.keys())
        # 获取当前网格级别的索引
        current_index = sorted_levels.index(grid_level.price)

        if current_index < len(sorted_levels)  and current_index + self.max_placed_orders < len(sorted_levels):
            # 返回下方网格距离为max_placed_orders级别的价格
            upper_price = sorted_levels[current_index + self.max_placed_orders]
            return self.grid_levels[upper_price]
        return None

    def get_order_size_for_grid_level(
        self,
        total_margin: float,  # 总可用保证金
        current_price: float,
        position_side: str = "long"  # 仓位方向：long或short
    ) -> float:
        """
        根据可用保证金、当前价格和杠杆计算合约数量。

        参数:
            total_margin: 可用保证金金额
            current_price: 当前价格
            position_side: 仓位方向

        返回:
            计算出的合约数量
        """
        # 计算该网格可分配的保证金
        margin_per_grid = total_margin / len(self.grid_levels)
        
        # 计算最大可开仓数量（考虑杠杆）
        max_position_size = (margin_per_grid * self.leverage) / current_price
        
        # 考虑维持保证金要求
        safe_position_size = max_position_size * (1 - self.margin_ratio)
        
        return safe_position_size

    def get_initial_order_quantity(
        self,
        current_price: float,
    ) -> float:
        """
        计算网格初始化时要开仓的合约数量。

        参数:
            available_margin: 可用保证金
            current_positions: 当前持仓数量
            current_price: 当前价格
            position_side: 仓位方向

        返回:
            要开仓的合约数量
        """
        # 计算当前价格之上的所有网格数量，乘以对应的网格价值
        count = sum(price > current_price for price in self.price_grids)
        return count * self.config_manager.get_grid_value()


    def update_positions(
        self,
        price: float,
        quantity: float,
        position_side: str
    ) -> None:
        """
        更新仓位信息

        参数:
            price: 开仓/平仓价格
            quantity: 合约数量
            position_side: 仓位方向（'long'或'short'）
        """
        if position_side == "long":
            if price in self.long_positions:
                self.long_positions[price] += quantity
            else:
                self.long_positions[price] = quantity
        else:
            if price in self.short_positions:
                self.short_positions[price] += quantity
            else:
                self.short_positions[price] = quantity

    def calculate_funding_fee(
        self,
        position_value: float,
        funding_rate: float
    ) -> float:
        """
        计算资金费用

        参数:
            position_value: 仓位价值
            funding_rate: 资金费率

        返回:
            资金费用金额
        """
        return position_value * funding_rate

    def check_margin_safety(
        self,
        total_margin: float,
        total_position_value: float
    ) -> bool:
        """
        检查保证金安全性

        参数:
            total_margin: 总保证金
            total_position_value: 总仓位价值

        返回:
            是否安全（True/False）
        """
        # 计算当前保证金率
        current_margin_ratio = total_margin / total_position_value
        
        # 检查是否低于维持保证金率
        return current_margin_ratio >= self.margin_ratio

    def adjust_grid_spacing(
        self,
        base_spacing: float
    ) -> float:
        """
        根据杠杆调整网格间距

        参数:
            base_spacing: 基础网格间距

        返回:
            调整后的网格间距
        """
        # 随着杠杆增加，适当增加网格间距以控制风险
        return base_spacing * (1 + (self.leverage - 1) * 0.1)

    def initialize_grids_and_levels(self) -> None:
        """
        初始化网格级别并根据所选策略分配其各自的状态。

        对于 `SIMPLE_GRID` 策略：
        - 在低于中心价格的网格级别上放置买入订单。
        - 在高于中心价格的网格级别上放置卖出订单。
        - 级别初始化为 `READY_TO_BUY` 或 `READY_TO_SELL` 状态。

        对于 `HEDGED_GRID` 策略：
        - 网格级别分为买入级别（除顶部网格外）和卖出级别（除底部网格外）。
        - 买入网格级别初始化为 `READY_TO_BUY`，顶部网格除外。
        - 卖出网格级别初始化为 `READY_TO_SELL`。
        """
        self.reversion_price = self.config_manager.get_reversion_price()
        if self.reversion_price is None:
            self.logger.info("Reversion price must be set")
            return None

        self.logger.info(f"Reversion price be set to {self.reversion_price}")
        # 计算网格价格和中心价格
        self.price_grids, self.reversion_price = self._calculate_price_grids_and_central_price()
        self.central_price = self.reversion_price

        if self.strategy_type == StrategyType.SIMPLE_GRID:
            # 筛选出低于中心价格的买入网格
            self.sorted_buy_grids = [price_grid for price_grid in self.price_grids if price_grid <= self.central_price]
            # 筛选出高于中心价格的卖出网格
            self.sorted_sell_grids = [price_grid for price_grid in self.price_grids if price_grid > self.central_price]
            # 初始化网格级别状态，低于中心价格为 READY_TO_BUY，高于中心价格为 READY_TO_SELL
            self.grid_levels = {price: GridLevel(price, GridCycleState.READY_TO_BUY if price <= self.central_price else GridCycleState.READY_TO_SELL) for price in self.price_grids}
        
        elif self.strategy_type == StrategyType.HEDGED_GRID:
            # 买入网格为除顶部网格外的所有网格
            self.sorted_buy_grids  = self.price_grids[:-1]  # 除顶部网格外
            # 卖出网格为除底部网格外的所有网格
            self.sorted_sell_grids = self.price_grids[1:]  # All except the bottom grid
            # 初始化网格级别状态，非顶部网格为 READY_TO_BUY_OR_SELL，顶部网格为 READY_TO_SELL
            self.grid_levels = {
                price: GridLevel(
                    price,
                    GridCycleState.READY_TO_BUY_OR_SELL if price != self.price_grids[-1] else GridCycleState.READY_TO_SELL
                )
                for price in self.price_grids
            }
        # 记录初始化信息
        self.logger.info(f"Grids and levels initialized. Central price: {self.central_price}")
        self.logger.info(f"Price grids: {self.price_grids}")
        self.logger.info(f"Buy grids: {self.sorted_buy_grids}")
        self.logger.info(f"Sell grids: {self.sorted_sell_grids}")
        self.logger.info(f"Grid levels: {self.grid_levels}")
        self.logger.info(f"Reversion price: {self.reversion_price}")

    def _extract_grid_config(self) -> Tuple[float, float, int, SpacingType]:
        """
        从配置管理器中提取网格配置参数。
        """
        # 获取底部范围
        bottom_range = self.config_manager.get_bottom_range()
        # 获取顶部范围
        top_range = self.config_manager.get_top_range()
        # 获取网格数量
        num_grids = self.config_manager.get_num_grids()
        # 获取间距类型（例如 ARITHMETIC 或 GEOMETRIC）
        spacing_type = self.config_manager.get_spacing_type()
        return bottom_range, top_range, num_grids, spacing_type

    def _extract_grid_new_config(self) -> Tuple[float, float, float, int, SpacingType]:
        """
        从配置管理器中提取新的网格配置参数。
        """
        # 获取回归价格
        reversion_price = self.config_manager.get_reversion_price()
        # 获取网格间距比例
        grid_ratio = self.config_manager.get_grid_ratio()
        # 获取网格价值
        grid_value = self.config_manager.get_grid_value()
        # 获取网格数量
        num_grids = self.config_manager.get_num_grids()
        # 获取间距类型（例如 ARITHMETIC 或 GEOMETRIC）
        spacing_type = self.config_manager.get_spacing_type()

        return reversion_price, grid_ratio, grid_value, num_grids, spacing_type

    def _calculate_price_grids_and_central_price(self) -> Tuple[List[float], float]:
        """
        根据配置计算价格网格和中心价格，考虑合约特性。
        """
        #bottom_range, top_range, num_grids, spacing_type = self._extract_grid_config()
        reversion_price, grid_ratio, grid_value, num_grids, spacing_type = self._extract_grid_new_config()
        top_range = reversion_price
        bottom_range = reversion_price * ((1 - grid_ratio) ** num_grids)
        if spacing_type == SpacingType.ARITHMETIC:
            # 调整网格间距
            grid_spacing = (top_range - bottom_range) / (num_grids - 1)
            adjusted_spacing = self.adjust_grid_spacing(grid_spacing)
            
            # 重新计算网格价格
            grids = [bottom_range + i * adjusted_spacing for i in range(num_grids)]
            central_price = (top_range + bottom_range) / 2

        elif spacing_type == SpacingType.GEOMETRIC:
            grids = []
            current_price = bottom_range
            for _ in range(num_grids):
                grids.append(current_price)
                current_price = current_price * (1 + grid_ratio)

        else:
            raise ValueError(f"不支持的间距类型: {spacing_type}")

        return grids, reversion_price

    def complete_order(
        self,
        grid_level: GridLevel,
        order_side: PerpetualOrderSide,
    ) -> None:
        """
        重写父类方法，处理合约订单完成后的状态转换

        参数:
            grid_level: 订单完成的网格级别
            order_side: 订单方向（买入/卖出）
            position_side: 仓位方向（多/空）
        """
        if self.strategy_type == StrategyType.SIMPLE_GRID:
            if order_side == PerpetualOrderSide.BUY_OPEN:  # 开多或平空
                grid_level.state = GridCycleState.READY_TO_SELL
                self.logger.info(f"开多仓完成，网格级别 {grid_level.price} 转换为 READY_TO_SELL")
            else:
                grid_level.state = GridCycleState.READY_TO_BUY
                self.logger.info(f"平多仓完成，网格级别 {grid_level.price} 转换为 READY_TO_BUY")


        elif self.strategy_type == StrategyType.HEDGED_GRID:
            if order_side == PerpetualOrderSide.BUY_OPEN:
                grid_level.state = GridCycleState.READY_TO_SELL
                self.logger.info(f"合约订单完成，网格级别 {grid_level.price} 转换为 READY_TO_SELL")

                if grid_level.paired_sell_level:
                    grid_level.paired_sell_level.state = GridCycleState.READY_TO_SELL
                    self.logger.info(f"配对的卖出网格级别 {grid_level.paired_sell_level.price} 转换为 READY_TO_SELL")

            elif order_side == PerpetualOrderSide.BUY_CLOSE:
                grid_level.state = GridCycleState.READY_TO_BUY
                self.logger.info(f"合约订单完成，网格级别 {grid_level.price} 转换为 READY_TO_BUY")

                if grid_level.paired_buy_level:
                    grid_level.paired_buy_level.state = GridCycleState.READY_TO_BUY
                    self.logger.info(f"配对的买入网格级别 {grid_level.paired_buy_level.price} 转换为 READY_TO_BUY")

        else:
            self.logger.error("未知的策略类型")

    def get_trigger_price(self) -> float:
        return self.central_price

    def get_reversion_price(self) -> float:
        return self.reversion_price

    def can_place_order(
        self,
        grid_level: GridLevel,
        order_side: PerpetualOrderSide,
    ) -> bool:
        """
        确定是否可以在给定的网格级别上放置订单。

        参数:
            grid_level: 被评估的网格级别。
            order_side: 订单的类型（买入或卖出）。

        返回:
            bool: 如果可以放置订单则为 True，否则为 False。
        """
        if self.strategy_type == StrategyType.SIMPLE_GRID:
            # 对于 SIMPLE_GRID 策略，买入订单要求状态为 READY_TO_BUY
            if order_side == PerpetualOrderSide.BUY_OPEN:
                return grid_level.state == GridCycleState.READY_TO_BUY
            elif order_side == PerpetualOrderSide.BUY_CLOSE:
                # 对于 SIMPLE_GRID 策略，卖出订单要求状态为 READY_TO_SELL
                return grid_level.state == GridCycleState.READY_TO_SELL
        elif self.strategy_type == StrategyType.HEDGED_GRID:
            if order_side == PerpetualOrderSide.BUY_OPEN:
                # 对于 HEDGED_GRID 策略，买入订单要求状态为 READY_TO_BUY 或 READY_TO_BUY_OR_SELL
                return grid_level.state in {GridCycleState.READY_TO_BUY, GridCycleState.READY_TO_BUY_OR_SELL}
            elif order_side == PerpetualOrderSide.BUY_CLOSE:
                # 对于 HEDGED_GRID 策略，卖出订单要求状态为 READY_TO_SELL 或 READY_TO_BUY_OR_SELL
                return grid_level.state in {GridCycleState.READY_TO_SELL, GridCycleState.READY_TO_BUY_OR_SELL}
        else:
            return False

    def mark_order_pending(
        self,
        grid_level: GridLevel,
        order: PerpetualOrder
    ) -> None:
        """
        将网格级别标记为有待定订单（买入或卖出）。

        参数:
            grid_level: 要更新的网格级别。
            order: 表示待定订单的 Order 对象。
            order_side: 订单的类型（买入或卖出）。
        """
        # 将订单添加到网格级别
        grid_level.add_order(order)
        # 更新状态为等待买入订单成交
        if order.side == PerpetualOrderSide.BUY_OPEN:
            # 更新状态为等待买入订单成交
            grid_level.state = GridCycleState.WAITING_FOR_BUY_FILL
            self.logger.info(f"Buy order placed and marked as pending at grid level {grid_level.price}.")
        elif order.side == PerpetualOrderSide.SELL_OPEN:
            # 更新状态为等待卖出订单成交
            grid_level.state = GridCycleState.WAITING_FOR_SELL_FILL
            self.logger.info(f"Sell order placed and marked as pending at grid level {grid_level.price}.")