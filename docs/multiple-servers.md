# Multiple Servers

You can add one hub per rtl_433 server.

Each hub owns its own WebSocket connection and discovery toggle. Device identities
are scoped to the hub, so two servers that decode the same model and ID produce
distinct Home Assistant devices and entities.

## Hub and Nested Device Model

There is one config entry per rtl_433 server. The RF devices it decodes are
device-registry devices nested under that hub entry, not separate config entries.
This mirrors Home Assistant's `rfxtrx` integration shape.

Deleting a hub removes all of its nested devices and entities. Deleting a single
RF device from its device page removes only that device.

## Upgrade Notes

Upgrading from 0.1.0 is seamless and in place. On first start, the integration
re-homes existing devices and entities onto the hub entry while preserving entity
IDs and history.

Motion is now an occupancy `binary_sensor` with synthesized auto-off behavior
instead of an event entity. Entity IDs change from `event.*_motion` to
`binary_sensor.*_motion`. Update automations, dashboards, or scripts that used
the old event entity. On upgrade, the integration removes the orphaned old entity
and raises a one-time repair issue.
