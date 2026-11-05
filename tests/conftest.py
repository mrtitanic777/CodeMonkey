"""Shared test setup.

Every test that builds the web app would otherwise start the MCP servers in the real mcp.json, and
those only close when the whole run ends: a full run left a hundred of them running. Tests that need
an MCP server make their own hub and start it themselves.
"""

import os

os.environ["CODEMONKEY_NO_MCP"] = "1"
