# Changelog

## [0.19.0](https://github.com/rtl-433-hass/rtl_433/compare/v0.18.1...v0.19.0) (2026-06-22)


### Features

* **repairs:** let users keep a low sample rate and silence the advisory ([840d945](https://github.com/rtl-433-hass/rtl_433/commit/840d945d1135f78c7e074eab062e83613349f2d3))

## [0.18.1](https://github.com/rtl-433-hass/rtl_433/compare/v0.18.0...v0.18.1) (2026-06-22)


### Bug Fixes

* **ci:** avoid pre-commit-uv Python assertion ([#95](https://github.com/rtl-433-hass/rtl_433/issues/95)) ([7262e81](https://github.com/rtl-433-hass/rtl_433/commit/7262e8136f9ab3810e2654e24bf270b90e119c4c))
* **device_trigger:** stop phantom event fire on config-entry reload ([4c188b3](https://github.com/rtl-433-hass/rtl_433/commit/4c188b3628fa8d365cda0fdaf0642b0b603cae7e))


### Documentation

* create mkdocs site foundation ([50d722a](https://github.com/rtl-433-hass/rtl_433/commit/50d722aaf854603d56b7806a631c80b0d33d51d1))
* refine integration documentation site ([9f1b715](https://github.com/rtl-433-hass/rtl_433/commit/9f1b715ae9fa36a7b99481a5e94162c699d28f61))
* refresh and expand UI screenshots ([d420c50](https://github.com/rtl-433-hass/rtl_433/commit/d420c506335f3cefd95fffa3ce403d2d66baf0f4))

## [0.18.0](https://github.com/rtl-433-hass/rtl_433/compare/v0.17.0...v0.18.0) (2026-06-16)


### Features

* **logging:** simplify user-facing log and notification wording ([b79cfad](https://github.com/rtl-433-hass/rtl_433/commit/b79cfadd5870474e515786e35dc42158760fe397))


### Bug Fixes

* **calibration:** reject non-positive calibration scale ([06baec9](https://github.com/rtl-433-hass/rtl_433/commit/06baec98b18a39df11bd65c1d609e05f7ee8b94d))
* **config_flow:** guard discovery adopt against identity corruption ([a73609e](https://github.com/rtl-433-hass/rtl_433/commit/a73609e4e2e017f9cb869bed49b8215776ab0110))
* **coordinator:** clamp replay high-water mark to now ([d5f2e14](https://github.com/rtl-433-hass/rtl_433/commit/d5f2e1452cb5480bec67a00c4b016fddc7a855a7))
* **event:** stop watchdog availability re-paint from firing phantom events ([d58c6b2](https://github.com/rtl-433-hass/rtl_433/commit/d58c6b28c5346e002200da3a9ec01c98e4a0a7bd))
* **library:** sentence-case entity names and auto-name device-class fields ([67f542d](https://github.com/rtl-433-hass/rtl_433/commit/67f542d640d5c8c3a5ab3127442478232e298456))


### Documentation

* **comments:** drop planning-doc references and trim duplicated rationale ([5e3c2e3](https://github.com/rtl-433-hass/rtl_433/commit/5e3c2e39d404f79af7a8479e1e9d1346fabe2143))

## [0.17.0](https://github.com/rtl-433-hass/rtl_433/compare/v0.16.0...v0.17.0) (2026-06-16)


### Features

* **logging:** add DEBUG end-to-end event trace ([c6a3243](https://github.com/rtl-433-hass/rtl_433/commit/c6a32438da3d7f382b3de4012d8463cd1a2b8f59))
* **logging:** add lifecycle, discovery, availability & decoder DEBUG traces ([07f9bd2](https://github.com/rtl-433-hass/rtl_433/commit/07f9bd22ffc2c5718934eb14c76925e5cc438575))


### Bug Fixes

* exclude hub sensor attributes from the recorder ([5ed31a2](https://github.com/rtl-433-hass/rtl_433/commit/5ed31a24d21c17af6aa526c61178aa5d916b24fe))

## [0.16.0](https://github.com/rtl-433-hass/rtl_433/compare/v0.15.0...v0.16.0) (2026-06-10)


### Features

* apply rtl_433's 1.024 MS/s default from the low-sample-rate repair ([fbfbc7b](https://github.com/rtl-433-hass/rtl_433/commit/fbfbc7bbec7bf045fc5524d37944331339dac99d)), closes [#69](https://github.com/rtl-433-hass/rtl_433/issues/69)


### Bug Fixes

* stop event devices re-firing on restart and going unavailable ([#75](https://github.com/rtl-433-hass/rtl_433/issues/75)) ([b874179](https://github.com/rtl-433-hass/rtl_433/commit/b87417963b3c333435ec1e77fe394a8725e5a3e7))

## [0.15.0](https://github.com/rtl-433-hass/rtl_433/compare/v0.14.0...v0.15.0) (2026-06-05)


### Features

* enable Last seen by default for event-driven devices ([0063599](https://github.com/rtl-433-hass/rtl_433/commit/0063599759ffd728d5d63a0876ee717adcb9c18d))
* never-expire availability for event-driven devices ([27a5b95](https://github.com/rtl-433-hass/rtl_433/commit/27a5b959c1b1530f373a63d9aea68b510dbff3b3))

## [0.14.0](https://github.com/rtl-433-hass/rtl_433/compare/v0.13.0...v0.14.0) (2026-06-04)


### Features

* standardize doorbell events on DoorbellEventType.RING ([#61](https://github.com/rtl-433-hass/rtl_433/issues/61)) ([ffe9a96](https://github.com/rtl-433-hass/rtl_433/commit/ffe9a96c44258616ab6c8de93f0b08cd92eb1721))

## [0.13.0](https://github.com/rtl-433-hass/rtl_433/compare/v0.12.0...v0.13.0) (2026-06-04)


### Features

* advise on low sample rate for high-band single-frequency receivers ([7dcb40e](https://github.com/rtl-433-hass/rtl_433/commit/7dcb40e4f0df8397b3558852f422e7299f134978))
* offer radio rebind at discovery and from the unreachable repair ([3ea1b58](https://github.com/rtl-433-hass/rtl_433/commit/3ea1b5873bdd5e91839abbfd6b95a4bffbc66841))
* rebind a hub to a replacement radio via reconfigure ([bcc1ee8](https://github.com/rtl-433-hass/rtl_433/commit/bcc1ee8331216db436a9d5b32f0d247f9f745071))


### Documentation

* note radio rebind paths in AGENTS.md config-flow inventory ([911c810](https://github.com/rtl-433-hass/rtl_433/commit/911c810e5a4b43e1f6bf9e4c9dee20c3fab341ab))

## [0.12.0](https://github.com/rtl-433-hass/rtl_433/compare/v0.11.1...v0.12.0) (2026-06-03)


### Features

* add per-device signal level diagnostic sensors ([02e3a4f](https://github.com/rtl-433-hass/rtl_433/commit/02e3a4f7f34a09c5ad6bf787e1b3ac82fea2f37c))
* **hub:** show the SDR model and serial on the hub device ([82f2ab9](https://github.com/rtl-433-hass/rtl_433/commit/82f2ab91463b9b5f460df0e23bd152ca4ecb6185))


### Documentation

* document per-device signal level diagnostics ([a162702](https://github.com/rtl-433-hass/rtl_433/commit/a162702e20d2f33c3fe928cff6389346ec7bfa22))

## [0.11.1](https://github.com/rtl-433-hass/rtl_433/compare/v0.11.0...v0.11.1) (2026-06-02)


### Bug Fixes

* **coordinator:** gate device registration to post-connection messages ([323396e](https://github.com/rtl-433-hass/rtl_433/commit/323396e1973e8ffe5c72df165671d3be003f4686))
* **coordinator:** make setup-time initial frequency authoritative ([f96e2b4](https://github.com/rtl-433-hass/rtl_433/commit/f96e2b4ef3970353b0deac373102fec502377074))
* **coordinator:** surface malformed getter JSON at error level ([d7e5236](https://github.com/rtl-433-hass/rtl_433/commit/d7e52360dee608621ee2725b2ce767a8e51d939f))
* **entity:** drop redundant model from device name ([be99f6d](https://github.com/rtl-433-hass/rtl_433/commit/be99f6dfd0180721e728d6be1c21ec28d43b9879))


### Documentation

* document post-connection registration gate and authoritative initial frequency ([fa1a437](https://github.com/rtl-433-hass/rtl_433/commit/fa1a437a226f318464fa0c93957ff627954b4046))
* **tasks:** add plan 18 for frequency and device-registration fixes ([0da3b71](https://github.com/rtl-433-hass/rtl_433/commit/0da3b7121f7e0e6fd95d8e30d6daa761b7ef48cd))
* **tasks:** archive plan 19 (mutation matrix) ([5a766d1](https://github.com/rtl-433-hass/rtl_433/commit/5a766d1b8a6bf6d2c1e011ac92417ef5966c2877))

## [0.11.0](https://github.com/rtl-433-hass/rtl_433/compare/v0.10.0...v0.11.0) (2026-06-02)


### Features

* **config_flow:** default initial frequency to 433.92 MHz and reorder add-time fields ([f955e08](https://github.com/rtl-433-hass/rtl_433/commit/f955e08dea31e138b58d19a73e2ee06787568159))

## [0.10.0](https://github.com/rtl-433-hass/rtl_433/compare/v0.9.1...v0.10.0) (2026-06-01)


### Features

* **config_flow:** choose manage/discovery toggles and an initial frequency at setup ([16a954d](https://github.com/rtl-433-hass/rtl_433/commit/16a954d240b8bdfc5a5c4b573d1e9a05db902ced))
* **sdr:** present center frequency in MHz with a store upgrade path ([53777e6](https://github.com/rtl-433-hass/rtl_433/commit/53777e6623813b03675f27a503c410d4972ff3a5))


### Documentation

* document add-time toggles, initial frequency, and MHz center frequency ([082df07](https://github.com/rtl-433-hass/rtl_433/commit/082df07d43258d9c3e4940e209b730644d04d55d))

## [0.9.1](https://github.com/rtl-433-hass/rtl_433/compare/v0.9.0...v0.9.1) (2026-06-01)


### Bug Fixes

* **config_flow:** refine hassio confirm dialog copy ([aeb8d72](https://github.com/rtl-433-hass/rtl_433/commit/aeb8d729d0359f6b4564f7fbe264d8a3e9113293))

## [0.9.0](https://github.com/rtl-433-hass/rtl_433/compare/v0.8.0...v0.9.0) (2026-06-01)


### Features

* **availability:** allow 0 (never) in config flow, docs for class defaults ([eda8286](https://github.com/rtl-433-hass/rtl_433/commit/eda82866cbdf8d0038e29e3b99de516f1b425b1e))
* **availability:** device-class-aware timeouts and never-expire resolution ([7a59c40](https://github.com/rtl-433-hass/rtl_433/commit/7a59c4069d4d011aa5fcf07bcf60e55b4daefcbe))
* **availability:** migrate legacy 600s hub timeout to class defaults ([acc8ec3](https://github.com/rtl-433-hass/rtl_433/commit/acc8ec3d10950e4ac50105d5543ad1571801381a))
* **config_flow:** support Supervisor (hassio) radio discovery ([7d3da09](https://github.com/rtl-433-hass/rtl_433/commit/7d3da09fde1422c64435d970162fcae3f6249f8e))
* **device-library:** map WH51 soil ad_raw and boost fields ([#39](https://github.com/rtl-433-hass/rtl_433/issues/39)) ([e73013d](https://github.com/rtl-433-hass/rtl_433/commit/e73013deb30b9cad68a8cb72c6b9fadab8614cab))
* **sensor:** ship Last seen disabled by default ([#41](https://github.com/rtl-433-hass/rtl_433/issues/41)) ([b08de9f](https://github.com/rtl-433-hass/rtl_433/commit/b08de9f65f2eb0f63ba27594b53e3a41fb1f8268))


### Documentation

* **plan:** archive completed plan 07 (availability timeout device-class defaults) ([5feb2a4](https://github.com/rtl-433-hass/rtl_433/commit/5feb2a4b4e96b026f69fae57dee053fbc2b7cd67))

## [0.8.0](https://github.com/rtl-433-hass/rtl_433/compare/v0.7.0...v0.8.0) (2026-05-30)


### Features

* **rtl_433:** edit mapping overrides in the UI ([#34](https://github.com/rtl-433-hass/rtl_433/issues/34)) ([9354ca3](https://github.com/rtl-433-hass/rtl_433/commit/9354ca39facb40c63f10b7cfd8939913cb3020c8))


### Bug Fixes

* **ci:** scope mutation runs for non-conforming test names ([#38](https://github.com/rtl-433-hass/rtl_433/issues/38)) ([73bb925](https://github.com/rtl-433-hass/rtl_433/commit/73bb925207e008b61397c8ab8ecc1040a0a33f45))
* **rtl_433:** don't notify for new devices on reconnect replay ([c0168b3](https://github.com/rtl-433-hass/rtl_433/commit/c0168b3b8911ff846f32b2df8727664b12aea593))


### Documentation

* **readme:** note Python 3.14 requirement for the test venv ([97b3f00](https://github.com/rtl-433-hass/rtl_433/commit/97b3f001d5232341e2ff1c966a9c5d68bc5ffdc9))
* **rtl_433:** point to reconfigure for connection settings ([#32](https://github.com/rtl-433-hass/rtl_433/issues/32)) ([eae8cb2](https://github.com/rtl-433-hass/rtl_433/commit/eae8cb2843b269382fea6f6b75b20aeed29e7738))
* **tasks:** add plan 14 (mutation testing) and task breakdown ([e051682](https://github.com/rtl-433-hass/rtl_433/commit/e051682602e8bb2864665eb95d4ebd870752f5d4))
* **tasks:** archive plan 14 with execution summary ([3789a61](https://github.com/rtl-433-hass/rtl_433/commit/3789a613db4297237b61309e4ce1bd2f262085b5))

## [0.7.0](https://github.com/rtl-433-hass/rtl_433/compare/v0.6.0...v0.7.0) (2026-05-29)


### Features

* **rtl_433:** add hub reconfigure flow to edit connection params in place ([992ca75](https://github.com/rtl-433-hass/rtl_433/commit/992ca75f2f8a5e27c56e92af0ccfa77b68c4a5ff))
* **rtl_433:** add model-scoped device-library lookup ([f7c6685](https://github.com/rtl-433-hass/rtl_433/commit/f7c66853efee3d801aa5123953792712d53dc13c))
* **rtl_433:** add per-device meter calibration (commodity/unit/scale) ([83fcf00](https://github.com/rtl-433-hass/rtl_433/commit/83fcf001df5e8ccf06af716201e0cf1eb10dea5e))
* **rtl_433:** device triggers for event entities ([#26](https://github.com/rtl-433-hass/rtl_433/issues/26)) ([348698e](https://github.com/rtl-433-hass/rtl_433/commit/348698e2e7a6bbb2d7224ada1ae46b368d272150))
* **rtl_433:** motion as an occupancy binary_sensor with a clear-delay ([#31](https://github.com/rtl-433-hass/rtl_433/issues/31)) ([85897d1](https://github.com/rtl-433-hass/rtl_433/commit/85897d1a3067b5ba2517eef99dab6f387744580b))
* **rtl_433:** persistent notification on new device discovery ([#28](https://github.com/rtl-433-hass/rtl_433/issues/28)) ([6809877](https://github.com/rtl-433-hass/rtl_433/commit/680987770c77c39988dfff6c51ea17c969a7ae50))
* **rtl_433:** suppress replayed history on websocket reconnect ([#24](https://github.com/rtl-433-hass/rtl_433/issues/24)) ([4ba139d](https://github.com/rtl-433-hass/rtl_433/commit/4ba139d306bd9a3415399ac37dd7b4cd39684376))

## [0.6.0](https://github.com/rtl-433-hass/rtl_433/compare/v0.5.0...v0.6.0) (2026-05-27)


### Features

* **rtl_433:** record stats statistics and gate hop/center controls by mode ([0d310fe](https://github.com/rtl-433-hass/rtl_433/commit/0d310feeab8ec07b707df95098ffa7b505b58b8f))


### Bug Fixes

* **rtl_433:** periodically refresh SDR meta so actual sensors don't go stale ([066170a](https://github.com/rtl-433-hass/rtl_433/commit/066170ae14cc359643e4a239165bfb790215d911))

## [0.5.0](https://github.com/rtl-433-hass/rtl_433/compare/v0.4.0...v0.5.0) (2026-05-27)


### Features

* **rtl_433:** add hub SDR control entities + management toggle ([d3b7af8](https://github.com/rtl-433-hass/rtl_433/commit/d3b7af8d0079954aa94edb5c9fc51cabf4c62d49))
* **rtl_433:** add SDR settings registry and management constants ([fbec5c3](https://github.com/rtl-433-hass/rtl_433/commit/fbec5c3a93872aa749eebd29aabb8aae71114cfc))
* **rtl_433:** coordinator desired-state store, write path, adoption, enforcement ([50a9f45](https://github.com/rtl-433-hass/rtl_433/commit/50a9f452cbb6188b20d922fc0f8888a877a956a9))


### Documentation

* **tasks:** generate tasks + execution blueprint for plan 06 ([b8c4b27](https://github.com/rtl-433-hass/rtl_433/commit/b8c4b27f9e663904c72c5994bb13984b9f21fad2))

## [0.4.0](https://github.com/rtl-433-hass/rtl_433/compare/v0.3.0...v0.4.0) (2026-05-27)


### Features

* **rtl_433:** add event entity platform for momentary RF transmissions ([c440f64](https://github.com/rtl-433-hass/rtl_433/commit/c440f6437f9c3b47e6df785d43126d8c6de8d292))
* **rtl_433:** add synthetic per-device "Last seen" timestamp sensor ([f477144](https://github.com/rtl-433-hass/rtl_433/commit/f477144b9e62c8ba595151ac73a66169dac1b6fd))
* **rtl_433:** create a Last-seen sensor for every device ([cff7fc3](https://github.com/rtl-433-hass/rtl_433/commit/cff7fc3eb49a25c8f616de2bb80b07fb0b3f676e))


### Bug Fixes

* **rtl_433:** unwrap result envelope for get_meta/get_stats over /cmd ([92d750d](https://github.com/rtl-433-hass/rtl_433/commit/92d750deeabd18dab5b28666ea056c4acd413318))

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
