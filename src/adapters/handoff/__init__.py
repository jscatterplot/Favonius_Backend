"""Inter-depot vehicle handoff management.

Reference: PRD_v2.md Section 5.4 (Inter-Depot Handoff Flow)
"""

from .manager import HandoffManager, HandoffMessage

__all__ = ['HandoffManager', 'HandoffMessage']
