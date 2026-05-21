import asyncio
import os
from dataclasses import dataclass
from typing import Any, Callable, Awaitable

from src.protocol import Request


@dataclass
class PendingItem:
    request: Request
    writer: asyncio.StreamWriter | None


class RequestQueue:
    def __init__(self):
        self.pending: list[PendingItem] = []

    def add(self, request: Request, writer: asyncio.StreamWriter | None) -> None:
        self.pending.append(PendingItem(request=request, writer=writer))

    def remove(self, request_id: str) -> None:
        self.pending = [p for p in self.pending if p.request.id != request_id]

    def get(self, request_id: str) -> PendingItem | None:
        for item in self.pending:
            if item.request.id == request_id:
                return item
        return None


OnRequestCallback = Callable[[Request, asyncio.StreamWriter], Awaitable[None]]


async def serve_unix_socket(
    path: str,
    queue: RequestQueue,
    on_request: OnRequestCallback,
) -> asyncio.AbstractServer:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            data = await reader.readline()
            if data:
                request = Request.from_json(data)
                queue.add(request, writer)
                await on_request(request, writer)
        except Exception:
            try:
                writer.close()
            except Exception:
                pass

    if os.path.exists(path):
        os.unlink(path)

    return await asyncio.start_unix_server(handle, path=path)
