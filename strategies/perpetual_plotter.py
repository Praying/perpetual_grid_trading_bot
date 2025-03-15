from plotly.subplots import make_subplots
from typing import List
import plotly.graph_objects as go
import pandas as pd
from core.grid_management.perpetual_grid_manager import PerpetualGridManager
from core.order_handling.perpetual_order import PerpetualOrder, PerpetualOrderSide
from core.order_handling.perpetual_order_book import PerpetualOrderBook


class PerpetualPlotter:
    def __init__(
        self, 
        grid_manager: PerpetualGridManager,
        order_book: PerpetualOrderBook
    ):
        self.grid_manager = grid_manager
        self.order_book = order_book

    def plot_results(
        self, 
        data: pd.DataFrame
    ) -> None:
        fig = make_subplots(rows=3, cols=1, shared_xaxes=True, row_heights=[0.70, 0.15, 0.15], vertical_spacing=0.02)
        self._add_candlestick_trace(fig, data)
        trigger_price = self.grid_manager.get_trigger_price()
        self._add_trigger_price_line(fig, trigger_price)
        self._add_grid_lines(fig, self.grid_manager.price_grids, self.grid_manager.central_price)
        self._add_trade_markers(fig, self.order_book.get_completed_orders())
        self._add_volume_trace(fig, data)
        self._add_account_value_trace(fig, data)

        fig.update_layout(
            title="Grid Trading Strategy Results",
            yaxis_title="Price (USDT)",
            yaxis2_title="Volume",
            yaxis3_title="Equity",
            xaxis=dict(rangeslider=dict(visible=False)),
            showlegend=False,
        )
        fig.show()

    def _add_candlestick_trace(
        self, 
        fig: go.Figure, 
        data: pd.DataFrame
    ) -> None:
        fig.add_trace(
            go.Candlestick(
                x=data.index,
                open=data['open'],
                high=data['high'],
                low=data['low'],
                close=data['close'],
                name=""
            ),
            row=1, col=1
        )
    
    def _add_trigger_price_line(
        self, 
        fig: go.Figure,
        trigger_price: float
    ):
        fig.add_trace(go.Scatter(
            x=[fig.data[0].x[0], fig.data[0].x[-1]],
            y=[trigger_price, trigger_price],
            mode='lines',
            line=dict(color="blue", width=2, dash="dash"),
            name="Central Price"
        ))
        fig.add_annotation(
            x=fig.data[0].x[-1],
            y=trigger_price,
            text=f"Trigger Price: {trigger_price:.2f}",
            showarrow=True,
            arrowhead=2,
            arrowsize=1,
            arrowcolor="blue",
            ax=20,
            ay=-20,
            font=dict(size=10, color="blue"),
        )

    def _add_grid_lines(
        self, 
        fig: go.Figure, 
        grids: List[float], 
        central_price: float
    ) -> None:
        for price in grids:
            color = 'green' if price < central_price else 'red'
            fig.add_trace(go.Scatter(
                x=[fig.data[0].x[0], fig.data[0].x[-1]],
                y=[price, price],
                mode='lines',
                line=dict(color=color, dash='dash'),
                showlegend=False
            ))

    def _add_trade_markers(
        self, 
        fig: go.Figure, 
        orders: List[PerpetualOrder]
    ) -> None:
        for order in orders:
            icon_name = 'triangle-up' if order.side == PerpetualOrderSide.BUY_OPEN else 'triangle-down'
            icon_color = 'green' if order.side == PerpetualOrderSide.BUY_OPEN else 'red'
            fig.add_trace(
                go.Scatter(
                    x=[order.format_last_trade_timestamp()],
                    y=[order.price],
                    mode='markers',
                    marker=dict(symbol=icon_name, color=icon_color, size=12, line=dict(color='black', width=2)),
                    name=f'{order.side.name} Order',
                    text=f"Price: {order.price}\nQty: {order.filled}\nDate: {order.format_last_trade_timestamp()}",
                    hoverinfo='x+y+text',
                ),
                row=1, col=1
            )

    def _add_volume_trace(
        self, 
        fig: go.Figure, 
        data: pd.DataFrame
    ) -> None:
        volume_colors = ['green' if close >= open_ else 'red' for close, open_ in zip(data['close'], data['open'])]
        
        fig.add_trace(
            go.Bar(
                x=data.index,
                y=data['volume'],
                marker=dict(color=volume_colors),
                name=""
            ),
            row=2, col=1
        )
        
        fig.update_yaxes(
            title="Volume",
            row=2,
            col=1
        )
    
    def _add_account_value_trace(
        self, 
        fig: go.Figure, 
        data: pd.DataFrame
    ) -> None:
        fig.add_trace(
            go.Scatter(
                x=data.index,
                y=data['account_value'],
                mode='lines',
                name="",
                line=dict(color='purple', width=2),
            ),
            row=3, col=1
        )