"""LensPilot tools.

INTEGRATION BASELINE — owner: Khalil (backend tools / publication).
Each handler takes (ToolContext, validated input model) and returns a
ToolResult. Expected failures raise SandboxError (mapped to a ToolResult by
the registry); handlers never return raw tracebacks.

READ tools : list_photos, get_photo_metadata, evaluate_photo_quality, read_style_guide
PROPOSE    : prepare_publication  (no side effect; triggers the human checkpoint)
WRITE      : publish_photo        (requires a human-minted approval token)
"""

from __future__ import annotations

from datetime import datetime, timezone

from security import check_proposal, check_publication_eligibility, wrap_untrusted
from tools.schemas import (
    EmptyInput,
    ListPhotosInput,
    PhotoIdsInput,
    PreparePublicationInput,
    PublishInput,
    ToolContext,
    ToolResult,
    compute_proposal_hash,
    idempotency_key,
)
from sandbox import SandboxError

PUBLIC_FIELDS = (
    "photo_id", "title", "category", "shoot", "captured_at", "camera", "lens",
    "focal_length_mm", "aperture", "iso", "shutter", "visibility",
    "client_consent", "consent_simulated",
)


def list_photos(ctx: ToolContext, args: ListPhotosInput) -> ToolResult:
    photos = [
        {"photo_id": p["photo_id"], "title": p["title"], "category": p["category"], "shoot": p["shoot"]}
        for p in ctx.sandbox.catalog()
        if args.category is None or p["category"] == args.category
    ]
    if not photos:
        return ToolResult.fail("empty_result", f"No photos found for category {args.category!r}.")
    return ToolResult.ok({"count": len(photos), "photos": photos})


def get_photo_metadata(ctx: ToolContext, args: PhotoIdsInput) -> ToolResult:
    found, missing = [], []
    for pid in args.photo_ids:
        try:
            p = ctx.sandbox.get_photo(pid)
        except SandboxError as exc:
            if exc.code != "photo_not_found":
                raise
            missing.append(pid)
            continue
        meta = {k: p.get(k) for k in PUBLIC_FIELDS}
        meta["description"] = wrap_untrusted(p.get("description", ""))  # untrusted free text
        meta["publication_eligibility"] = check_publication_eligibility(p).__dict__
        found.append(meta)
    if not found:
        return ToolResult.fail("photo_not_found", f"None of the requested photos exist: {missing}")
    return ToolResult.ok({"photos": found, "missing": missing})


def evaluate_photo_quality(ctx: ToolContext, args: PhotoIdsInput) -> ToolResult:
    results = []
    for pid in args.photo_ids:
        assessment = ctx.quality.assess(ctx.sandbox.image_path(pid))
        results.append({"photo_id": pid, **assessment})
    return ToolResult.ok({"assessments": results})


def read_style_guide(ctx: ToolContext, args: EmptyInput) -> ToolResult:
    return ToolResult.ok({"style_guide": wrap_untrusted(ctx.sandbox.style_guide())})


def prepare_publication(ctx: ToolContext, args: PreparePublicationInput) -> ToolResult:
    """Validate a proposal and compute its hash. Writes NOTHING."""
    raw = [i.model_dump() for i in args.items]
    # Photo eligibility + caption guard (security/policy.py, owner: Skander).
    blocked = check_proposal(raw, ctx.sandbox.get_photo)
    if blocked:
        return ToolResult.fail(
            "policy_denied",
            "Publication policy forbids: "
            + "; ".join(f"{b['photo_id']}: {', '.join(b['reasons'])}" for b in blocked)
            + ". Remove these photos or fix their captions, then prepare a new proposal.",
        )
    items = [{"photo_id": i.photo_id, "caption": i.caption, "title": ctx.sandbox.get_photo(i.photo_id)["title"]}
             for i in args.items]
    proposal_hash = compute_proposal_hash(args.collection, raw)
    return ToolResult.ok(
        {
            "proposal_id": proposal_hash[:12],
            "proposal_hash": proposal_hash,
            "collection": args.collection,
            "destination": f"sandbox://published/{args.collection}",
            "items": items,
            "status": "awaiting_human_approval",
        }
    )


def publish_photo(ctx: ToolContext, args: PublishInput) -> ToolResult:
    """The only write. Re-checks everything; idempotent per (collection, photo)."""
    raw = [i.model_dump() for i in args.items]
    if compute_proposal_hash(args.collection, raw) != args.proposal_hash:
        return ToolResult.fail("approval_invalid", "Proposal content does not match the approved hash.")
    if not ctx.approvals.verify(args.approval_token, args.run_id, args.proposal_hash):
        return ToolResult.fail("approval_invalid", "Missing or invalid human approval token.")
    # Defence in depth: eligibility and captions re-checked at write time.
    blocked = check_proposal(raw, ctx.sandbox.get_photo)
    if blocked:
        return ToolResult.fail("policy_denied", "; ".join(f"{b['photo_id']}: {', '.join(b['reasons'])}"
                                                          for b in blocked))
    published = []
    for item in args.items:
        key = idempotency_key(args.collection, item.photo_id)
        photo = ctx.sandbox.get_photo(item.photo_id)
        record = {
            "publication_key": key,
            "photo_id": item.photo_id,
            "title": photo["title"],
            "category": photo["category"],
            "caption": item.caption,
            "collection": args.collection,
            "run_id": args.run_id,
            "proposal_hash": args.proposal_hash,
            "approved_by": "human",
            "published_at": datetime.now(timezone.utc).isoformat(),
        }
        stored, created = ctx.sandbox.write_publication(key, record, ctx.sandbox.image_path(item.photo_id))
        published.append({"photo_id": item.photo_id, "publication_key": key, "created": created,
                          "already_published": not created, "caption": stored["caption"]})
    return ToolResult.ok({"collection": args.collection, "published": published})
