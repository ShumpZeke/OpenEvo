<!--
Read CLAUDE.md before your first change here. The rules below are load-bearing
and each one exists because breaking it cost someone real time.
-->

## What this changes

<!-- One paragraph. Say which of the two paths it touches: the shipping path
     (upstream engine + OE-MAX broker) or the BrainPort path, or neither. -->

## Evidence

<!-- If this claims a behavioural improvement, put the numbers here. "It should
     be faster" is not evidence; a run against a real provider is. If you did
     not measure it, say that plainly -- an honest "unverified" is fine and a
     fabricated result is not. -->

## Checklist

- [ ] `./test.sh` (or `.\test.ps1`) run, and the upstream suite is still green.
      Six upstream tests fail on Windows for platform reasons documented in
      `TEST_STRATEGY.md`; those are expected.
- [ ] `openevolve/` is untouched. Behaviour is added by wrapping public methods
      at runtime — `control_plane/telemetry/instrument.py` is the pattern. If it
      genuinely had to be edited, `PATCH_SURFACE.md` records the reason.
- [ ] No fixtures or placeholder numbers in `web/`. Where the backend has no
      value the UI renders "no data".
- [ ] Unsupported controls are disabled with a reason rather than rendered as
      buttons that do nothing.
- [ ] No credentials leave the broker process, and redaction still runs before
      persistence rather than at render time.
- [ ] `REQUIREMENTS_PROGRESS.md` updated if a status changed, and `DECISIONS.md`
      updated if this involved a judgement call worth defending later.
