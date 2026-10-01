"""SIMULATION mode: the production control loop over a synthetic, in-memory world.

Everything here is isolated by construction. This package must never import anything that can
reach Docker, NVIDIA, a subprocess or the network (an AST test enforces that), it never receives a
real provider or a real telemetry source, and its engine, store and audit chain are stamped
SIMULATION so a simulated outcome can never be read as a real one.
"""
