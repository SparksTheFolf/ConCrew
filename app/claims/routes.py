from datetime import datetime

from flask import Blueprint, redirect, url_for, flash, request
from flask_login import login_required, current_user

from app.extensions import db
from app.models import RewardClaim, ClaimStatus, Role, AuditLog
from app.utils import roles_required

claims_bp = Blueprint("claims", __name__)


@claims_bp.route("/<int:claim_id>/fulfill", methods=["POST"])
@login_required
@roles_required(*Role.CAN_FULFILL)  # admin, assigner, or constore -- one signoff is enough
def fulfill(claim_id):
    claim = RewardClaim.query.get_or_404(claim_id)

    if claim.status == ClaimStatus.FULFILLED:
        flash("That item was already marked fulfilled.", "warning")
        return redirect(request.referrer or url_for("volunteer.dashboard"))

    claim.status = ClaimStatus.FULFILLED
    claim.fulfilled_by_id = current_user.id
    claim.fulfilled_at = datetime.utcnow()

    db.session.add(AuditLog(
        actor_id=current_user.id,
        action="fulfill_claim",
        target_type="RewardClaim",
        target_id=claim.id,
        detail=f"item={claim.reward_item.name} volunteer={claim.volunteer.badge_name}",
    ))
    db.session.commit()

    flash(f"Marked '{claim.reward_item.name}' fulfilled for {claim.volunteer.badge_name}.", "success")
    return redirect(request.referrer or url_for("admin.claims_matrix"))


@claims_bp.route("/<int:claim_id>/unfulfill", methods=["POST"])
@login_required
@roles_required(Role.ADMIN)  # undo requires admin -- prevents accidental/malicious un-signing
def unfulfill(claim_id):
    claim = RewardClaim.query.get_or_404(claim_id)
    claim.status = ClaimStatus.ELIGIBLE
    claim.fulfilled_by_id = None
    claim.fulfilled_at = None

    db.session.add(AuditLog(
        actor_id=current_user.id,
        action="unfulfill_claim",
        target_type="RewardClaim",
        target_id=claim.id,
        detail=f"item={claim.reward_item.name} volunteer={claim.volunteer.badge_name}",
    ))
    db.session.commit()

    flash("Reverted claim to eligible.", "info")
    return redirect(request.referrer or url_for("admin.claims_matrix"))
