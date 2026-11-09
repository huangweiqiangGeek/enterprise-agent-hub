import asyncio
import sys
from collections.abc import Coroutine


def run_async[T](coroutine: Coroutine[object, object, T]) -> T:
    # Psycopg async connections require SelectorEventLoop on Windows/Python 3.12.
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        return runner.run(coroutine)
