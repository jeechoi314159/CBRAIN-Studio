"""
CBRAIN Studio — next-gen desktop platform for wireless neural acquisition.

Layered architecture (see docs/architecture.md):
  L1 hal/     — Hardware Abstraction Layer (Transport/Device/Led/Sync), the only door to hardware.
  L2 core/    — hardware-agnostic services (types, CB v2 codec, sample bus, ring buffer, time base).
  L3 app/     — domain managers (acquisition, recording, …).
  L4 ui/      — presentation (added in a later phase).
"""

__version__ = "0.1.0-dev"
