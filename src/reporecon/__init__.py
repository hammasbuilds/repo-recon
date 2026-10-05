"""Read an unfamiliar Python codebase: structure, risk, and what a change would mean.

Four tools that were four separate packages, behind one MCP server. Each answers a
question an agent has when it lands in a repository it did not write:

``cartographer``  what is in here, and what depends on what
``pilot``         what can be modernised, and where that would change meaning
``captain``       is this diff safe to ship, scored from the history rather than opinion
``reviewbot``     which review findings survive an attempt to disprove them

No dependencies, nothing written, nothing executed. Every tool reads source and
returns what it read.
"""

__version__ = "0.1.0"
