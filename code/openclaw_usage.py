"""Compatibility shim — old name for openclaw_cost_tracker.

Per Arcurus 2026-06-07 #cost-tracker, the rename is to make the two
trackers' roles clearer:
  - app_llm_cost_tracker.py = our apps call /api/llm-usage/record
  - openclaw_cost_tracker.py = parses open-claw session transcripts

The shim re-exports the underscore-prefixed names explicitly because
`from X import *` skips names starting with an underscore unless
`__all__` is defined. Several legacy callers (api_server.py,
cost_tracker.py) still use `openclaw_usage._iter_events` etc.
"""
import warnings as _warnings
_warnings.warn(
    "openclaw_usage is deprecated; use openclaw_cost_tracker instead",
    DeprecationWarning,
    stacklevel=2,
)
import openclaw_cost_tracker as _oct
# Re-export the underscore-prefixed names so legacy callers
# (`openclaw_usage._iter_events`, `_filter_events`, `_build_stats`,
# `_bucketize`, `_project_label_for_event`, etc.) keep working
# after the 2026-06-07 rename. `from X import *` would skip them
# because Python convention hides underscore names from star imports.
for _name in (
    '_log', '_load_state', '_save_state', '_append_event',
    '_load_sessions_index', '_infer_from_key', '_infer_kind_from_path',
    '_parse_session_transcript', '_provider_for_model',
    '_iter_session_files', '_parse_trajectory_metadata',
    '_process_one', '_iter_events', '_filter_events', '_build_stats',
    '_bucketize', '_project_label_for_event',
):
    if hasattr(_oct, _name):
        globals()[_name] = getattr(_oct, _name)
# Re-export the public names too (would happen via `import *` for
# non-underscore names, but doing it explicitly keeps behavior stable
# regardless of __all__).
for _name in dir(_oct):
    if not _name.startswith('_'):
        globals().setdefault(_name, getattr(_oct, _name))


# ---------------------------------------------------------------------------
# Loud-failure guard (added 2026-06-11 per Arcurus #cost-tracker)
# ---------------------------------------------------------------------------
# History: on 2026-06-07 this file became a pure re-export shim as part of
# the openclaw_cost_tracker rename. The systemd timer
# `openclaw-usage-track.service` kept calling `python3 code/openclaw_usage.py
# sync`, which silently exited 0 without doing any work. The .jsonl log
# stopped writing; the cost tracker read fewer openclaw sessions; the web
# UI's per-model/timeseries charts undercounted spend by 22 hours before
# anyone noticed.
#
# Fix: if this module is invoked as a script (not imported), refuse to be
# a no-op. Callers that want the legacy entry point should use
# `python3 code/openclaw_cost_tracker.py sync` instead. Importing this
# module for its re-exports is still safe and side-effect-free.
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    import sys as _sys
    _sys.stderr.write(
        "openclaw_usage.py is a compatibility shim and no longer supports "
        "CLI invocation. Use `python3 code/openclaw_cost_tracker.py <cmd>` "
        "instead (e.g. `sync`, `backfill`, `status`, `report`, "
        "`timeseries`). See the docstring at the top of this file for "
        "the rename history.\n"
    )
    _sys.exit(2)
