# Generated capture fixtures

**Do not hand-edit these files.** They are the verbatim JSON output of the pinned
`rtl_433` container image decoding the pinned `.cu8` captures from the
`rtl_433_tests` submodule. Regenerate instead:

```bash
./scripts/fetch_captures.sh                      # blobless sparse clone, ~13 MB
python3 scripts/regen_capture_fixtures.py        # rewrite the fixtures
python3 scripts/regen_capture_fixtures.py --check # or: fail on drift (what CI runs)
```

## What they are for

The device library's field keys are transcribed by hand from upstream rtl_433
`data_make()` calls in C, and field matching is **case-sensitive**. A
transcription slip is completely silent: keying SCMplus consumption as
`consumption` instead of `Consumption` produced no sensor, no warning, and no
error — just a meter that appeared to have no reading.

`tests/test_fixture_coverage.py` sweeps every fixture in `tests/fixtures/` and
asserts each field resolves to a library descriptor. For the hand-authored
fixtures that check is only as good as the hand-authoring; against these, it
checks the device library against what rtl_433 actually puts on the wire.

`.github/workflows/captures.yml` re-decodes and diffs, so an upstream field
rename or a bump of the pinned image lands as a red build.

| Fixture | Model | Covers |
| --- | --- | --- |
| `acurite_tower.json` | `Acurite-Tower` | `temperature_C`, `humidity`, `battery_ok` — a non-meter protocol, so the mechanism isn't only exercised by the SCM family |
| `scmplus.json` | `SCMplus` | The CamelCase meter fields: `Consumption`, `MeterType`, `ProtocolID`, `EndpointType`, `EndpointID`, `Tamper`, `PacketCRC` |
| `ert_scm.json` | `ERT-SCM` | The lower_snake_case meter fields: `consumption_data`, `ert_type`, `physical_tamper`, `encoder_tamper` |

## Things that will trip you up

**The `.json` files vendored in the `rtl_433_tests` submodule are stale — never
assert against them.** `tests/scmplus/01/g002_912.6M_2359.3k.json` still records
`"model": "SCM+"` with no `id` field; rtl_433 25.12 emits `"model": "SCMplus"`
*and* `id`. That staleness is the whole reason these fixtures are decoded fresh
rather than copied.

**Timestamps are relative (`"@0.045207s"`), and that is deliberate.** Replaying a
file leaves rtl_433's default relative offsets, which are a pure function of the
capture, so regeneration is byte-stable. Passing `-M time:iso` would stamp
wall-clock time and make every single run a diff.

**Repeated events are kept.** A device transmitting the same frame three times in
one capture yields three identical events (see `acurite_tower.json`). These files
are meant to be exactly what rtl_433 produced; deduplicating would make them a
summary rather than a golden output.

**A capture that decodes to zero events is a hard error**, not an empty fixture.
`scmplus/01/g005_912.6M_2359.3k.cu8` is excluded from the manifest for exactly
this reason — it decodes to nothing under 25.12, which is what upstream's
`ignore` marker in that directory is about.
