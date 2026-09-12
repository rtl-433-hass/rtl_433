---
id: 5
group: "union-devices-across-receivers"
dependencies: [3]
status: "pending"
created: 2026-09-12
skills:
  - python
  - home-assistant-integration
---
# Location-scoped adoption and the union add-device candidate list

## Objective
Let a user approve a physical sensor once for the whole location, and present one candidate per sensor rather than one per receiver.

## Skills Required
python, home-assistant-integration

## Acceptance Criteria
- [ ] `adopted` / `ignored` / `pending` move from each coordinator to LOCATION scope; `adoption.py` is re-pointed at the location and stays the single implementation behind both the options flow and the panel
- [ ] Adopting or ignoring a `device_key` applies to every receiver in the location
- [ ] The aggregator merges each receiver's pending map into a single location-wide candidate list keyed by `device_key` and re-emits one location-scoped pending signal
- [ ] A candidate heard by several receivers is ONE row showing LAST-RECEIVED-WINS data (no debounce), and records which receivers have heard it
- [ ] The existing backlog/replay gate is preserved per receiver and applied BEFORE the merge; the pending-map cap is enforced on the MERGED list so several receivers cannot multiply the bound
- [ ] On deliberate consolidation, `adopted` and `ignored` union across merged receivers and `ignored` wins a conflict
- [ ] `uv run pytest tests/` passes and lint/format are clean

Use your internal Todo tool to track these and keep on track.

## Technical Requirements
The approval workflow lives in `adoption.py` + `coordinator/base.py:266-283`; `SIGNAL_PENDING_UPDATE` (`const.py:339`) is hub-scoped today. `adopted` mirrors `entry.data[CONF_DEVICES]` and `ignored` mirrors `entry.data[CONF_IGNORED_DEVICES]`.

## Input Dependencies
Task 003's aggregator and location-scoped identity.

## Output Artifacts
Location-scoped adoption state and a single merged candidate list.

## Implementation Notes
The last-received-wins display rule is deliberately simpler than task 003's dedup: a discovery preview needs the freshest sample, a recorded entity value needs the anti-regression guard. See plan Component 5 and Clarification #12.
