from dataclasses import dataclass


@dataclass(frozen=True)
class Store:
    id: str
    name: str
    status: str = "active"
