# Screenshots

These captures are produced by the containerized harness replaying a real Acurite
capture. See the integration harness runbook in the repository for details.

## Device Page

An auto-added **Acurite-Tower** device, nested under the hub, with temperature,
humidity, battery, and signal diagnostics.

![Acurite-Tower device page showing Temperature 26.7 C, Humidity 74.0%, Battery 100%, and signal diagnostics](images/02-device-page.png)

## Hub Options Flow

The hub options flow opens a menu for **Hub settings**, **Device settings**, and
**Device mappings**.

![Hub options flow menu showing Hub settings, Device settings, and Device mappings](images/03-options-flow.png)

## Device Mappings

The **Device mappings** step opens Home Assistant's YAML editor pre-filled with
the hub's current overrides.

![Device mappings step showing the YAML editor pre-filled with an example override](images/05-mapping-overrides.png)

## Unavailable State

After the stream stops and the availability timeout elapses, the device's
entities flip to unavailable.

![Device entities showing the unavailable state after the availability timeout](images/04-unavailable-state.png)
