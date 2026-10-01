"""Guarded real fault injection: pause the managed workload, with a lease that always ends it.

This package is operator TESTING, not part of normal operation: nothing here runs unless an
operator explicitly confirms it, and the control plane never injects a fault on its own. It is
deliberately separate from `DockerProvider` (observation and approved remediation): it has its own
container runner that allows four argv shapes and nothing else.
"""
