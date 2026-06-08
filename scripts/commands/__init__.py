"""BuildSession command clusters.

build.py's BuildSession was a single ~11k-line god-class.  Its command
handlers are split here by function — one ``*CommandsMixin`` per cluster,
each in its own module.  ``BuildSession`` in build.py inherits every
mixin; Python's MRO assembles the methods onto one instance, so behaviour
is identical to the monolith (the test suite is the proof).

Each mixin inherits :class:`commands.base.SessionState`, which declares —
for the type checker only — the instance attributes that
``BuildSession.__init__`` populates.  See base.py for the rationale.
"""
