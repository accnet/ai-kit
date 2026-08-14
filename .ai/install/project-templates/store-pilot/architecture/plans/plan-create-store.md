# Plan: Create Store pilot

## Goal

Create a Store through the versioned HTTP contract and publish a lifecycle
event for asynchronous consumers.

## Boundaries

- `frontend`: sends a create request using generated SDK types and test mocks.
- `backend`: owns the Store aggregate and implements `store-api@1.0.0`.
- `worker`: consumes `store-lifecycle@1.0.0`; it does not import backend code.

## Contract sequence

1. Inspect or update `contracts/openapi/v1/store-api.yaml`.
2. Run `ai-kit contract import ... --id store-api --version 1.0.0` and generate
   SDK/mocks under `contracts/generated/sdk`.
3. Propose/review/approve the API contract before implementation work.
4. Verify API and event producer/consumer conformance in integration QA.

## Delivery checks

- Backend tests cover the aggregate and application service.
- Frontend uses generated types; it does not duplicate the contract DTO.
- Worker validates the event shape at its boundary.
- An integration task verifies producer and consumer behavior before contract
  activation and delivery attestation.
