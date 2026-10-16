import asyncio
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import Field

from app.agent.contracts import StrictModel
from app.context.contracts import DocumentInput, PreferenceInput, SearchInput, parse_upload
from app.context.memory import MemoryService
from app.context.retrieval import KnowledgeService
from app.context.workflows import WorkflowService
from app.core.auth import Principal, principal
from app.core.uploads import iter_upload_chunks

router = APIRouter()
Actor = Annotated[Principal, Depends(principal)]


def knowledge(request):
    return KnowledgeService(request.app.state.settings, request.app.state.resources.engine)


def memories(request):
    return MemoryService(request.app.state.settings, request.app.state.resources.engine)


def workflows(request):
    return WorkflowService(request.app.state.settings, request.app.state.resources.engine)


class Version(StrictModel):
    expected_version: int = Field(ge=1)


class Replacement(Version):
    document: DocumentInput


class MemorySettings(StrictModel):
    automatic_memory_enabled: bool


class Candidate(StrictModel):
    source_run_id: UUID = Field(strict=False)


class WorkflowAction(StrictModel):
    action: Literal["validate", "approve", "disable"]


@router.post("/knowledge/documents", status_code=202)
async def create_document(body: DocumentInput, request: Request, actor: Actor):
    return await knowledge(request).create(actor, body)


@router.post("/knowledge/upload", status_code=202)
async def upload_document(
    request: Request,
    actor: Actor,
    file: Annotated[UploadFile, File()],
    title: Annotated[str, Form(min_length=1, max_length=200)],
    scope: Annotated[Literal["private", "tenant"], Form()] = "private",
):
    # Reject unauthorized publishing before reading multipart file content.
    if scope == "tenant" and actor.role not in {"support", "supervisor"}:
        raise HTTPException(403, "tenant_publisher_role_required")
    try:
        raw = bytearray()
        async for chunk in iter_upload_chunks(
            file, request.app.state.settings.upload_max_file_bytes
        ):
            raw.extend(chunk)
            if len(raw) > 4_000_003:
                raise HTTPException(413, "parsed_document_text_limit")
        try:
            content = await asyncio.to_thread(parse_upload, bytes(raw), file.filename)
            body = DocumentInput(title=title, text=content, scope=scope)
        except ValueError:
            # ValidationError.__str__ includes the original document/title; never echo it.
            raise HTTPException(422, "invalid_document_upload") from None
        # Filename is metadata only, never used as a filesystem path.
        return await knowledge(request).create(actor, body, (file.filename or "upload")[:200])
    finally:
        await file.close()


@router.get("/knowledge/documents")
async def list_documents(request: Request, actor: Actor):
    return await knowledge(request).list(actor)


@router.post("/knowledge/search")
async def search(body: SearchInput, request: Request, actor: Actor):
    return await knowledge(request).search(actor, body.query.strip(), body.limit)


@router.get("/knowledge/evidence/{chunk_id}")
async def evidence(chunk_id: UUID, request: Request, actor: Actor):
    return await knowledge(request).evidence(actor, chunk_id)


@router.put("/knowledge/documents/{document_id}")
async def replace(document_id: UUID, body: Replacement, request: Request, actor: Actor):
    return await knowledge(request).mutate(
        actor, document_id, "replace", body.expected_version, body.document
    )


@router.post("/knowledge/documents/{document_id}/approve")
async def approve(document_id: UUID, body: Version, request: Request, actor: Actor):
    return await knowledge(request).mutate(actor, document_id, "approve", body.expected_version)


@router.post("/knowledge/documents/{document_id}/retry", status_code=202)
async def retry(document_id: UUID, body: Version, request: Request, actor: Actor):
    return await knowledge(request).mutate(actor, document_id, "retry", body.expected_version)


@router.delete("/knowledge/documents/{document_id}")
async def delete_document(document_id: UUID, body: Version, request: Request, actor: Actor):
    return await knowledge(request).mutate(actor, document_id, "delete", body.expected_version)


@router.get("/memories")
async def list_memories(request: Request, actor: Actor):
    return await memories(request).list(actor)


@router.put("/memories/preferences")
async def preference(body: PreferenceInput, request: Request, actor: Actor):
    return await memories(request).preference(actor, body)


@router.put("/memories/settings")
async def memory_settings(body: MemorySettings, request: Request, actor: Actor):
    return await memories(request).settings_update(actor, body.automatic_memory_enabled)


@router.delete("/memories/{memory_id}")
async def delete_memory(memory_id: UUID, request: Request, actor: Actor):
    return await memories(request).delete(actor, memory_id)


@router.delete("/memories")
async def forget(request: Request, actor: Actor):
    return await memories(request).delete(actor)


@router.post("/workflows/candidates", status_code=201)
async def candidate(body: Candidate, request: Request, actor: Actor):
    return await workflows(request).candidate(actor, body.source_run_id)


@router.get("/workflows")
async def list_workflows(request: Request, actor: Actor):
    return await workflows(request).list(actor)


@router.post("/workflows/{workflow_id}/review")
async def review(workflow_id: UUID, body: WorkflowAction, request: Request, actor: Actor):
    return await workflows(request).review(actor, workflow_id, body.action)


@router.get("/workflows/{workflow_id}/skill")
async def export_skill(workflow_id: UUID, request: Request, actor: Actor):
    return await workflows(request).export(actor, workflow_id)
