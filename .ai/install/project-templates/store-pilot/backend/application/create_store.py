from backend.domain.store import Store


def create_store(store_id: str, name: str) -> Store:
    if not name.strip():
        raise ValueError("name is required")
    return Store(id=store_id, name=name.strip())
