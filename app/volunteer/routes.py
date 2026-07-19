from datetime import datetime

from flask import Blueprint, render_template
from flask_login import login_required, current_user

from app.models import RewardTier, HourEntry, ShiftRequestStatus, ShiftRequest, ShiftSwapRequest, ShiftSwapStatus
from app.utils import get_current_con_year

volunteer_bp = Blueprint("volunteer", __name__)


@volunteer_bp.route("/")
@login_required
def dashboard():
    con_year = get_current_con_year()
    tiers = RewardTier.query.order_by(RewardTier.threshold_hours).all()

    hours = current_user.approved_hours(con_year.id) if con_year else 0
    current_tier = current_user.current_tier(con_year.id) if con_year else None
    next_tier = current_user.next_tier(con_year.id) if con_year else None
    remaining = round(next_tier.threshold_hours - hours, 2) if next_tier else 0

    my_entries = []
    if con_year:
        my_entries = (
            HourEntry.query.filter_by(volunteer_id=current_user.id, con_year_id=con_year.id)
            .order_by(HourEntry.work_date.desc())
            .all()
        )

    my_claims = {c.reward_item_id: c for c in current_user.claims if con_year and c.con_year_id == con_year.id}

    now = datetime.now()

    pending_requests = [r for r in current_user.shift_requests if r.status == ShiftRequestStatus.REQUESTED]
    confirmed_requests = [
        r for r in current_user.shift_requests
        if r.status in (
            ShiftRequestStatus.APPROVED,
            ShiftRequestStatus.IN_PROGRESS,
            ShiftRequestStatus.LEAVE_REQUESTED,
            ShiftRequestStatus.LEAVE_DENIED,
        )
        and r.scheduled_end() > now
    ]
    pending_requests.sort(key=lambda r: (r.shift_slot.shift_date, r.shift_slot.start_time))
    confirmed_requests.sort(key=lambda r: (r.shift_slot.shift_date, r.shift_slot.start_time))

    incoming_swap_count = (
        ShiftSwapRequest.query
        .join(ShiftRequest, ShiftSwapRequest.target_shift_request_id == ShiftRequest.id)
        .filter(
            ShiftRequest.volunteer_id == current_user.id,
            ShiftSwapRequest.status == ShiftSwapStatus.PENDING,
        )
        .count()
    )

    return render_template(
        "volunteer/dashboard.html",
        con_year=con_year,
        tiers=tiers,
        hours=hours,
        current_tier=current_tier,
        next_tier=next_tier,
        remaining=remaining,
        my_entries=my_entries,
        my_claims=my_claims,
        pending_requests=pending_requests,
        confirmed_requests=confirmed_requests,
        incoming_swap_count=incoming_swap_count,
        now=now,
    )
