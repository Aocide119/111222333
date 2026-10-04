from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from time import sleep

from evog.app import Application


def test_concurrent_provider_initialization_owns_and_closes_one_client(tmp_path, monkeypatch):
    created = []

    class Client:
        closed = False

        def __init__(self, settings):
            sleep(0.01)
            created.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr("evog.app.ChatProvider", Client)
    app = Application(tmp_path)
    barrier = Barrier(4)

    def initialize(_):
        barrier.wait(timeout=5)
        return app.provider

    with ThreadPoolExecutor(max_workers=4) as pool:
        providers = list(pool.map(initialize, range(4)))
    app.close()
    assert len(created) == 1
    assert all(p is created[0] for p in providers)
    assert created[0].closed
