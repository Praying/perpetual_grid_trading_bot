import ccxt
import ccxt.pro as ccxtpro
import os
from typing import Optional
from config.config_manager import ConfigManager
from core.services.exceptions import UnsupportedExchangeError, MissingEnvironmentVariableError

class CCXTExchangeFactory:
    @staticmethod
    def _get_env_variable(key: str) -> str:
        value = os.getenv(key)
        if value is None:
            raise MissingEnvironmentVariableError(f"Missing required environment variable: {key}")
        return value

    @staticmethod
    def create(config_manager: ConfigManager, is_paper_trading: bool = False) -> ccxt.Exchange:
        """
        创建并返回配置好的ccxt.Exchange实例

        Args:
            config_manager: 配置管理器实例
            is_paper_trading: 是否启用模拟交易模式

        Returns:
            ccxt.Exchange: 配置好的交易所实例

        Raises:
            UnsupportedExchangeError: 当指定的交易所不支持时
            MissingEnvironmentVariableError: 当缺少必要的环境变量时
        """
        exchange_name = config_manager.get_exchange_name()
        
        try:
            # 获取API密钥
            api_key = CCXTExchangeFactory._get_env_variable("EXCHANGE_API_KEY")
            secret_key = CCXTExchangeFactory._get_env_variable("EXCHANGE_SECRET_KEY")
            password = CCXTExchangeFactory._get_env_variable("PASSWORD")

            exchange = getattr(ccxtpro, exchange_name)({
                'apiKey': api_key,
                'secret': secret_key,
                'password': password,
                'enableRateLimit': True,
                'options': {
                    'defaultType': 'swap',  # 设置为永续合约模式
                }
            })
            # 打开模拟交易模式（确保使用OKX模拟盘接口）
            if is_paper_trading:
                exchange.set_sandbox_mode(True)

            return exchange

        except AttributeError:
            raise UnsupportedExchangeError(f"The exchange '{exchange_name}' is not supported.")