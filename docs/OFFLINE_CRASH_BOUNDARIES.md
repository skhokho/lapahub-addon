# Offline process-crash boundaries — addon78f96b9

Four deterministic tests in `tests/test_hard_crash_boundaries.py` kill a separate interpreter using `os._exit(77)`. The child runs the actual AST-extracted command executor, durable SQLite journal and acknowledgement loop. Only temporary fsynced JSONL files represent an external effect and a cloud acceptance; no addon constructor, HA connection, Firebase call or physical service is used.

| Hard-exit boundary | Fake effects before crash | Durable outcome after restart | Redelivery behavior |
| --- | --- | --- | --- |
| Journal claim committed, before dispatch | 0 | unknown | Suppressed; no speculative new execution |
| Fake external effect persisted, before journal completion | 1 | unknown | Suppressed; uncertainty retained |
| Accepted journal completion committed, before cloud acknowledgement | 1 | service_accepted | Receipt outbox recovered; effect suppressed |
| Fake cloud accepted receipt, before local outbox acknowledgement | 1 | service_accepted | Receipt retried; effect suppressed |

Each case verifies SQLite integrity after an unclean process exit, preserved command fingerprint/dispatch trace, eight concurrent duplicate deliveries plus a later serial duplicate, actual outbox acknowledgement recovery, and a second journal restart. The final case intentionally produces two accepted receipt records for one fake effect. Receipt counts are not execution counts.

All four scenarios passed on the existing78f96b9 implementation; no production correctness bug or runtime code change was needed. Full local Python suite:69 tests passed. Run `python -m unittest discover -s tests` to reproduce.

These results strengthen exception-based tests by bypassing every normal Python cleanup path. They do not establish actual physical relay behavior, real cloud/network delivery, filesystem power-loss guarantees, retention-horizon behavior, or exactly-once effects across HA and SQLite. A process may crash after a durable claim but before execution; preserving unknown and refusing a blind replay deliberately trades automatic completion for avoiding an unintended repeated non-idempotent action. The live restart/SQLite/journal evidence remains separate, and actual interrupted-network/expiry-renewal validation remains outstanding.
