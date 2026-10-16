"""Run inside the API image: SDK persistence smoke, without model/business calls."""

import asyncio
import json
import uuid
from typing import TypedDict

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo

from app.core.config import get_settings


class State(TypedDict):
    count: int


def graph(saver):
    builder = StateGraph(State)
    builder.add_node("increment", lambda state: {"count": state["count"] + 1})
    builder.add_edge(START, "increment")
    builder.add_edge("increment", END)
    return builder.compile(checkpointer=saver, interrupt_before=["increment"])


async def main():
    settings = get_settings()
    conninfo = make_conninfo(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        dbname=settings.postgres_db,
        password=settings.postgres_password.get_secret_value(),
    )
    config = {"configurable": {"thread_id": "infra-probe-" + uuid.uuid4().hex}}
    async with AsyncPostgresSaver.from_conn_string(conninfo) as saver:
        await saver.setup()
        first = await graph(saver).ainvoke({"count": 0}, config)
        assert first["count"] == 0
    # A new connection and new graph instance must resume the persisted checkpoint.
    async with AsyncPostgresSaver.from_conn_string(conninfo) as saver:
        resumed = await graph(saver).ainvoke(None, config)
        assert resumed["count"] == 1
        snapshot = await graph(saver).aget_state(config)
        assert not snapshot.next
    async with await AsyncConnection.connect(conninfo) as connection:
        cursor = await connection.execute("SELECT '[1,0]'::vector <=> '[1,0]'::vector")
        assert (await cursor.fetchone())[0] == 0
    print(json.dumps({"checkpoint_reconnect_resume": "passed", "pgvector_cosine": "passed"}))


if __name__ == "__main__":
    asyncio.run(main())
