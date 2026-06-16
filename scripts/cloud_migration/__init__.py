"""Cloud-namespace migration core for screencap (SCR-139 / plan U8–U9).

The three operator scripts at ``scripts/migrate_flat_to_staging.py``,
``scripts/promote_staging_to_demo.py`` and
``scripts/decommission_flat_namespace.py`` are thin argparse shims over the
``run_*`` orchestration functions in :mod:`scripts.cloud_migration.core`.

Migration shape (all within the one shared recordings bucket)::

    recordings/ , sessions/   (legacy flat, public-listable)
        |  U8 stage (rewrite, no deletes)
        v
    import-review/            (PRIVATE staging — no public handler)
        |  U8 promote (founder allow-list, markers stripped)
        v
    demo/                     (public gallery, served by the demo-* function)

    recordings/ , sessions/   removed LAST (U9), after U7 is verified live.
"""
