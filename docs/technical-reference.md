# Plenitude Solar Panels — technical reference

Detailed English reference retained for maintainers. See the
[Italian README](../README.md) for installation and initial configuration,
and the [recovery guide](recupero-produzione.md) for an Italian overview.

Custom integration that creates a device for every Plenitude photovoltaic panel.
Each device provides the API's five-minute average power (`W`), a production
sensor for the same five-minute interval (`Wh`), and an irradiance sensor
(`W/m2`) when the API supplies it. The API reports the average power as an
hourly-equivalent Wh value; the integration uses the unchanged value for
`Average Power (5 min)` and divides it by 12 before displaying or accumulating
the five-minute energy. It also provides a cumulative `Total Production` sensor
(`Wh`) for the Energy Dashboard.

## Install

Copy `custom_components/plenitude_panels` into the `custom_components` directory
of the Home Assistant configuration directory, then restart Home Assistant.
Add **Plenitude Solar Panels** from **Settings > Devices & services**.

## Configuration values

The integration needs browser-session values entered directly in its UI:

- The complete `Cookie` request header, including `GYSESSION` and `GAESA`.
- The complete browser **User-Agent**, copied from the same successful request
  as the Cookie header. The default is Chrome 156 on Windows; existing entries
  use this default until a custom value is saved. Chromium client hints follow
  the configured browser version, platform and mobile indicator automatically.
- The Plenitude account ID and supply ID.
- Optionally, the **POD** used by the `productions-consumes` endpoint (distinct
  from the supply ID), to enable historical production recovery.
- **Recover missing production**, enabled by default. Without a POD, recovery
  waits for configuration. Disabling this switch leaves live monitoring active.

Plenitude login includes CAPTCHA and may require OTP, so Home Assistant cannot
perform the initial browser login. At startup and then every five minutes, the
integration calls `c360/init` with the saved browser session and persists all
returned cookie updates, including `GAESA` and `GYSESSION`. This session
maintenance continues even when panel data is configured to refresh less often.

When Plenitude rejects the saved browser session, Home Assistant starts a
reauthentication flow. Capture Cookie and User-Agent from the same successful
API request after a fresh browser login. Both fields are available in that flow.

HTTP 406 means the server rejected the request; it does not prove the cookie
expired. First check that Cookie and User-Agent match the working browser session.
If rejection persists, site protection may also check other client characteristics
or the network address. A matching User-Agent alone cannot guarantee acceptance.
Diagnostic logs identify the endpoint, HTTP status, response content type and
returned cookie names without recording cookie values or response bodies.

To replace credentials before they expire, or to change the account ID,
supply ID, User-Agent, POD, recovery switch, or polling interval, open the integration and
select **Configure > Settings**.
The submitted values are validated before they are saved and the integration is
reloaded.

ConfigEntry data is stored by Home Assistant under `.storage` and is not
encrypted. Protect the Home Assistant configuration directory and do not share
diagnostics containing its configuration data.

## Energy Dashboard

Every panel device exposes a `Total Production` sensor with Home Assistant's
`energy` device class and `total_increasing` state class. To add a panel to the
Energy Dashboard:

1. Open **Settings > Dashboards > Energy**.
2. Under **Solar panels**, select **Add solar production**.
3. Select that panel's `Total Production` sensor.

The total starts at the first successful integration reading and is restored
after normal Home Assistant restarts. Existing totals are retained when updating
the integration; only readings collected after version 0.3.0 use the corrected
five-minute conversion. It intentionally does not estimate energy
for five-minute windows that occur while Home Assistant is offline. Do not set
the integration polling interval above five minutes if the cumulative total is
used for energy reporting.

## Missing-production recovery

With a POD configured and recovery enabled, the integration checks completed
days once per hour, retaining the last seven days of live five-minute samples
in Home Assistant storage across restarts. A live reading is assigned to the
preceding five-minute window (including the previous day at midnight). It compares them with the aggregate
quarter-hour `productions-consumes` API. Aggregate values in Wh are already
interval energy and are not divided by 12.

For panels with no positive readings during a day, the integration subtracts
the other panels' recorded production from each aggregate interval and divides
the positive remainder proportionally to the affected panels' nominal power.
If any affected power is unavailable or zero, it uses equal shares. This is an
estimate: the aggregate API cannot distinguish a real panel outage from missing
panel telemetry. Panels with any positive daily production are not reconstructed.

Reconstruction requires all three live sampling windows in every productive
quarter-hour, valid energy on the nonzero panels, a stable panel inventory and a
complete aggregate day. Missing windows (including offline time or longer polling
intervals) defer recovery; the integration does not invent the other panels'
production. Keep polling at five minutes. All-zero or incomplete aggregate
responses are retried within the seven-day window. Naive ambiguous timestamps on
the autumn DST transition are deferred; offset-aware 23/25-hour days are supported.
Day labels without an offset are interpreted in Home Assistant's local timezone.

Recovered energy increases `Total Production`. After Recorder has recorded that
increase and compiled its statistics, the integration moves its statistical
contribution to the original day, spreading each quarter over its three
five-minute statistical buckets. The Energy Dashboard's hourly sums are also
corrected, without counting the increase again on the recovery day. Missing
hourly buckets and a baseline are created for the requested day; five-minute
buckets are filled only within Recorder's retention period. If the recovery
was already part of Recorder's initial state and never counted as an increase,
it is added to the later statistical sums as well. Raw state
history still shows when the total was updated. Recorder must include these
sensors for historical correction; pending corrections retry on the five-minute
session heartbeat, including with longer panel polling intervals.
Transactional, non-energy `plenitude_panels:recovery_audit_*` statistic markers
make retries safe across restarts. Do not select audit markers as energy sources.

`Total Production` attributes expose `recovered_energy_wh`, `last_recovered_day`,
`recovery_status` and the number of `recovery_pending_statistics` days. Turning recovery off stops new allocations, retains
previously recovered energy, and allows already allocated statistical corrections
to finish. Automatic recovery uses only the locally collected history. Manual
recovery can supplement it with Recorder or explicit user input.

### Manual recovery

Open **Settings > Devices & services > Plenitude > Configure > Recover production**.
The wizard is separate from Settings and does not reload the integration or
change its credentials. It works even with automatic recovery disabled.

1. Choose a date **before today**. There is no seven-day limit for manual requests;
   the aggregate API must still return a complete, positive day for that date.
2. The integration first analyzes local samples and supplements gaps with
   Recorder's original cumulative states. Only consecutive recorded sampling IDs
   are converted into five-minute production; missing readings are not filled
   forward. Daily recorded energy is read using the same normalized statistics
   change API as the Energy Dashboard, converted to Wh. Missing midnight or
   nighttime hourly points no longer discard valid daytime production.
   Each panel is evaluated independently: available totals remain visible even
   if another panel has no history. Only recoveries already applied to the
   requested day's statistics are subtracted from those totals.
3. With sufficient samples, the wizard proposes the interval residuals on panels
   with no positive production. Otherwise it asks for recipient panels and the
   **original production already counted across all panels, excluding previous
   recoveries**, in Wh. If nothing was counted, explicitly enter **0**. This
   daily estimate uses `aggregate - original energy - previous recoveries` and
   distributes the remainder over the aggregate quarter-hour profile, by nominal
   panel power (equal shares when any required power is unavailable).
4. Review the aggregate, original energy, previous recoveries and the new Wh for
   each panel. The **≈** mode indicates a daily estimate rather than measured
   interval residuals. **Edit allocation** lets you change recipients or supply
   the original daily energy manually. **Confirm and apply recovery** commits it;
   closing the wizard before confirmation does not allocate energy.

After confirmation, the completion screen offers **Previous day** and **Next day**
shortcuts with their dates, plus **Close**. A shortcut immediately analyzes that
day without reopening Configure; recipients and quantities are determined again
and still require confirmation. Next day is hidden when it would point to today.
If analysis fails, the requested date remains selected for retry. Returning to
the completion screen does not apply the previous recovery again.

The analysis and preview show one summary item per line and a per-panel table
with original recorded production, previous recovery and the calculated new
allocation. **—** means unavailable (or not calculated yet); **≥** denotes an
observed lower bound from partial samples. Panels with unavailable data or a
recorded zero are preselected; positive totals or partial positive readings are
not suggested. When all panels lack data, the editable original-energy field
suggests **0 Wh** while the detected totals retain **—**. This is an editable
assumption, not a measured zero. Partial or uncertain data keeps an unknown total
blank. Editing the preview or correcting invalid input preserves your own
recipient selections and energy values, including zero and an empty selection.
New panel allocations are calculated after recipients and original energy have
been supplied, before confirmation. A manually entered global total is not
invented as original production for individual panels.

Summary numbers use the requesting user's saved Home Assistant **number format**,
including comma/decimal, decimal/comma, French spacing, Swiss grouping and no
grouping. Language-based formatting uses the user's language, then browser request
language and Home Assistant's configured language. Browser-system mode uses the
request's `Accept-Language`, with language fallback when no request is available.
No other user's preferences are used. Values are displayed with three decimal
places; calculation precision is unchanged and the editable numeric field remains
Home Assistant's native number control. The integration declares Babel as a
dependency; Home Assistant installs its locale data automatically on setup.

Totals are published immediately after the durable save. Historical corrections
finish after Recorder has recorded/compiled the increase and a subsequent
heartbeat processes it. Repeated requests account for the persistent per-day
ledger and add only the remaining deficit. Concurrent polling/recoveries cannot
apply the same preview twice: a changed ledger, supply or panel inventory
requires a fresh analysis. Existing energy is not reduced or moved to different
panels by this wizard.

Version 0.7 pending recoveries are imported into the ledger. Completed recoveries
are reconstructed from their Recorder audit markers when available; if an older
amount cannot be attributed reliably, the wizard reports that instead of risking
duplicate energy. Keep Recorder audit markers alongside the recovery storage.

### Repairing statistics

If the Energy Dashboard shows a negative production spike, open **Configure >
Repair statistics** and select the completed day. The integration compares the
aggregate API with Recorder's hourly statistics and identifies falling sums.
Select the affected panels and review the current and proposed daily energy for
each panel before confirming. Selected panels' daily statistical curves are
**replaced** with the aggregate profile, divided by nominal power after retaining
valid, unselected panel totals and the selected panels' prior recovery amounts.
The profile and distribution are estimates, not original per-panel measurements.
Finite negative production API samples are treated as zero; invalid or absent
values remain unavailable. The tool displays the number of negative aggregate
samples encountered.

The repair shifts later cumulative statistical sums to preserve subsequent
increments. It does not change current sensor totals, recovered-energy amounts,
or raw sensor state history. Independent anomalies on later days require their
own repair. Recorder must be enabled and all panels must have statistics; the POD
and aggregate API data are required. Pending recoveries must have been recorded
and compiled; unrelated unapplied recoveries must be resolved first. Local day
boundaries must align with UTC hours; ambiguous API timestamps are deferred.

The previous values are durably saved before applying the database transaction.
**Configure > Undo last statistics repair** restores the most recent repair,
provided the saved rows and audit markers have not since been changed. Later
appended statistics are adjusted to remove the repair's offset. Applying a new
repair replaces the previous undo checkpoint. The journal resolves an interrupted
operation on a subsequent Recorder heartbeat or when the tool is reopened.

The recovery code now refuses to apply a historical correction across falling
cumulative sums; use Repair statistics for the affected date before retrying the
pending correction. Correctly stored kWh/MWh sensor states are converted back to
Wh when restoring the integration's cumulative sensor.

### Recovery tests

With Home Assistant, the integration's Babel requirement and pytest installed,
run from the repository root:

```sh
python -m pytest tests -q
```

The tests use Home Assistant's Recorder models with an in-memory SQLite database
to verify historical sum adjustments, transactional rollback and replay safety.

## Version and changelog

The integration version is declared in
`custom_components/plenitude_panels/manifest.json` and is shown by Home
Assistant in the integration details. Changes for each release are recorded in
`custom_components/plenitude_panels/CHANGELOG.md`.
