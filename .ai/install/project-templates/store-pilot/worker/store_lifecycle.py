def consume_store_lifecycle_changed(event: dict) -> str:
    required = {"event_id", "store_id", "status"}
    if not required.issubset(event):
        raise ValueError("invalid StoreLifecycleChanged event")
    if event["status"] != "active":
        raise ValueError("unsupported store lifecycle status")
    return str(event["store_id"])
