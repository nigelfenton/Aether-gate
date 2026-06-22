# attic — roads not taken

Kept for the record, not for building. Nothing here is on the roadmap.

## kiwi/

The KiwiSDR adapter stub + its original design, relocated out of flex-sim on
2026-06-21 when Aether-gate was created to keep the KiwiSDR work private.

**Superseded the same day.** AetherSDR merged *native* KiwiSDR receive support
upstream (rfoust, #3668, June 2026) — in-process, with direct access to AE's own
DSP, which an external re-FFT bridge can never match. The reason this code was kept
private (don't tip our hand on a novel KiwiSDR bridge) became moot the moment AE
shipped it natively.

It is parked here, not deleted, because the *thinking* was sound and the
IQ → VITA-49 mechanics are reusable reference for the SoapySDR adapter, which is the
live first target. The KiwiSDR adapter itself is **not** something to resurrect —
AE already reaches KiwiSDR better than the gate would.

See the top-level DESIGN.md "Scope" and "Rationale" sections for the live direction.
