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

Setup and configuration validation use dedicated HTTP sessions with automatic
cookie handling disabled. Home Assistant's shared cookie jar cannot override
the explicit Cookie header. Only the integration applies successful response
cookie updates; rejected response cookies are not automatically reused.
Temporary validation sessions are detached without closing HA's shared connector.

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

Recovered energy increases `Total Production`, retaining the same statistic IDs.
After Recorder records and compiles a **proven counted increase**, the integration
imports corrected hourly sums through public `async_import_statistics`.
The import covers completed hours before the spike hour (excluding that hour),
creating missing hourly buckets and a baseline when a reliable reference exists.
The final cumulative sum is unchanged. Live sampling remains five-minute; historical
five-minute buckets are not reconstructed or rewritten by recovery.
If an increase may already be part of Recorder's first baseline, or its raw history
or pre-increase statistic has expired, correction stays pending rather than
guessing. Recorder must include these sensors. Pending corrections retry on the
five-minute session heartbeat, including with longer panel polling intervals.
Raw state history still shows when the total changed.

New journals are saved in the integration's existing Store, not Recorder markers.
Legacy non-energy `plenitude_panels:recovery_audit_*` statistics remain read-only
and are read through the public statistics API alongside the integration's energy
IDs (the API suppresses state for a request containing only `has_sum=False`
metadata). Do not select audit markers as energy sources.

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

Summary numbers follow the browser request's `Accept-Language` preferences, falling
back to Home Assistant's configured language and then English. The user's explicit
Home Assistant number-format preference is not applied to these text summaries.
Values are displayed with three decimal
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

The repair imports the selected day's **hourly** curve and shifts later cumulative statistical sums to preserve subsequent
increments. It does not change current sensor totals, recovered-energy amounts,
or raw sensor state history. Independent anomalies on later days require their
own repair. Recorder must be enabled and all panels must have statistics; the POD
and aggregate API data are required. Pending recoveries must have been recorded
and compiled; unrelated unapplied recoveries must be resolved first. Local day
boundaries must align with UTC hours; ambiguous API timestamps are deferred.

The repair uses public `async_import_statistics` and
`get_instance(hass).async_adjust_statistics(statistic_id, start_time, delta, unit)`.
The latter adjusts existing hourly and five-minute sums at/after the boundary;
the next normal Recorder compilation continues from the corrected short-term sum.
It does not rebuild historical five-minute curves or create short-term buckets.
These are **separate commits**, not an atomic cross-table transaction: a partially
completed operation can be visible until journal reconciliation finishes.

Before queue submission the journal saves intent, original hourly values and
fixed hourly/five-minute delta witnesses. The integration drains Recorder and
reads persisted values after each public call. A restart/timeout resumes imports
only when rows match their original or intended values; a delta already reflected
by all witnesses is acknowledged without repetition. Submitted deltas with
unchanged, missing or conflicting witnesses are **not resubmitted**: the public
drain API cannot prove a task is no longer executing, and a crash can occur between
intent storage and queue submission. Retain the journal and backup; do not clear
storage to force retry. Concurrently appended statistics are allowed and receive
the queue-time adjustment; changes to the preview's existing rows are rejected.
This is conservative recovery, not a guarantee of automatic resolution of every
interruption. Other tools changing the same statistics concurrently are unsupported.

**Configure > Undo last statistics repair** restores existing hourly rows and
reverses verified offsets when the saved evidence remains consistent. It refuses
repairs that inserted any bucket: there is no public API to delete one imported
hourly bucket. Later appended statistics retain their increments. Applying a new
repair replaces the previous undo checkpoint. Legacy repair backups are preserved,
but their private-schema undo is no longer executable. An unresolved legacy journal
requires a backup-based/manual review rather than unsafe replay.

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

The tests use Home Assistant's Recorder models and real public import/adjust
implementations with in-memory SQLite to verify hourly correction, queue-time
advancement, compilation continuity, crash/timeout replay safety and undo limits.

## Version and changelog

The integration version is declared in
`custom_components/plenitude_panels/manifest.json` and is shown by Home
Assistant in the integration details. Changes for each release are recorded in
`custom_components/plenitude_panels/CHANGELOG.md`.
## Native cumulative restore checkpoints (0.11.2)

`Total Production` now uses `RestoreSensor` and custom `SensorExtraStoredData` to
persist native Wh, live Wh, the recovered Wh reference and the last accepted
sampling ID together. The checkpoint remains numeric when the displayed state
is unavailable and avoids restoring a converted/rounded presentation value.
It is validated for finite nonnegative values and total = live + recovered.
Only live energy is restored; the current durable recovery ledger is then added,
so new committed recoveries are retained without counting old ones twice.

Legacy numeric states remain supported in Wh, kWh and MWh. If checkpoint/state
restoration is unavailable, a public history read in Recorder's executor searches
the latest 1000 recorded raw states and their attributes for a valid baseline.
An older valid value outside that bound is not used. Normalized statistical sums
are never a restore source: repairs intentionally change those independently.
Existing entities with no recoverable baseline, database read failures or a
recovery ledger lower than the saved reference remain unavailable, skip sample
accumulation and log the problem rather than publishing an invented reset.
Restore consistent saved data and reload to resume them. Brand-new entities
with no saved state or recorded baseline initialize normally at zero.

The old generic-state path initialized live Wh to zero on an unavailable saved
state, leaving only recovered Wh. Real Recorder compilation reproduces both
symptoms: 105000 -> 54000 Wh is a >10% reset and counts +54000 Wh; 24500 -> 23000 Wh
is a smaller dip and counts -1500 Wh. Regression tests cover historical repair,
journal reconciliation after restart, legacy unavailable-state fallback, actual
five-minute sum compilation, hourly rollup, undo and a second restore. They also
cover native checkpoint round trips, unavailable/converted display states,
sample deduplication and additional committed recovery.

`live_energy_wh` is exposed for diagnostics. The fix does not rewrite earlier
state drops or infer a replacement current counter from historical maxima.
Repair every affected panel on the contaminated day after updating. The wizard
also suggests panels whose positive daily Wh exceed the entire API aggregate;
this is an advisory check, not a complete anomaly detector.

## Per-panel live production limits (0.11.0)

Each panel exposes configuration entities `switch` (Production limit) and
`number` (Maximum allowed power, W). Settings are stored per entry and supply,
loaded before the first coordinator refresh, and saved before publishing control
changes. The global `limit_new_panels` setting defaults to true and only seeds
unseen panel identifiers, including panels present at the first upgrade.

The initial maximum is twice the details endpoint's nominal `power` in kW,
converted to W (`0.9` or `0,9` gives 1800 W). Missing/zero/invalid nominal power
leaves the number unknown; the switch's `threshold_required` attribute is true
and no cap applies until a positive threshold is entered. Control changes apply
to future API samples, without reprocessing the current sample.

Before recovery storage and sensor publication, `instantProductionWh` values
above an enabled threshold are capped and logged. This protects average power,
five-minute energy (power / 12), cumulative energy and stored live recovery
samples consistently. Existing history, statistics and aggregate recovery
allocations are not rewritten or capped by this live-only check.
