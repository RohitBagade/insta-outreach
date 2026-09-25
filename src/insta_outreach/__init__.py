"""LemmeDeliver Instagram outreach orchestrator.

Hybrid execution: official Meta APIs where they expose a capability reliably,
a Playwright browser agent where they do not. All business decisions (who to
contact, what to send, when) live in the orchestrator; executors only carry
out approved, bounded operations and report structured results.
"""

__version__ = "0.1.0"
