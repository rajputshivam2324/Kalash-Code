import re

with open("src/kalash/mcp/client.py") as f:
    code = f.read()

# Fix auth in _send_http
code = code.replace(
    "        from kalash.mcp.auth import MCPAuth",
    """        from kalash.mcp.auth import MCPAuth
        auth_headers = MCPAuth.get_headers(self._config.name)
        if auth_headers:
            headers.update(auth_headers)"""
)

with open("src/kalash/mcp/client.py", "w") as f:
    f.write(code)
