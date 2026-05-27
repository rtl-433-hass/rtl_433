# Changelog

## [0.3.0](https://github.com/rtl-433-hass/rtl_433/compare/v0.2.0...v0.3.0) (2026-05-27)


### Features

* **rtl_433:** add HTTP getters and hub connectivity sensor ([e8561fc](https://github.com/rtl-433-hass/rtl_433/commit/e8561fc1b84b9cbc7e9048625e28bc368d5d52ba))
* **rtl_433:** add hub meta/SDR and server-stats diagnostic sensors ([1dbb835](https://github.com/rtl-433-hass/rtl_433/commit/1dbb835f77bb2ae66baa8fc1eb61317bea651fd0))


### Bug Fixes

* **rtl_433:** classify WS frames and clean up phantom unknown device ([c518855](https://github.com/rtl-433-hass/rtl_433/commit/c5188554e439701df55c19b98f8c3da1f7795cee))


### Documentation

* **rtl_433:** document hub observability and frame-routing contracts ([9bbbfe7](https://github.com/rtl-433-hass/rtl_433/commit/9bbbfe76c76f39519eb45d38f86b12ad1c89480d))
* **rtl_433:** require conventional-commit-style PR titles ([53c24b5](https://github.com/rtl-433-hass/rtl_433/commit/53c24b56b80e5dc2307efeeb79c4150bf92348e1))
* **tasks:** generate task blueprint for plan 03 (hub observability + frame routing) ([555171d](https://github.com/rtl-433-hass/rtl_433/commit/555171d550ac822fd63e0052b6361c4f0e96ff9f))

## [0.2.0](https://github.com/rtl-433-hass/rtl_433/compare/v0.1.0...v0.2.0) (2026-05-26)


### Features

* **rtl_433:** add devices-map contract and coordinator device eviction ([99f6dbc](https://github.com/rtl-433-hass/rtl_433/commit/99f6dbc1bb1985f2c775e66455c4fc46f92e08c8))
* **rtl_433:** migrate 0.1.0 per-device entries to nested devices ([88b5206](https://github.com/rtl-433-hass/rtl_433/commit/88b520696169552f2e88e535e38f971fd06e5978))


### Documentation

* **rtl_433:** recapture screenshots for the nested-devices model ([e2de325](https://github.com/rtl-433-hass/rtl_433/commit/e2de325ba01cb71c76801ca6a6f5dd77365724e6))
* **tasks:** record plan 02 execution summary and complete the blueprint ([ba8cde8](https://github.com/rtl-433-hass/rtl_433/commit/ba8cde8738efb765550913a39c23a62a0cf7c43c))

## [0.1.0](https://github.com/rtl-433-hass/rtl_433/compare/v0.0.1...v0.1.0) (2026-05-26)


### Features

* add config, options and discovery flows ([6af5e9d](https://github.com/rtl-433-hass/rtl_433/commit/6af5e9d60fc4155525ef156502f0aeb2aa57d397))
* add data-driven device mapping library ([70d1148](https://github.com/rtl-433-hass/rtl_433/commit/70d114834205325e3a1f938c741f3beb15459e72))
* add device-library YAML loader ([edd9159](https://github.com/rtl-433-hass/rtl_433/commit/edd91596f79adbd0584cfa8e38dbc9241e0f53f0))
* add event normalizer and websocket coordinator ([9cf49a1](https://github.com/rtl-433-hass/rtl_433/commit/9cf49a10f977642d52a44cb1d46243211ffc5d07))
* add placeholder brand icon and logo ([87acb93](https://github.com/rtl-433-hass/rtl_433/commit/87acb9330a157d33a3e2c5bc7de01d2713e963bf))
* add rtl_433 integration package skeleton ([13d5113](https://github.com/rtl-433-hass/rtl_433/commit/13d5113c9a41b0f061a0d08b404a625440015e2c))
* add sensor and binary_sensor platforms ([2aa8fd9](https://github.com/rtl-433-hass/rtl_433/commit/2aa8fd91c093c8c83780e530fb2a9878c504288e))
* ship brand images in-repo for HA 2026.3+ local serving ([3e8badd](https://github.com/rtl-433-hass/rtl_433/commit/3e8baddca24ae7f7014c7c851fa0aaa5f66a7449))
* wire integration lifecycle, diagnostics and repairs ([9d1cf4c](https://github.com/rtl-433-hass/rtl_433/commit/9d1cf4c0ab34b631b01c1c25691e5b6068e00b13))


### Bug Fixes

* avoid event-loop YAML load during entity setup ([d29aa89](https://github.com/rtl-433-hass/rtl_433/commit/d29aa899fda3635125fbfb5a785668fdbf2c9c7d))
* correct server_unreachable repair issue translation for hassfest ([f535aff](https://github.com/rtl-433-hass/rtl_433/commit/f535aff7d556956ced9ce1e0a1bf157824457692))


### Documentation

* add Apache-2.0 license ([ab2c6a9](https://github.com/rtl-433-hass/rtl_433/commit/ab2c6a98ac44f31ff2d8ae1a2bc5132f5b134224))
* add README, AGENTS.md and CONTRIBUTING ([dc0f0c4](https://github.com/rtl-433-hass/rtl_433/commit/dc0f0c41e1e011c0b8fbe224d35dcc007eeb1661))
* append execution summary to plan 01 ([6fee65d](https://github.com/rtl-433-hass/rtl_433/commit/6fee65d929e2f1e2f707ff48a1b480e7ba8dc75d))
* initial integration plan ([9dec7f0](https://github.com/rtl-433-hass/rtl_433/commit/9dec7f0ee3167dd909b521bd2c979153c8442385))
* regenerate README screenshots with brand icon and Demo User ([4732282](https://github.com/rtl-433-hass/rtl_433/commit/4732282f5d7a510d389a52dcd3218e6dee7f8249))
* use uv instead of pip in instructions ([85d4036](https://github.com/rtl-433-hass/rtl_433/commit/85d40360f19be490ab191e0956bd44e7ced681fc))
