"""Thin wrappers where upstream exposes public functions but no CLI.

Each wrapper imports the INSTALLED upstream packages and calls their public API.
None of them modifies upstream. See UPSTREAM_WISHLIST.md for what would remove them.
"""
