from core.order_handling.perpetual_order import PerpetualOrderType, PerpetualOrderSide

class OrderExecutionFailedError(Exception):
    def __init__(
        self, 
        message: str, 
        order_side: PerpetualOrderSide,
        order_type: PerpetualOrderType,
        pair: str, 
        quantity: float,
        price: float
    ):
        super().__init__(message)
        self.order_side = order_side
        self.order_type = order_type
        self.pair = pair
        self.quantity = quantity
        self.price = price