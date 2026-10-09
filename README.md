# Lockbox Python API

Python 3.11+ synchronous controls for the Lockbox server. Install from this
directory with `python -m pip install .` (or `python -m pip install -e ".[dev]"`
for development).

```python
from lockbox import Server, SlopePreference

with Server("http://lockboxcontrol.internal:5106") as server:
    box = server.get_lockbox(1)  # Choose an ID from server.list_lockboxes().
    box.on_coarse_resonance_found(lambda: print("Coarse resonance found"))
    box.on_fine_resonance_found(lambda: print("Fine resonance found"))
    box.on_lock_acquired(server.stop)

    box.set_setpoint(0.0)
    box.set_low_high_threshold(-100, 100, SlopePreference.POSITIVE)
    box.lock()
    server.run_forever()  # Wait until lock is acquired, or press Ctrl+C.
```

`Server` owns one HTTP client and two SignalR connections: `/Hubs/Status` for
events and `/Hubs/Run` for setpoint and threshold commands. It uses the server's
JSON hub protocol. Entering the context connects both hubs; exiting closes all
connections without changing the hardware. There are no reconnects or retries.
`Server(url, timeout=10.0)` sets HTTP timeouts and the time allowed for hub
handshake readiness and command completion, not for physical device operations.
Supply the server root URL, including any hosting path prefix, without `/api`.

`list_lockboxes()` and `get_lockbox(id)` reuse objects by ID, preserving callbacks
while refreshing their name, IP and MAC metadata. Objects are registered for
status routing automatically. `connect_to_status_hub(box)` also registers manually
created objects; repeating registration of the same object is harmless.

## Keeping an experiment running

Call `server.run_forever()` inside the `with` block to keep the script alive.
It blocks the calling thread while callbacks continue on the SignalR status
receiver thread. Call `server.stop()` from a callback or another thread to return
from the wait. Do not call `run_forever()` inside a callback.

`stop()` is idempotent and only stops waiting: it leaves connections open and
does not change hardware state. Exiting the context closes connections. Ctrl+C
propagates `KeyboardInterrupt`, so leaving the context also cleans up normally.
The wait uses 0.1-second blocking intervals to stay responsive on Windows.

A stop received before `run_forever()` is remembered. Subsequent waits in the
same context return immediately; re-entering the context resets this state.
Calling `run_forever()` outside an active context raises `RuntimeError`.
If either hub closes unexpectedly, the wait raises `ConnectionError` identifying
the hub, including when the disconnect happened before the wait. A recorded
disconnect takes precedence over `stop()`. Intentional context cleanup releases
the wait without creating a disconnect error. There is no automatic reconnection.

## Commands

All command methods return `None` after HTTP success or Run hub invocation
completion. They do not wait for a status or physical completion. Failed HTTP
responses raise `LockboxHttpError` with the method, path, HTTP status/reason, and
response body in its message; hub errors use the server-provided
error message. Transport errors propagate.

| Method | Argument convention |
| --- | --- |
| `set_gain(gain)` | Input gain, at least 1 |
| `set_demodulation_cutoff(frequency)` | Hz |
| `set_demodulation_phase(phase)` | Radians |
| `set_demodulation_phase_deg(phase)` | Degrees; converted to radians before sending |
| `set_modulation_amplitude(amplitude)` | Integer DAC codes, 0–524287 |
| `set_demodulation_amplitude(amplitude)` | Server amplitude scale, 0–100 |
| `set_setpoint(setpoint)` | Native device setpoint units |
| `set_pid(p, i, d)` | Native device PID coefficients |
| `set_threshold(threshold)` | Integer reflection threshold |
| `set_low_high_threshold(low, high, slope_preference=SlopePreference.NONE)` | Integer error-signal limits; enum values `NONE`, `NEGATIVE`, `POSITIVE` |
| `set_fine_output_code(code)`, `set_coarse_output_code(code)` | Signed DAC codes, -524287–524287 |
| `lock()`, `unlock()` | Normal lock; unlock without starting a scan |
| `stop()`, `go()` | Stop or run the device, matching GUI Stop/Start |
| `coarse_scan_only(lower_bound=-524287, upper_bound=524287, period_ms=0)` | Coarse-only scan; bounds in DAC codes, period in milliseconds |
| `fine_scan_only(lower_bound=-524287, upper_bound=524287, period_ms=0)` | Fine-only scan; bounds in DAC codes, period in milliseconds |
| `freeze()`, `unfreeze()` | Both toggle freeze through the same endpoint |
| `reload_configuration()` | Apply the server's saved configuration |

Setters change runtime values, not saved configuration. Validation is left to
the server, except conversion of the slope enum. Slope preference is forwarded
as the server enum value (0, 1, or 2). The current firmware `ThresholdsEndpoint`
updates limits but does not read the slope query parameter; actual slope behavior
depends on firmware support. This package does not change firmware.

`box.stop()` sends `POST /api/Run/stop?teensyId={id}` and `box.go()` sends
`POST /api/Run/start?teensyId={id}`, both without a request body. They return
on HTTP success and raise `LockboxHttpError` on failure.
`box.stop()` stops the device; `server.stop()` only releases `run_forever()`.

```python
box.stop()
box.go()  # Same action as Start in the GUI.
```

Catch HTTP errors by status without parsing their message:

```python
from lockbox import LockboxHttpError

try:
    box.go()
except LockboxHttpError as error:
    if error.status_code == 409:
        print("Lockbox is already running:", error)
    else:
        raise
```

For Stop, 409 means already stopped; for Go, it means already running.
The updated server preserves the firmware status and message for these commands.
Older servers may translate these errors into 400 or 500; Python reports the
status it actually receives. `error.body` preserves the response text, and
`error.response` exposes the original HTTP response. This replaces the previous
body-only exception message, including for saved-configuration errors.

`set_demodulation_phase()` takes **radians**. Use the degrees helper when more
convenient; both methods send the same runtime command:

```python
import math

box.set_demodulation_phase(math.pi / 2)  # 90 degrees, expressed in radians.
box.set_demodulation_phase_deg(90)      # Equivalent command using degrees.
```

The freeze endpoint may translate a firmware rejection (including not-ready 409)
into a server 500. The client reports the response it receives.

### Scan-only operation

Both scan methods use the GUI's `POST /api/Run/scan` endpoint and start a scan
without attempting to acquire lock. A zero period leaves the device's scan period
unchanged. All three scan parameters are sent so custom bounds are not discarded
by the server when the period is omitted in Python.

```python
box.coarse_scan_only()  # Full DAC range, existing scan period.
# Or select a fine-only scan with explicit bounds and timing:
box.fine_scan_only(lower_bound=-1000, upper_bound=1000, period_ms=2000)
```

These are runtime commands: they return on HTTP success, do not wait for the scan,
and do not save configuration. Calling the second method switches the scan mode.

## Callbacks

Register callbacks after entering the context. Each registration stores one
zero-argument function; registering again replaces it.

| Registration | Incoming Arduino status |
| --- | --- |
| `on_coarse_resonance_found(callback)` | `CoarseResonanceFound` |
| `on_fine_resonance_found(callback)` | `FineResonanceFound` |
| `on_lock_acquired(callback)` | `Locked` |
| `on_lock_lost(callback)` | `Unlocked` |
| `on_lockbox_frozen(callback)` | `FrozenEnabled` |
| `on_lockbox_unfrozen(callback)` | `FrozenDisabled` |
| `on_lockbox_freeze_ready(callback)` | `DriftEstimateReady` |

`on_lockbox_recentering` raises `NotImplementedError` because no dedicated
status exists. HTTP results never trigger callbacks.
Unregistered devices and unrelated statuses are ignored.

`on_lock_acquired` requires firmware and server versions supporting `Locked`
(status 26). It reports completed acquisition and entry into feedback control;
it does not guarantee continued lock stability. Each successful reacquisition
emits another event. `FineResonanceFound` remains a separate event and does not
trigger this callback. Initial startup and freeze toggles do not emit `Locked`.

Functions run directly on the status receiver thread, without arguments or custom
exception recovery. They may issue commands using the separate HTTP/Run connections.
While a callback runs, it delays further status delivery: do not wait inside it
for another callback. There is no replay, ordering contract, or experiment scheduler.

## Saved configuration

Fetch an independent snapshot, edit its supported fields, and explicitly save it:

```python
from lockbox import Server, PidControllerType, SlopePreference

with Server("http://lockboxcontrol.internal:5106") as server:
    box = server.get_lockbox(1)
    config = box.get_configuration()
    config.pid.proportional_gain = 2.0
    config.pid.controller_type = PidControllerType.FILTERED_PID
    config.demodulation.set_point = 0.1
    config.lock_thresholds.out_of_lock_threshold = 80
    config.lock_thresholds.ms_without_lock = 250
    config.lock_thresholds.delock_smoothing_constant = 0.25
    config.coarse_scan.scan_min = -2000
    config.coarse_scan.scan_max = 2000
    config.fine_scan.scan_time_ms = 1500
    if config.pdh is not None:
        config.pdh.low_limit = -100.5
        config.pdh.high_limit = 100.25
        config.pdh.slope_preference = SlopePreference.POSITIVE
    box.save_configuration(config)

    # Apply only when wanted:
    box.reload_configuration()
```

`get_configuration()` reads `GET /api/LockboxConfigurations/arduino/{id}` on
every call. It returns a `LockboxConfiguration` with typed, snake_case sections:
`pid`, `demodulation`, `lock_thresholds`, `pdh`, `coarse_scan`, `fine_scan`,
`coarse_refinement`, `fine_refinement`, and top-level `modulation_amplitude`.
An absent optional PDH section is `None`. Runtime setters do not modify snapshots.
`PidControllerType` has `SIMPLE_PI` and `FILTERED_PID`; slope settings reuse
`SlopePreference.NONE`, `NEGATIVE`, and `POSITIVE`.

All lock-threshold and coarse/fine scan settings are editable. IDs, device
identity, section references, `notch_filters`, and
`filter_selections` are read-only. Filter selections expose `notch_filter_id`,
`enabled`, and the associated read-only `notch_filter` definition when present.
Snapshots update existing records only; a missing configuration raises the normal
HTTP error. They cannot create records or replace section identities.

`save_configuration(config)` returns `None` after these eight sequential writes
(seven when PDH is absent):

| Saved settings | Endpoint |
| --- | --- |
| PID coefficients, controller type, derivative cutoff | `PUT /api/PidConfigurations/{pidId}/persist` |
| Demodulation cutoff, phase, amplitude, setpoint | `PUT /api/DemodulationConfigurations/{id}/persist` |
| Modulation amplitude | `PUT /api/PidConfigurations/{pidId}/modulation` |
| All lock-threshold settings | `PUT /api/LockThresholdSettings/{id}` |
| PDH low/high limits and slope, when present | `PUT /api/PdhSettings/{id}` |
| Coarse scan limits and time | `PUT /api/ScanSettings/{coarseId}` |
| Fine scan limits and time | `PUT /api/ScanSettings/{fineId}` |
| Coarse and fine refinement | `PUT /api/PidConfigurations/{pidId}/refinement` |

Saving does not apply hardware settings or modify filters. It sends all supported
values, including unchanged ones. Fetch again before editing if another client
may have changed the saved configuration; there is no conflict detection.

The snapshot's device/server identity, required linked records, and payloads are
checked before any writes, including both required scan records. Nonfinite or
unserializable payloads are rejected before writing. PDH limits accept decimal
values and are transmitted without integer conversion. Range and ordering
validation is performed by the server. PDH is skipped only when both its section
and linked ID are absent; a linked ID with a missing section is an error.

**Saving is not atomic.** A failed response stops subsequent requests and raises
`LockboxHttpError`; earlier successful writes remain saved. There is no
rollback or retry. Other server-side validation errors can therefore leave a
partially saved configuration. The ordinary PID update endpoint is not used
because it also applies changes to hardware.

This version requires the server's resource-style PUT endpoints for
`LockThresholdSettings`, `PdhSettings`, and `ScanSettings`. There is no fallback
to the old integer-only threshold-saving endpoint. Notch-filter editing remains
excluded; definitions, selections, and enabled flags are preserved unchanged.
Runtime setters still use their existing interfaces; fractional PDH support here
applies to saved configuration.

## Development

Run `python -m pytest` and `python -m ruff check src tests` from this directory.
Tests use mock HTTP responses and hub connections; they do not operate hardware.
