# Example

Insert `OrderPlaced` with the order. Relay by `order_id`; consumers record `event_id` before applying side effects so duplicates are no-ops.
