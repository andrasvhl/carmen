"""Canvas LMS archive: pull a course into the vault, verbatim.

Split into modules because this is the largest tool in the repo and the halves
have different dependency stories: `api` and `render` are stdlib-only so a pull
always runs, `extract` needs third-party parsers and degrades to a clear message
when they are missing.
"""
