# kabu_micro_edge_c Comparison Notes

Date: 2026-05-18

This note records what was borrowed from `D:\kabu_micro_edge_c` and what was intentionally not ported into `kabu_maker_taker`.

## Decision

Keep the current Python architecture:

- `main.py` launches one Python worker per symbol.
- Each worker owns one `CombinedMakerTakerStrategy`, one REST executor, one WebSocket loop, one journal, and one per-symbol log directory.
- The launcher remains the supervisor: if a real worker exits non-zero, it stops the other workers.

Do not port the C++ single-process multi-symbol runtime in this round. That architecture has advantages for shared account risk and synchronized reconciliation, but moving Python to it would be a larger refactor with higher live-trading risk.

## C++ Capabilities Worth Borrowing

Evidence in `D:\kabu_micro_edge_c`:

- `include/kabu_micro_edge/diagnostics.hpp` has a compact JSONL runtime summary writer.
- `src/main.cpp` writes diagnostics on startup, periodic heartbeat, and shutdown.
- `include/kabu_micro_edge/app/runtime.hpp` has authorization retry for 401/403, recovery state, periodic reconciliation, and account-level risk snapshotting.
- `config.example.json` contains `diagnostics.summary_jsonl_path`, `diagnostics.heartbeat_interval_s`, and `account_risk`.

Python changes inspired by those pieces:

- Added `diagnostics.runtime_summary_jsonl_path` and `diagnostics.heartbeat_interval_s` to `AppConfig`.
- Added `RuntimeSummaryWriter` JSONL output with `websocket`, `position`, `active_orders`, `metrics`, `risk`, `auth`, and `consistency`.
- Added `token_refresh_count` to `KabuRestExecutor.auth_context()`.
- Added one-shot 401/403 auth retry for snapshot, register/unregister, submit, cancel, poll, positions, and open-order snapshots.
- Added launcher-level static `account_risk` validation for aggregate configured inventory and notional caps.
- Added strategy consistency checks that halt live trading on high-risk local state contradictions.

## Not Ported In This Round

- No single-process multi-symbol Python runtime.
- No realtime cross-worker account risk sharing.
- No periodic full reconciliation loop independent of the current board/poll cycle.
- No strategy alpha changes, C++ signal port, or maker/taker policy replacement.
- No change to broker JSON request shape except safe token refresh retry around existing requests.

## Python-Specific Safety Behavior

The Python version now treats these as high-risk consistency violations:

- Active exit order while local position is flat.
- Active exit side does not reduce the local position.
- Active exit quantity exceeds local position quantity.
- Order cumulative quantity exceeds intent quantity.
- Local position quantity exceeds `risk.max_inventory_qty`.

When any high-risk issue is detected in the live board path, the worker returns `consistency_violation`, emits `live_halted`, and allows the existing emergency cleanup logic to run.

## Operational Notes For Future AI Agents

- Runtime summary is disabled by default in generic config and enabled in live examples with `runtime_summary_jsonl_path="runtime_summary.jsonl"`.
- Relative summary paths resolve under each worker `log_dir`, so multi-symbol runs produce per-symbol summaries.
- Raw API tokens must never be logged. Only `token_sha256_8`, `token_source`, `shared_token`, and `token_refresh_count` are allowed in diagnostics.
- `account_risk` is only a launcher startup config check. It does not yet coordinate live inventory across already-running worker processes.
- A future single-process or shared-state design is needed before claiming realtime account-level risk enforcement.

