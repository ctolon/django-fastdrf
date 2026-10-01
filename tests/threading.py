"""Start concurrent copy operations together to check request isolation."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier


def race(operation):
    barrier = Barrier(16)

    def run(_):
        barrier.wait(timeout=10)
        return operation()

    with ThreadPoolExecutor(max_workers=16) as executor:
        return list(executor.map(run, range(16)))
