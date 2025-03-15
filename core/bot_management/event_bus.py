import logging, asyncio, inspect
from typing import Callable, Dict, List, Any, Awaitable, Union

class Events:
    """
    Defines event types for the EventBus.
    """
    ORDER_FILLED = "order_filled"
    ORDER_CANCELLED = "order_cancelled"
    START_BOT = "start_bot"
    STOP_BOT = "stop_bot"
    FUNDING_FEE_CHARGED = "funding_fee_changed"
    FUNDING_FEE_SETTLED = "funding_fee_settled"
    POSITION_UPDATED = "position_updated"
    MARGIN_CALL = "margin_call"

class EventBus:
    """
    A simple event bus for managing pub-sub interactions with support for both sync and async publishing.
    """

    def __init__(self):
        """
        Initializes the EventBus with an empty subscriber list.
        """
        self.logger = logging.getLogger(self.__class__.__name__)
        self.subscribers: Dict[str, List[Callable[[Any], None]]] = {}
        self._tasks: set[asyncio.Task] = set()

    def subscribe(
        self, 
        event_type: str, 
        callback: Union[Callable[[Any], None], Callable[[Any], Awaitable[None]]]
    ) -> None:
        """
        Subscribes a callback to a specific event type.

        Args:
            event_type: The type of event to subscribe to.
            callback: The callback function to invoke when the event is published.
        """
        if event_type not in self.subscribers:
            self.subscribers[event_type] = []

        self.subscribers[event_type].append(callback)
        callback_name = getattr(callback, "__name__", str(callback))
        caller_frame = inspect.stack()[1]
        caller_name = f"{caller_frame.function} (from {caller_frame.filename}:{caller_frame.lineno})"
        self.logger.info(f"Callback '{callback_name}' subscribed to event: {event_type} by {caller_name}")

    async def publish(
        self, 
        event_type: str, 
        data: Any = None
    ) -> None:
        """
        Publishes an event asynchronously to all subscribers.
        """
        if event_type not in self.subscribers:
            self.logger.warning(f"No subscribers for event: {event_type}")
            return

        self.logger.info(f"Publishing async event: {event_type} with data: {data}")
        tasks = [
            self._safe_invoke_async(callback, data) if asyncio.iscoroutinefunction(callback) 
            else asyncio.to_thread(self._safe_invoke_sync, callback, data)
            for callback in self.subscribers[event_type]
        ]
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    self.logger.error(f"Exception in async event callback: {result}", exc_info=True)

    def publish_sync(
        self, 
        event_type: str, 
        data: Any
    ) -> None:
        """
        Publishes an event synchronously to all subscribers.
        """
        if event_type in self.subscribers:
            self.logger.info(f"Publishing sync event: {event_type} with data: {data}")
            loop = asyncio.get_event_loop()
            for callback in self.subscribers[event_type]:
                if asyncio.iscoroutinefunction(callback):
                    asyncio.run_coroutine_threadsafe(self._safe_invoke_async(callback, data), loop)
                else:
                    self._safe_invoke_sync(callback, data)

    async def _safe_invoke_async(
        self, 
        callback: Callable[[Any], None], 
        data: Any
    ) -> None:
        """
        Safely invokes an async callback, suppressing and logging any exceptions.
        """
        task = asyncio.create_task(self._invoke_callback(callback, data))
        self._tasks.add(task)

        def remove_task(completed_task: asyncio.Task):
            if not completed_task.cancelled():
                self._tasks.discard(completed_task)

        task.add_done_callback(remove_task)
        self.logger.debug(f"Task created for callback '{callback.__name__}' with data: {data}")
    
    async def _invoke_callback(
        self, 
        callback: Callable[[Any], None], 
        data: Any
    ) -> None:
        try:
            self.logger.info(f"Executing async callback '{callback.__name__}' for event with data: {data}")
            await callback(data)
        except Exception as e:
            self.logger.error(f"Error in async callback '{callback.__name__}': {e}", exc_info=True)

    def _safe_invoke_sync(
        self, 
        callback: Callable[[Any], None], 
        data: Any
    ) -> None:
        """
        Safely invokes a sync callback, suppressing and logging any exceptions.
        """
        try:
            callback(data)
        except Exception as e:
            self.logger.error(f"Error in sync subscriber callback: {e}", exc_info=True)
    
    async def shutdown(self):
        """
        Cancels all active tasks tracked by the EventBus for graceful shutdown.
        """
        self.logger.info("Shutting down EventBus...")
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self.logger.info("EventBus shutdown complete.")