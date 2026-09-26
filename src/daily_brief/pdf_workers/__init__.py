"""PDF extraction workers launched as `python -m` subprocesses.

Keep this package free of imports: each worker starts in a fresh, memory-limited
process, and anything imported here is loaded before every PDF is parsed.
"""
