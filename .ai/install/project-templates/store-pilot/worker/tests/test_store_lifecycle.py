import unittest

from worker.store_lifecycle import consume_store_lifecycle_changed


class StoreLifecycleTests(unittest.TestCase):
    def test_consumes_active_store_event(self) -> None:
        self.assertEqual(
            consume_store_lifecycle_changed({"event_id": "event-1", "store_id": "store-1", "status": "active"}),
            "store-1",
        )


if __name__ == "__main__":
    unittest.main()
