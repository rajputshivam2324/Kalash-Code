import re

with open("src/kalash/mcp/client.py") as f:
    code = f.read()

sse_connect_code = """    async def _connect_sse(self) -> None:
        \"\"\"Connect via legacy SSE transport.\"\"\"
        if not self._config.url:
            raise MCPConnectionError(
                f"SSE transport requires a URL for server '{self._config.name}'",
                recoverable=True,
            )
        import httpx
        from kalash.mcp.auth import MCPAuth
        self._sse_client = httpx.AsyncClient(timeout=self._config.timeout_s)
        self._sse_endpoint = self._config.url
        self._post_endpoint = self._config.url
        
        self._reader_task = asyncio.create_task(self._sse_read_loop())
        self._connected = True

    async def _sse_read_loop(self) -> None:
        import httpx
        import json
        from kalash.mcp.auth import MCPAuth
        
        while True:
            try:
                headers = {"Accept": "text/event-stream"}
                auth_headers = MCPAuth.get_headers(self._config.name)
                if auth_headers:
                    headers.update(auth_headers)
                    
                async with self._sse_client.stream("GET", self._sse_endpoint, headers=headers) as response:
                    response.raise_for_status()
                    event_type = "message"
                    buffer = []
                    
                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line:
                            if buffer:
                                data_str = "\\n".join(buffer)
                                buffer = []
                                
                                if event_type == "endpoint":
                                    from urllib.parse import urljoin
                                    # Post endpoint is relative to sse_endpoint or absolute
                                    self._post_endpoint = urljoin(self._sse_endpoint, data_str)
                                elif event_type == "message":
                                    try:
                                        msg = json.loads(data_str)
                                        req_id = msg.get("id")
                                        if req_id and req_id in self._pending:
                                            future = self._pending.pop(req_id)
                                            if "error" in msg:
                                                future.set_exception(MCPSchemaError(str(msg["error"]), recoverable=True))
                                            else:
                                                future.set_result(msg.get("result"))
                                    except json.JSONDecodeError:
                                        pass
                            event_type = "message"
                            continue
                            
                        if line.startswith("event:"):
                            event_type = line[6:].strip()
                        elif line.startswith("data:"):
                            buffer.append(line[5:].strip())
                            
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(2.0)

    async def _send_sse(self, message: dict[str, Any], req_id: int) -> Any:
        import httpx
        import json
        from kalash.mcp.auth import MCPAuth
        
        if not self._sse_client:
            raise MCPConnectionError("Not connected via SSE", recoverable=True)
            
        future: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future
        
        headers = {"Content-Type": "application/json"}
        auth_headers = MCPAuth.get_headers(self._config.name)
        if auth_headers:
            headers.update(auth_headers)
            
        try:
            response = await self._sse_client.post(self._post_endpoint, json=message, headers=headers)
            response.raise_for_status()
            
            result = await asyncio.wait_for(future, timeout=self._config.timeout_s)
            return result
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise ToolTimeoutError(
                f"MCP request to '{self._config.name}' timed out after {self._config.timeout_s}s",
                recoverable=True,
            )
        except Exception as e:
            self._pending.pop(req_id, None)
            raise MCPConnectionError(f"Failed to send SSE POST: {e}", recoverable=True)
"""

# Replace the old _connect_sse
old_connect = """    async def _connect_sse(self) -> None:
        \"\"\"Connect via legacy SSE transport.\"\"\"
        if not self._config.url:
            raise MCPConnectionError(
                f"SSE transport requires a URL for server '{self._config.name}'",
                recoverable=True,
            )
        # Legacy SSE: establish event stream connection
        self._connected = True"""

if old_connect in code:
    code = code.replace(old_connect, sse_connect_code)
else:
    print("Could not find old _connect_sse")

# Wire it up in _send_request
old_send_request = """        if self._config.transport == TransportType.STDIO:
            return await self._send_stdio(message, req_id)
        elif self._config.transport == TransportType.HTTP:
            return await self._send_http(message)
        else:
            return await self._send_http(message)"""

new_send_request = """        if self._config.transport == TransportType.STDIO:
            return await self._send_stdio(message, req_id)
        elif self._config.transport == TransportType.HTTP:
            return await self._send_http(message)
        else:
            return await self._send_sse(message, req_id)"""

code = code.replace(old_send_request, new_send_request)

with open("src/kalash/mcp/client.py", "w") as f:
    f.write(code)

