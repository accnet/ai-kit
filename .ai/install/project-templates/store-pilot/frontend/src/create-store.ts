import type { CreateStoreRequest, Store } from "../../contracts/generated/sdk/contracts";

export async function createStore(request: CreateStoreRequest): Promise<Store> {
  const response = await fetch("/stores", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(request),
  });
  if (!response.ok) throw new Error(`create store failed: ${response.status}`);
  return response.json() as Promise<Store>;
}
