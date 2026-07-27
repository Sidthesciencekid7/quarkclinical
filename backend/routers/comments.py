"""Section review comments — persistent, org-shared, user-attributed.

The editor's review notes used to live in the browser's localStorage: invisible
to teammates, lost on cache clear, attributed to a hardcoded "Reviewer". These
endpoints back them with the `comments` table instead.

AUTHORIZATION: every route resolves the verified actor and then fetches the
parent protocol through get_protocol_scoped, so a comment on another org's
protocol is a 403 (and an unknown protocol a 404) — the same isolation rule the
protocol routes themselves use. Deliberately NOT role-gated: reviewers
(read-only for protocol content) must still be able to leave notes; commenting
is the whole point of that role.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_user
from database import get_db
from dependencies import (
    audit_logger,
    authenticate_actor as _authenticate_actor,
    get_protocol_scoped,
)
from models import Comment
from schemas import CommentCreate, CommentRead, CommentUpdate

router = APIRouter(
    prefix="/api/protocols/{protocol_id}/comments",
    tags=["comments"],
    dependencies=[Depends(audit_logger)],
)


@router.get("", response_model=list[CommentRead], dependencies=[Depends(require_user)])
async def list_comments(
    protocol_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """All notes on this protocol, newest first (the panel filters by section
    client-side so switching sections needs no extra round-trip)."""
    actor = await _authenticate_actor(request, db)
    await get_protocol_scoped(db, protocol_id, actor)
    result = await db.execute(
        select(Comment)
        .where(Comment.protocol_id == protocol_id)
        .order_by(Comment.created_at.desc())
    )
    return result.scalars().all()


@router.post("", response_model=CommentRead, status_code=201, dependencies=[Depends(require_user)])
async def create_comment(
    protocol_id: uuid.UUID,
    payload: CommentCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Attribute the note to the VERIFIED actor — never to a client-supplied
    name — so the review trail can't be forged."""
    actor = await _authenticate_actor(request, db)
    await get_protocol_scoped(db, protocol_id, actor)

    text = payload.text.strip()
    if not text:
        raise HTTPException(422, detail="Comment text cannot be empty.")

    comment = Comment(
        id=uuid.uuid4(),
        protocol_id=protocol_id,
        section_name=payload.section_name.strip(),
        text=text,
        author_id=str(actor.id),
        author_name=actor.email,
        resolved=False,
    )
    db.add(comment)
    await db.commit()
    await db.refresh(comment)
    request.state.audit_resource_id = str(comment.id)
    return comment


@router.patch("/{comment_id}", response_model=CommentRead, dependencies=[Depends(require_user)])
async def update_comment(
    protocol_id: uuid.UUID,
    comment_id: uuid.UUID,
    payload: CommentUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Resolve / reopen. Any member of the owning org may do this — review
    notes are a shared workspace, not private to their author."""
    actor = await _authenticate_actor(request, db)
    await get_protocol_scoped(db, protocol_id, actor)

    comment = await _get_comment_or_404(db, protocol_id, comment_id)
    comment.resolved = payload.resolved
    await db.commit()
    await db.refresh(comment)
    return comment


@router.delete("/{comment_id}", status_code=204, dependencies=[Depends(require_user)])
async def delete_comment(
    protocol_id: uuid.UUID,
    comment_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    actor = await _authenticate_actor(request, db)
    await get_protocol_scoped(db, protocol_id, actor)

    comment = await _get_comment_or_404(db, protocol_id, comment_id)
    await db.delete(comment)
    await db.commit()
    request.state.audit_resource_id = str(comment_id)


async def _get_comment_or_404(
    db: AsyncSession, protocol_id: uuid.UUID, comment_id: uuid.UUID
) -> Comment:
    """Fetch scoped to the parent protocol so a comment id from another
    protocol (possibly another org's) can't be reached through this one."""
    result = await db.execute(
        select(Comment).where(Comment.id == comment_id, Comment.protocol_id == protocol_id)
    )
    comment = result.scalar_one_or_none()
    if not comment:
        raise HTTPException(404, detail="Comment not found.")
    return comment
