from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from app.context.conversation import ConversationService
from app.core.auth import identity

router = APIRouter()
Owner = Annotated[str, Depends(identity)]


@router.post("/conversations", status_code=201)
async def create(request: Request, owner: Owner):
    return await ConversationService(request.app.state.resources.engine).create(owner)


@router.get("/conversations/{conversation_id}")
async def get(conversation_id: UUID, request: Request, owner: Owner):
    return {
        "id": str(conversation_id),
        "turns": await ConversationService(request.app.state.resources.engine).turns(
            owner, conversation_id
        ),
    }


@router.delete("/conversations/{conversation_id}")
async def delete(conversation_id: UUID, request: Request, owner: Owner):
    return await ConversationService(request.app.state.resources.engine).delete(
        owner, conversation_id
    )
