from backend.application.create_store import create_store


def handle_create_store(payload: dict, store_id: str) -> dict:
    store = create_store(store_id, str(payload.get("name") or ""))
    return {"id": store.id, "name": store.name, "status": store.status}
