import json
from datetime import datetime

from flask import Blueprint, render_template, redirect, url_for, flash, request, abort, jsonify
from flask_login import login_required, current_user

from app.extensions import db
from app.models import (
    User, Role, Department, ShiftSlot, ShiftRequest, ShiftRequestStatus, AuditLog,
    AvailabilityBlock, VolunteerDepartmentEligibility, ShiftSwapRequest, ShiftSwapStatus
)
from app.utils import roles_required, get_current_con_year, send_shift_notification

calendar_bp = Blueprint("calendar", __name__)


def _managed_department_ids():
    if current_user.role == Role.ADMIN:
        return [d.id for d in Department.query.all()]
    return [link.department_id for link in current_user.department_links]


def _overlaps_slot(block, slot):
    return (
        block.avail_date == slot.shift_date
        and block.start_time < slot.end_time
        and block.end_time > slot.start_time
    )


def _slot_overlaps(a, b):
    return (
        a.shift_date == b.shift_date
        and a.start_time < b.end_time
        and a.end_time > b.start_time
    )


def _would_overlap_other_shift(volunteer, new_slot, excluding_request_id):
    """True if giving `volunteer` a shift on `new_slot` would double-book
    them against another approved shift they already hold."""
    for other in volunteer.shift_requests:
        if other.id == excluding_request_id or other.status != ShiftRequestStatus.APPROVED:
            continue
        if _slot_overlaps(other.shift_slot, new_slot):
            return True
    return False


def _eligible_volunteer_ids_by_department():
    """dept_id -> list of active volunteer ids approved to work that dept."""
    links = (
        VolunteerDepartmentEligibility.query
        .join(User, VolunteerDepartmentEligibility.volunteer_id == User.id)
        .filter(User.active.is_(True), User.role == Role.VOLUNTEER)
        .all()
    )
    by_dept = {}
    for link in links:
        by_dept.setdefault(link.department_id, []).append(link.volunteer_id)
    return by_dept


def build_auto_assign_plan(con_year, department_ids=None, wipe_existing=False):
    """Compute (without writing to the DB) who auto-assign would put on
    which open shifts. `department_ids=None` means every department;
    `wipe_existing=True` first clears every current approved assignment in
    scope and re-fills every slot from scratch instead of only filling gaps.

    Candidates are ranked by: eligible for the department (required), then
    volunteers who marked themselves available for that time slot first,
    then whoever currently has the fewest shifts (so assignments spread out
    instead of piling onto the same few people). A volunteer is never
    double-booked against a shift they already hold.

    Returns a dict with ORM objects, meant for a preview page -- nothing is
    persisted here."""
    now = datetime.now()

    slots_query = ShiftSlot.query.filter(ShiftSlot.con_year_id == con_year.id)
    if department_ids is not None:
        slots_query = slots_query.filter(ShiftSlot.department_id.in_(department_ids))
    slots = [s for s in slots_query.all() if datetime.combine(s.shift_date, s.end_time) > now]
    slots.sort(key=lambda s: (s.shift_date, s.start_time))
    target_slot_ids = {s.id for s in slots}

    volunteers_by_id = {v.id: v for v in User.query.filter_by(role=Role.VOLUNTEER, active=True).all()}
    approved_slots_by_volunteer = {v_id: [] for v_id in volunteers_by_id}
    load_count = {v_id: 0 for v_id in volunteers_by_id}

    to_cancel = []
    existing_approved = (
        ShiftRequest.query
        .join(ShiftSlot, ShiftRequest.shift_slot_id == ShiftSlot.id)
        .filter(ShiftRequest.status == ShiftRequestStatus.APPROVED, ShiftSlot.con_year_id == con_year.id)
        .all()
    )
    for req in existing_approved:
        if wipe_existing and req.shift_slot_id in target_slot_ids:
            to_cancel.append(req)
            continue
        if req.volunteer_id in approved_slots_by_volunteer:
            approved_slots_by_volunteer[req.volunteer_id].append(req.shift_slot)
            load_count[req.volunteer_id] += 1

    eligible_by_dept = _eligible_volunteer_ids_by_department()
    availability_by_volunteer = {}
    for block in AvailabilityBlock.query.filter_by(con_year_id=con_year.id).all():
        availability_by_volunteer.setdefault(block.volunteer_id, []).append(block)

    assignments = []
    unfilled = []

    for slot in slots:
        currently_filled = 0 if wipe_existing else slot.approved_count()
        remaining = max(slot.capacity - currently_filled, 0)
        if remaining <= 0:
            continue

        candidates = []
        for v_id in eligible_by_dept.get(slot.department_id, []):
            if v_id not in volunteers_by_id:
                continue
            already_on_this_slot = any(s.id == slot.id for s in approved_slots_by_volunteer[v_id])
            if already_on_this_slot:
                continue
            overlaps = any(_slot_overlaps(other, slot) for other in approved_slots_by_volunteer[v_id])
            if overlaps:
                continue
            is_available = any(
                _overlaps_slot(block, slot) for block in availability_by_volunteer.get(v_id, [])
            )
            candidates.append((v_id, is_available))

        # available-first, then fewest current shifts, then stable by id
        candidates.sort(key=lambda c: (not c[1], load_count[c[0]], c[0]))

        filled_here = 0
        for v_id, _is_available in candidates:
            if filled_here >= remaining:
                break
            volunteer = volunteers_by_id[v_id]
            assignments.append({"slot": slot, "volunteer": volunteer})
            approved_slots_by_volunteer[v_id].append(slot)
            load_count[v_id] += 1
            filled_here += 1

        if filled_here < remaining:
            unfilled.append({"slot": slot, "open_spots": remaining - filled_here})

    return {"to_cancel": to_cancel, "assignments": assignments, "unfilled": unfilled}
    return False


def _cancel_overlapping_requests(volunteer, confirmed_slot, keep_request_id=None):
    query = ShiftRequest.query.join(ShiftSlot, ShiftRequest.shift_slot_id == ShiftSlot.id).filter(
        ShiftRequest.volunteer_id == volunteer.id,
        ShiftRequest.status != ShiftRequestStatus.CANCELLED,
    )
    if keep_request_id is not None:
        query = query.filter(ShiftRequest.id != keep_request_id)

    overlapping_requests = query.all()

    for request_row in overlapping_requests:
        if not _slot_overlaps(request_row.shift_slot, confirmed_slot):
            continue
        request_row.status = ShiftRequestStatus.CANCELLED
        request_row.decided_by_id = current_user.id
        request_row.decided_at = datetime.utcnow()
        db.session.add(AuditLog(
            actor_id=current_user.id,
            action="auto_cancel_shift_request",
            target_type="ShiftRequest",
            target_id=request_row.id,
            detail=(
                f"{request_row.volunteer.badge_name} overlap with confirmed "
                f"{confirmed_slot.shift_date} {confirmed_slot.start_time}-{confirmed_slot.end_time}"
            ),
        ))


@calendar_bp.route("/")
@login_required
def view():
    con_year = get_current_con_year()
    if not con_year:
        flash("No active con year is set.", "warning")
        return redirect(url_for("index"))

    now = datetime.now()

    days = con_year.date_range()
    slots_by_day = {d: [] for d in days}

    filter_department_id = request.args.get("department_id", type=int)
    filter_start_hour = request.args.get("start_hour", type=int)
    filter_end_hour = request.args.get("end_hour", type=int)
    filter_min_length = request.args.get("min_length", type=float)

    def slot_matches_filters(slot):
        if filter_department_id and slot.department_id != filter_department_id:
            return False
        if filter_start_hour is not None and slot.start_time.hour < filter_start_hour:
            return False
        if filter_end_hour is not None and slot.start_time.hour >= filter_end_hour:
            return False
        if filter_min_length is not None and slot.hours() < filter_min_length:
            return False
        return True

    slots = ShiftSlot.query.filter_by(con_year_id=con_year.id).order_by(
        ShiftSlot.shift_date, ShiftSlot.start_time
    ).all()

    can_manage = current_user.role in (Role.ADMIN, Role.ASSIGNER)
    managed_dept_ids = _managed_department_ids() if can_manage else []
    eligible_dept_ids = current_user.eligible_department_ids() if current_user.role == Role.VOLUNTEER else []

    for s in slots:
        if s.shift_date not in slots_by_day:
            continue
        slot_end = datetime.combine(s.shift_date, s.end_time)
        if slot_end <= now:
            continue
        # Volunteers only ever see (and can request) open shifts in
        # departments they've been approved for -- everyone else sees all.
        if current_user.role == Role.VOLUNTEER and s.department_id not in eligible_dept_ids:
            continue
        if not slot_matches_filters(s):
            continue
        slots_by_day[s.shift_date].append(s)

    my_request_by_slot = {}
    if current_user.role == Role.VOLUNTEER:
        for r in current_user.shift_requests:
            if r.status != ShiftRequestStatus.CANCELLED:
                my_request_by_slot[r.shift_slot_id] = r

    departments = Department.query.order_by(Department.name).all()
    volunteers = User.query.filter_by(role=Role.VOLUNTEER, active=True).order_by(User.badge_name).all() if can_manage else []
    # Plain-data version of `volunteers` for the assign-slot search box (JS
    # can't call ORM methods like eligible_department_ids() on the fly).
    volunteers_for_assign = [
        {"id": v.id, "name": v.badge_name, "eligible_dept_ids": v.eligible_department_ids()}
        for v in volunteers
    ] if can_manage else []
    available_volunteers_by_slot = {}
    if can_manage:
        for slot in slots:
            if slot.shift_date not in slots_by_day:
                continue
            available_volunteers_by_slot[slot.id] = [
                volunteer
                for volunteer in volunteers
                if any(
                    block.con_year_id == con_year.id and _overlaps_slot(block, slot)
                    for block in volunteer.availability_blocks
                )
            ]

    # My own availability blocks (volunteer's drag-selected free time)
    my_availability_by_day = {d: [] for d in days}
    if current_user.role == Role.VOLUNTEER:
        for block in current_user.availability_blocks:
            if block.con_year_id == con_year.id and block.avail_date in my_availability_by_day:
                my_availability_by_day[block.avail_date].append(block)

    # For staff: everyone's availability, grouped by day, so they know who to reach out to
    all_availability_by_day = {d: [] for d in days}
    if can_manage:
        all_blocks = AvailabilityBlock.query.filter_by(con_year_id=con_year.id).order_by(
            AvailabilityBlock.avail_date, AvailabilityBlock.start_time
        ).all()
        for block in all_blocks:
            if block.avail_date in all_availability_by_day:
                all_availability_by_day[block.avail_date].append(block)

    # Data for the staff-only hourly (24hr) calendar view: who's on shift at
    # each hour of each day. Built here (rather than in the template) since
    # Jinja has no list-comprehension syntax for the per-slot roster.
    hourly_shifts_by_day = {}
    if can_manage:
        active_statuses = (ShiftRequestStatus.APPROVED, ShiftRequestStatus.IN_PROGRESS, ShiftRequestStatus.COMPLETED)
        for d in days:
            day_shifts = []
            for slot in slots_by_day[d]:
                end_hour = slot.end_time.hour if slot.end_time.minute == 0 else slot.end_time.hour + 1
                names = [
                    r.volunteer.badge_name + (" (in progress)" if r.status == ShiftRequestStatus.IN_PROGRESS else "")
                    for r in slot.requests
                    if r.status in active_statuses
                ]
                day_shifts.append({
                    "start": slot.start_time.hour,
                    "end": end_hour,
                    "title": slot.label or slot.department.name,
                    "capacity": slot.capacity,
                    "approved": slot.approved_count(),
                    "is_full": slot.is_full(),
                    "names": names,
                })
            hourly_shifts_by_day[d.isoformat()] = day_shifts

    return render_template(
        "calendar/view.html",
        con_year=con_year,
        days=days,
        slots_by_day=slots_by_day,
        my_request_by_slot=my_request_by_slot,
        departments=departments,
        volunteers=volunteers,
        volunteers_for_assign=volunteers_for_assign,
        available_volunteers_by_slot=available_volunteers_by_slot,
        can_manage=can_manage,
        managed_dept_ids=managed_dept_ids,
        eligible_dept_ids=eligible_dept_ids,
        my_availability_by_day=my_availability_by_day,
        all_availability_by_day=all_availability_by_day,
        hourly_shifts_by_day=hourly_shifts_by_day,
        filter_department_id=filter_department_id,
        filter_start_hour=filter_start_hour,
        filter_end_hour=filter_end_hour,
        filter_min_length=filter_min_length,
        now=now,
    )


@calendar_bp.route("/slots/new", methods=["POST"])
@login_required
@roles_required(Role.ADMIN, Role.ASSIGNER)
def new_slot():
    con_year = get_current_con_year()
    department_id = int(request.form["department_id"])

    if current_user.role != Role.ADMIN and department_id not in _managed_department_ids():
        abort(403)

    shift_date = datetime.strptime(request.form["shift_date"], "%Y-%m-%d").date()
    start_time = datetime.strptime(request.form["start_time"], "%H:%M").time()
    end_time = datetime.strptime(request.form["end_time"], "%H:%M").time()
    capacity = int(request.form.get("capacity", 1))
    label = request.form.get("label", "").strip() or None

    slot = ShiftSlot(
        con_year_id=con_year.id,
        department_id=department_id,
        shift_date=shift_date,
        start_time=start_time,
        end_time=end_time,
        capacity=capacity,
        label=label,
        created_by_id=current_user.id,
    )
    db.session.add(slot)
    db.session.commit()
    flash("Shift slot added to calendar.", "success")
    return redirect(url_for("calendar.view"))


@calendar_bp.route("/slots/bulk", methods=["POST"])
@login_required
@roles_required(Role.ADMIN, Role.ASSIGNER)
def bulk_new_slots():
    """Create the same shift slot (same department/time/capacity/label)
    across several con days at once -- for recurring shifts like a daily
    registration desk, instead of adding each day one at a time."""
    con_year = get_current_con_year()
    department_id = int(request.form["department_id"])

    if current_user.role != Role.ADMIN and department_id not in _managed_department_ids():
        abort(403)

    shift_date_strings = request.form.getlist("shift_dates")
    if not shift_date_strings:
        flash("Select at least one day for the recurring shift.", "danger")
        return redirect(url_for("calendar.view"))

    start_time = datetime.strptime(request.form["start_time"], "%H:%M").time()
    end_time = datetime.strptime(request.form["end_time"], "%H:%M").time()
    capacity = int(request.form.get("capacity", 1))
    label = request.form.get("label", "").strip() or None

    valid_dates = set(con_year.date_range())
    created = 0
    skipped = 0

    for raw in shift_date_strings:
        try:
            shift_date = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            continue
        if shift_date not in valid_dates:
            continue

        exists = ShiftSlot.query.filter_by(
            con_year_id=con_year.id, department_id=department_id,
            shift_date=shift_date, start_time=start_time, end_time=end_time,
        ).first()
        if exists:
            skipped += 1
            continue

        db.session.add(ShiftSlot(
            con_year_id=con_year.id,
            department_id=department_id,
            shift_date=shift_date,
            start_time=start_time,
            end_time=end_time,
            capacity=capacity,
            label=label,
            created_by_id=current_user.id,
        ))
        created += 1

    db.session.commit()

    if created:
        message = f"Created {created} shift slot(s)."
        if skipped:
            message += f" Skipped {skipped} day(s) that already had a matching slot."
        flash(message, "success")
    else:
        flash("No new shift slots created -- all selected days already had a matching slot.", "warning")
    return redirect(url_for("calendar.view"))


def _auto_assign_department_scope():
    """Resolve the (mode, department_ids, send_email) the auto-assign form
    submitted, enforcing that assigners can never touch a department they
    don't manage -- 'All Departments' for an assigner silently means all of
    *their* departments, not the whole system."""
    mode = request.form.get("mode")
    if mode not in ("assign", "reassign"):
        abort(400)

    managed_ids = _managed_department_ids()
    dept_param = request.form.get("department_id", "").strip()
    if dept_param:
        try:
            department_id = int(dept_param)
        except ValueError:
            abort(400)
        if current_user.role != Role.ADMIN and department_id not in managed_ids:
            abort(403)
        department_ids = [department_id]
    else:
        department_ids = None if current_user.role == Role.ADMIN else managed_ids

    send_email = request.form.get("send_email") == "on"
    return mode, department_ids, send_email


@calendar_bp.route("/auto-assign/preview", methods=["POST"])
@login_required
@roles_required(Role.ADMIN, Role.ASSIGNER)
def auto_assign_preview():
    """Step 1 of 2: compute the proposed auto-assignment plan and show it
    for review -- nothing is written to the database yet. Confirming on
    that page (auto_assign_apply) is the second, explicit step."""
    con_year = get_current_con_year()
    if not con_year:
        flash("No active con year is set.", "warning")
        return redirect(url_for("calendar.view"))

    mode, department_ids, send_email = _auto_assign_department_scope()
    wipe_existing = mode == "reassign"

    dept_param = request.form.get("department_id", "").strip()
    if dept_param:
        department = Department.query.get(int(dept_param))
        scope_label = department.name if department else "Unknown department"
    else:
        scope_label = "All Departments" if current_user.role == Role.ADMIN else "All my departments"

    plan = build_auto_assign_plan(con_year, department_ids=department_ids, wipe_existing=wipe_existing)

    plan_payload = json.dumps({
        "to_cancel_ids": [r.id for r in plan["to_cancel"]],
        "assignments": [{"slot_id": a["slot"].id, "volunteer_id": a["volunteer"].id} for a in plan["assignments"]],
    })

    return render_template(
        "calendar/auto_assign_preview.html",
        con_year=con_year,
        mode=mode,
        department_id=dept_param,
        scope_label=scope_label,
        send_email=send_email,
        plan=plan,
        plan_payload=plan_payload,
    )


@calendar_bp.route("/auto-assign/apply", methods=["POST"])
@login_required
@roles_required(Role.ADMIN, Role.ASSIGNER)
def auto_assign_apply():
    """Step 2 of 2: re-validate and actually write the plan the user just
    confirmed on the preview page. Nothing from the submitted plan is
    trusted beyond which slot/volunteer ids it names -- every assignment
    and cancellation is re-checked against live data (still approved,
    still eligible, still open, still non-overlapping, still an authorized
    department) since time may have passed since the preview was shown."""
    con_year = get_current_con_year()
    if not con_year:
        flash("No active con year is set.", "warning")
        return redirect(url_for("calendar.view"))

    mode = request.form.get("mode")
    if mode not in ("assign", "reassign"):
        abort(400)
    send_email = request.form.get("send_email") == "on"

    try:
        plan_payload = json.loads(request.form.get("plan_payload", "{}"))
    except (TypeError, ValueError):
        abort(400)

    to_cancel_ids = plan_payload.get("to_cancel_ids") or []
    proposed_assignments = plan_payload.get("assignments") or []

    managed_ids = _managed_department_ids()

    def _authorized(department_id):
        return current_user.role == Role.ADMIN or department_id in managed_ids

    cancelled_count = 0
    if to_cancel_ids:
        for req in ShiftRequest.query.filter(ShiftRequest.id.in_(to_cancel_ids)).all():
            if req.status != ShiftRequestStatus.APPROVED or not _authorized(req.shift_slot.department_id):
                continue
            req.status = ShiftRequestStatus.CANCELLED
            req.decided_by_id = current_user.id
            req.decided_at = datetime.utcnow()
            cancelled_count += 1

    db.session.flush()  # so the cancellations above count in the checks below

    assigned_count = 0
    skipped_count = 0
    newly_assigned = []

    for item in proposed_assignments:
        slot = ShiftSlot.query.get(item.get("slot_id"))
        volunteer = User.query.get(item.get("volunteer_id"))
        if not slot or not volunteer or volunteer.role != Role.VOLUNTEER or not volunteer.active:
            skipped_count += 1
            continue
        if not _authorized(slot.department_id) or slot.department_id not in volunteer.eligible_department_ids():
            skipped_count += 1
            continue
        if slot.is_full():
            skipped_count += 1
            continue

        existing = ShiftRequest.query.filter_by(shift_slot_id=slot.id, volunteer_id=volunteer.id).first()
        if existing and existing.status == ShiftRequestStatus.APPROVED:
            skipped_count += 1
            continue
        if _would_overlap_other_shift(volunteer, slot, existing.id if existing else None):
            skipped_count += 1
            continue

        if existing:
            existing.status = ShiftRequestStatus.APPROVED
            existing.decided_by_id = current_user.id
            existing.decided_at = datetime.utcnow()
        else:
            db.session.add(ShiftRequest(
                shift_slot_id=slot.id, volunteer_id=volunteer.id,
                status=ShiftRequestStatus.APPROVED,
                decided_by_id=current_user.id, decided_at=datetime.utcnow(),
            ))
            db.session.flush()

        assigned_count += 1
        newly_assigned.append((volunteer, slot))

    db.session.add(AuditLog(
        actor_id=current_user.id, action="auto_assign_shifts",
        target_type="ShiftRequest", target_id=0,
        detail=f"mode={mode}; assigned={assigned_count}; cancelled={cancelled_count}; skipped={skipped_count}",
    ))
    db.session.commit()

    if send_email:
        for volunteer, slot in newly_assigned:
            send_shift_notification(volunteer, slot)

    message = f"Auto-assign complete: {assigned_count} volunteer(s) assigned"
    if cancelled_count:
        message += f", {cancelled_count} prior assignment(s) cleared"
    if skipped_count:
        message += f", {skipped_count} skipped (no longer valid by the time this was applied)"
    flash(message + ".", "success")
    return redirect(url_for("calendar.view"))


@calendar_bp.route("/slots/<int:slot_id>/assign", methods=["POST"])
@login_required
@roles_required(Role.ADMIN, Role.ASSIGNER)
def assign_slot(slot_id):
    """Proactively put a volunteer on a shift, skipping the request step --
    for when staff wants to build the schedule directly instead of waiting
    on requests to come in."""
    slot = ShiftSlot.query.get_or_404(slot_id)
    if current_user.role != Role.ADMIN and slot.department_id not in _managed_department_ids():
        abort(403)

    volunteer_id = request.form.get("volunteer_id", type=int)
    send_email = request.form.get("send_email") == "on"
    volunteer = User.query.filter_by(id=volunteer_id, role=Role.VOLUNTEER).first()
    if not volunteer:
        flash("Volunteer not found.", "danger")
        return redirect(url_for("calendar.view"))

    existing = ShiftRequest.query.filter_by(shift_slot_id=slot.id, volunteer_id=volunteer.id).first()
    if existing and existing.status == ShiftRequestStatus.APPROVED:
        flash(f"{volunteer.badge_name} is already on this shift.", "warning")
        return redirect(url_for("calendar.view"))

    if slot.is_full():
        flash("Can't assign -- slot is already full.", "danger")
        return redirect(url_for("calendar.view"))

    if existing:
        existing.status = ShiftRequestStatus.APPROVED
        existing.decided_by_id = current_user.id
        existing.decided_at = datetime.utcnow()
        req = existing
    else:
        req = ShiftRequest(
            shift_slot_id=slot.id, volunteer_id=volunteer.id,
            status=ShiftRequestStatus.APPROVED,
            decided_by_id=current_user.id, decided_at=datetime.utcnow(),
        )
        db.session.add(req)
        db.session.flush()  # populate req.id for the audit log below

    db.session.add(AuditLog(
        actor_id=current_user.id, action="assign_shift",
        target_type="ShiftRequest", target_id=req.id,
        detail=f"{volunteer.badge_name} -> {slot.shift_date} {slot.start_time}-{slot.end_time}"
    ))

    _cancel_overlapping_requests(volunteer, slot, keep_request_id=req.id)
    db.session.commit()

    if send_email:
        emailed, message = send_shift_notification(volunteer, slot)
        if emailed:
            flash(f"Assigned {volunteer.badge_name} to {slot.label or slot.department.name} and notified them.", "success")
        else:
            flash(f"Assigned {volunteer.badge_name} to {slot.label or slot.department.name}. {message}", "warning")
        return redirect(url_for("calendar.view"))

    flash(f"Assigned {volunteer.badge_name} to {slot.label or slot.department.name}.", "success")
    return redirect(url_for("calendar.view"))


@calendar_bp.route("/requests/<int:request_id>/notify", methods=["POST"])
@login_required
@roles_required(Role.ADMIN, Role.ASSIGNER)
def notify_request(request_id):
    req = ShiftRequest.query.get_or_404(request_id)
    slot = req.shift_slot
    if current_user.role != Role.ADMIN and slot.department_id not in _managed_department_ids():
        abort(403)

    sent, message = send_shift_notification(req.volunteer, slot)
    if sent:
        flash(f"Notified {req.volunteer.badge_name}.", "success")
    else:
        flash(message or "Unable to notify volunteer.", "warning")
    return redirect(request.referrer or url_for("calendar.view"))


@calendar_bp.route("/slots/<int:slot_id>/delete", methods=["POST"])
@login_required
@roles_required(Role.ADMIN, Role.ASSIGNER)
def delete_slot(slot_id):
    slot = ShiftSlot.query.get_or_404(slot_id)
    if current_user.role != Role.ADMIN and slot.department_id not in _managed_department_ids():
        abort(403)
    db.session.delete(slot)
    db.session.commit()
    flash("Shift slot removed.", "info")
    return redirect(url_for("calendar.view"))


@calendar_bp.route("/slots/<int:slot_id>/request", methods=["POST"])
@login_required
@roles_required(Role.VOLUNTEER)
def request_slot(slot_id):
    slot = ShiftSlot.query.get_or_404(slot_id)

    if slot.department_id not in current_user.eligible_department_ids():
        abort(403)

    existing = ShiftRequest.query.filter_by(shift_slot_id=slot.id, volunteer_id=current_user.id).first()
    if existing:
        flash("You've already requested this slot.", "warning")
        return redirect(url_for("calendar.view"))

    if slot.is_full():
        flash("That slot is already full.", "warning")
        return redirect(url_for("calendar.view"))

    req = ShiftRequest(shift_slot_id=slot.id, volunteer_id=current_user.id)
    db.session.add(req)
    db.session.commit()
    flash(f"Requested: {slot.label or slot.department.name} on {slot.shift_date}.", "success")
    return redirect(url_for("calendar.view"))


@calendar_bp.route("/requests/<int:request_id>/cancel", methods=["POST"])
@login_required
def cancel_request(request_id):
    req = ShiftRequest.query.get_or_404(request_id)
    if req.volunteer_id != current_user.id and current_user.role not in (Role.ADMIN, Role.ASSIGNER):
        abort(403)
    if req.volunteer_id == current_user.id and req.status in (
        ShiftRequestStatus.APPROVED,
        ShiftRequestStatus.IN_PROGRESS,
        ShiftRequestStatus.COMPLETED,
    ):
        flash("Use request leave for a confirmed shift, or end the shift if it has already started.", "warning")
        return redirect(request.referrer or url_for("calendar.view"))
    req.status = ShiftRequestStatus.CANCELLED
    db.session.commit()
    flash("Request cancelled.", "info")
    return redirect(request.referrer or url_for("calendar.view"))


@calendar_bp.route("/requests/<int:request_id>/leave-request", methods=["POST"])
@login_required
@roles_required(Role.VOLUNTEER)
def request_leave(request_id):
    req = ShiftRequest.query.get_or_404(request_id)
    if req.volunteer_id != current_user.id:
        abort(403)
    if req.status != ShiftRequestStatus.APPROVED:
        flash("You can only request leave for a confirmed shift.", "warning")
        return redirect(request.referrer or url_for("calendar.view"))

    reason = request.form.get("reason", "").strip()
    if not reason:
        flash("A reason is required to request leave.", "danger")
        return redirect(request.referrer or url_for("calendar.view"))

    req.status = ShiftRequestStatus.LEAVE_REQUESTED
    req.notes = reason
    db.session.add(AuditLog(
        actor_id=current_user.id,
        action="request_shift_leave",
        target_type="ShiftRequest",
        target_id=req.id,
        detail=f"{req.volunteer.badge_name} -> {req.shift_slot.shift_date} {req.shift_slot.start_time}-{req.shift_slot.end_time}: {reason}",
    ))
    db.session.commit()
    flash("Leave request submitted.", "success")
    return redirect(request.referrer or url_for("calendar.view"))


@calendar_bp.route("/requests/<int:request_id>/check-in", methods=["POST"])
@login_required
@roles_required(Role.VOLUNTEER)
def check_in_request(request_id):
    req = ShiftRequest.query.get_or_404(request_id)
    if req.volunteer_id != current_user.id:
        abort(403)
    if not req.can_check_in(datetime.now()):
        flash("You can check in starting 1 hour before the shift and up to the shift end time.", "warning")
        return redirect(request.referrer or url_for("calendar.view"))

    req.status = ShiftRequestStatus.IN_PROGRESS
    req.checked_in_at = datetime.now()
    db.session.add(AuditLog(
        actor_id=current_user.id,
        action="check_in_shift",
        target_type="ShiftRequest",
        target_id=req.id,
        detail=f"{req.volunteer.badge_name} checked in for {req.shift_slot.shift_date} {req.shift_slot.start_time}-{req.shift_slot.end_time}",
    ))
    db.session.commit()
    flash("Shift started.", "success")
    return redirect(request.referrer or url_for("calendar.view"))


@calendar_bp.route("/requests/<int:request_id>/check-out", methods=["POST"])
@login_required
@roles_required(Role.VOLUNTEER)
def check_out_request(request_id):
    req = ShiftRequest.query.get_or_404(request_id)
    if req.volunteer_id != current_user.id:
        abort(403)
    if req.status != ShiftRequestStatus.IN_PROGRESS:
        flash("That shift is not currently in progress.", "warning")
        return redirect(request.referrer or url_for("calendar.view"))

    req.status = ShiftRequestStatus.COMPLETED
    req.checked_out_at = datetime.now()
    db.session.add(AuditLog(
        actor_id=current_user.id,
        action="check_out_shift",
        target_type="ShiftRequest",
        target_id=req.id,
        detail=f"{req.volunteer.badge_name} checked out of {req.shift_slot.shift_date} {req.shift_slot.start_time}-{req.shift_slot.end_time}",
    ))
    db.session.commit()
    flash("Shift ended.", "success")
    return redirect(request.referrer or url_for("calendar.view"))


@calendar_bp.route("/requests/<int:request_id>/approve", methods=["POST"])
@login_required
@roles_required(Role.ADMIN, Role.ASSIGNER)
def approve_request(request_id):
    req = ShiftRequest.query.get_or_404(request_id)
    slot = req.shift_slot
    if current_user.role != Role.ADMIN and slot.department_id not in _managed_department_ids():
        abort(403)
    if req.status == ShiftRequestStatus.LEAVE_REQUESTED:
        req.status = ShiftRequestStatus.CANCELLED
        req.decided_by_id = current_user.id
        req.decided_at = datetime.utcnow()
        db.session.add(AuditLog(
            actor_id=current_user.id, action="approve_shift_leave",
            target_type="ShiftRequest", target_id=req.id,
            detail=f"{req.volunteer.badge_name} leave approved for {slot.shift_date} {slot.start_time}-{slot.end_time}: {req.notes or ''}"
        ))
        db.session.commit()
        flash("Leave request approved.", "success")
        return redirect(request.referrer or url_for("calendar.view"))

    if slot.is_full() and req.status != ShiftRequestStatus.APPROVED:
        flash("Can't approve -- slot is already full.", "danger")
        return redirect(request.referrer or url_for("calendar.view"))

    req.status = ShiftRequestStatus.APPROVED
    req.decided_by_id = current_user.id
    req.decided_at = datetime.utcnow()
    db.session.add(AuditLog(
        actor_id=current_user.id, action="approve_shift_request",
        target_type="ShiftRequest", target_id=req.id,
        detail=f"{req.volunteer.badge_name} -> {slot.shift_date} {slot.start_time}-{slot.end_time}"
    ))

    _cancel_overlapping_requests(req.volunteer, slot, keep_request_id=req.id)
    db.session.commit()
    flash("Shift request approved.", "success")
    return redirect(request.referrer or url_for("calendar.view"))


@calendar_bp.route("/requests/<int:request_id>/deny", methods=["POST"])
@login_required
@roles_required(Role.ADMIN, Role.ASSIGNER)
def deny_request(request_id):
    req = ShiftRequest.query.get_or_404(request_id)
    slot = req.shift_slot
    if current_user.role != Role.ADMIN and slot.department_id not in _managed_department_ids():
        abort(403)

    if req.status == ShiftRequestStatus.LEAVE_REQUESTED:
        req.status = ShiftRequestStatus.LEAVE_DENIED
        req.decided_by_id = current_user.id
        req.decided_at = datetime.utcnow()
        db.session.add(AuditLog(
            actor_id=current_user.id, action="deny_shift_leave",
            target_type="ShiftRequest", target_id=req.id,
            detail=f"{req.volunteer.badge_name} leave denied for {slot.shift_date} {slot.start_time}-{slot.end_time}: {req.notes or ''}"
        ))
        db.session.commit()
        flash("Leave request denied.", "info")
        return redirect(request.referrer or url_for("calendar.view"))

    req.status = ShiftRequestStatus.DENIED
    req.decided_by_id = current_user.id
    req.decided_at = datetime.utcnow()
    db.session.add(AuditLog(
        actor_id=current_user.id, action="deny_shift_request",
        target_type="ShiftRequest", target_id=req.id,
        detail=f"{req.volunteer.badge_name} -> {slot.shift_date} {slot.start_time}-{slot.end_time}"
    ))
    db.session.commit()
    flash("Shift request denied.", "info")
    return redirect(request.referrer or url_for("calendar.view"))


@calendar_bp.route("/availability/add", methods=["POST"])
@login_required
@roles_required(Role.VOLUNTEER)
def add_availability():
    """Called via fetch() from the click-and-drag availability grid."""
    con_year = get_current_con_year()
    if not con_year:
        return jsonify({"error": "no active con year"}), 400

    try:
        avail_date = datetime.strptime(request.form["avail_date"], "%Y-%m-%d").date()
        start_time = datetime.strptime(request.form["start_time"], "%H:%M").time()
        end_time = datetime.strptime(request.form["end_time"], "%H:%M").time()
    except (KeyError, ValueError):
        return jsonify({"error": "invalid input"}), 400

    if avail_date not in con_year.date_range():
        return jsonify({"error": "date outside con range"}), 400
    if start_time >= end_time:
        return jsonify({"error": "start must be before end"}), 400

    block = AvailabilityBlock(
        volunteer_id=current_user.id,
        con_year_id=con_year.id,
        avail_date=avail_date,
        start_time=start_time,
        end_time=end_time,
    )
    db.session.add(block)
    db.session.commit()
    return jsonify({
        "id": block.id,
        "avail_date": avail_date.isoformat(),
        "start_time": start_time.strftime("%H:%M"),
        "end_time": end_time.strftime("%H:%M"),
    })


@calendar_bp.route("/availability/<int:block_id>/delete", methods=["POST"])
@login_required
@roles_required(Role.VOLUNTEER)
def delete_availability(block_id):
    block = AvailabilityBlock.query.get_or_404(block_id)
    if block.volunteer_id != current_user.id:
        abort(403)
    db.session.delete(block)
    db.session.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"deleted": True})
    flash("Availability removed.", "info")
    return redirect(url_for("calendar.view"))


@calendar_bp.route("/swaps")
@login_required
@roles_required(Role.VOLUNTEER)
def swaps():
    """Self-serve shift trading: propose swapping one of your confirmed
    shifts for another volunteer's, no staff involvement needed unless
    something's gone wrong by the time it's accepted."""
    con_year = get_current_con_year()
    now = datetime.now()

    my_shifts = []
    candidates = []
    incoming = []
    outgoing = []

    if con_year:
        my_shifts = [
            r for r in current_user.shift_requests
            if r.status == ShiftRequestStatus.APPROVED and r.scheduled_end() > now
        ]
        my_shifts.sort(key=lambda r: (r.shift_slot.shift_date, r.shift_slot.start_time))

        eligible_dept_ids = current_user.eligible_department_ids()
        candidates = (
            ShiftRequest.query
            .join(ShiftSlot, ShiftRequest.shift_slot_id == ShiftSlot.id)
            .filter(
                ShiftRequest.status == ShiftRequestStatus.APPROVED,
                ShiftRequest.volunteer_id != current_user.id,
                ShiftSlot.con_year_id == con_year.id,
                ShiftSlot.department_id.in_(eligible_dept_ids),
            )
            .all()
        )
        candidates = [r for r in candidates if r.scheduled_end() > now]
        candidates.sort(key=lambda r: (r.shift_slot.shift_date, r.shift_slot.start_time))

        incoming = (
            ShiftSwapRequest.query
            .join(ShiftRequest, ShiftSwapRequest.target_shift_request_id == ShiftRequest.id)
            .filter(
                ShiftRequest.volunteer_id == current_user.id,
                ShiftSwapRequest.status == ShiftSwapStatus.PENDING,
            )
            .order_by(ShiftSwapRequest.created_at.desc())
            .all()
        )

        outgoing = (
            ShiftSwapRequest.query
            .filter_by(requester_id=current_user.id)
            .order_by(ShiftSwapRequest.created_at.desc())
            .limit(20)
            .all()
        )

    return render_template(
        "calendar/swaps.html",
        con_year=con_year,
        my_shifts=my_shifts,
        candidates=candidates,
        incoming=incoming,
        outgoing=outgoing,
        now=now,
    )


@calendar_bp.route("/swaps/propose", methods=["POST"])
@login_required
@roles_required(Role.VOLUNTEER)
def propose_swap():
    my_req = ShiftRequest.query.get_or_404(request.form.get("my_shift_request_id", type=int))
    target_req = ShiftRequest.query.get_or_404(request.form.get("target_shift_request_id", type=int))

    if my_req.volunteer_id != current_user.id:
        abort(403)
    if target_req.volunteer_id == current_user.id:
        flash("You can't propose a swap with yourself.", "danger")
        return redirect(url_for("calendar.swaps"))
    if my_req.status != ShiftRequestStatus.APPROVED or target_req.status != ShiftRequestStatus.APPROVED:
        flash("Both shifts must be confirmed to propose a swap.", "danger")
        return redirect(url_for("calendar.swaps"))

    now = datetime.now()
    if my_req.scheduled_end() <= now or target_req.scheduled_end() <= now:
        flash("Can't propose a swap involving a shift that's already over.", "danger")
        return redirect(url_for("calendar.swaps"))

    target_volunteer = target_req.volunteer
    if target_req.shift_slot.department_id not in current_user.eligible_department_ids():
        flash("You're not eligible for that shift's department.", "danger")
        return redirect(url_for("calendar.swaps"))
    if my_req.shift_slot.department_id not in target_volunteer.eligible_department_ids():
        flash(f"{target_volunteer.badge_name} isn't eligible for your shift's department.", "danger")
        return redirect(url_for("calendar.swaps"))

    duplicate = ShiftSwapRequest.query.filter_by(
        requester_id=current_user.id,
        requester_shift_request_id=my_req.id,
        target_shift_request_id=target_req.id,
        status=ShiftSwapStatus.PENDING,
    ).first()
    if duplicate:
        flash("You've already proposed that swap.", "warning")
        return redirect(url_for("calendar.swaps"))

    note = request.form.get("note", "").strip() or None
    swap = ShiftSwapRequest(
        requester_id=current_user.id,
        requester_shift_request_id=my_req.id,
        target_shift_request_id=target_req.id,
        note=note,
    )
    db.session.add(swap)
    db.session.commit()
    flash(f"Swap proposed to {target_volunteer.badge_name}. They'll need to accept it.", "success")
    return redirect(url_for("calendar.swaps"))


@calendar_bp.route("/swaps/<int:swap_id>/accept", methods=["POST"])
@login_required
@roles_required(Role.VOLUNTEER)
def accept_swap(swap_id):
    swap = ShiftSwapRequest.query.get_or_404(swap_id)
    if swap.target_shift_request.volunteer_id != current_user.id:
        abort(403)
    if swap.status != ShiftSwapStatus.PENDING:
        flash("That swap proposal is no longer pending.", "warning")
        return redirect(url_for("calendar.swaps"))

    req_a = swap.requester_shift_request  # the requester's shift
    req_b = swap.target_shift_request     # your shift
    requester = swap.requester
    target_volunteer = current_user
    now = datetime.now()

    if req_a.status != ShiftRequestStatus.APPROVED or req_b.status != ShiftRequestStatus.APPROVED:
        flash("One of these shifts is no longer confirmed, so this swap can't proceed.", "danger")
        swap.status = ShiftSwapStatus.CANCELLED
        swap.decided_at = datetime.utcnow()
        db.session.commit()
        return redirect(url_for("calendar.swaps"))

    if req_a.scheduled_end() <= now or req_b.scheduled_end() <= now:
        flash("One of these shifts has already happened, so this swap can't proceed.", "danger")
        swap.status = ShiftSwapStatus.CANCELLED
        swap.decided_at = datetime.utcnow()
        db.session.commit()
        return redirect(url_for("calendar.swaps"))

    if req_b.shift_slot.department_id not in requester.eligible_department_ids():
        flash("This swap is no longer valid -- eligibility has changed.", "danger")
        return redirect(url_for("calendar.swaps"))
    if req_a.shift_slot.department_id not in target_volunteer.eligible_department_ids():
        flash("This swap is no longer valid -- eligibility has changed.", "danger")
        return redirect(url_for("calendar.swaps"))

    if _would_overlap_other_shift(requester, req_b.shift_slot, req_a.id):
        flash(f"{requester.badge_name} already has an overlapping shift, so this swap can't proceed.", "danger")
        return redirect(url_for("calendar.swaps"))
    if _would_overlap_other_shift(target_volunteer, req_a.shift_slot, req_b.id):
        flash("You already have an overlapping shift, so this swap can't proceed.", "danger")
        return redirect(url_for("calendar.swaps"))

    conflict_a = ShiftRequest.query.filter(
        ShiftRequest.shift_slot_id == req_a.shift_slot_id,
        ShiftRequest.volunteer_id == target_volunteer.id,
        ShiftRequest.id != req_a.id,
    ).first()
    conflict_b = ShiftRequest.query.filter(
        ShiftRequest.shift_slot_id == req_b.shift_slot_id,
        ShiftRequest.volunteer_id == requester.id,
        ShiftRequest.id != req_b.id,
    ).first()
    if conflict_a or conflict_b:
        flash(
            "This swap can't be completed automatically -- one of you already has a request on "
            "record for the other's slot. Ask staff to assist.",
            "danger",
        )
        return redirect(url_for("calendar.swaps"))

    req_a_slot, req_b_slot = req_a.shift_slot, req_b.shift_slot
    req_a.volunteer_id, req_b.volunteer_id = target_volunteer.id, requester.id
    for r in (req_a, req_b):
        r.checked_in_at = None
        r.checked_out_at = None
        r.decided_by_id = current_user.id
        r.decided_at = datetime.utcnow()

    swap.status = ShiftSwapStatus.ACCEPTED
    swap.decided_at = datetime.utcnow()

    stale = ShiftSwapRequest.query.filter(
        ShiftSwapRequest.status == ShiftSwapStatus.PENDING,
        ShiftSwapRequest.id != swap.id,
        db.or_(
            ShiftSwapRequest.requester_shift_request_id.in_([req_a.id, req_b.id]),
            ShiftSwapRequest.target_shift_request_id.in_([req_a.id, req_b.id]),
        ),
    ).all()
    for s in stale:
        s.status = ShiftSwapStatus.CANCELLED
        s.decided_at = datetime.utcnow()

    db.session.add(AuditLog(
        actor_id=current_user.id, action="accept_shift_swap",
        target_type="ShiftSwapRequest", target_id=swap.id,
        detail=(
            f"{requester.badge_name} <-> {target_volunteer.badge_name}: "
            f"{req_a_slot.shift_date} {req_a_slot.start_time}-{req_a_slot.end_time} for "
            f"{req_b_slot.shift_date} {req_b_slot.start_time}-{req_b_slot.end_time}"
        ),
    ))
    db.session.commit()

    send_shift_notification(target_volunteer, req_a_slot)
    send_shift_notification(requester, req_b_slot)

    flash(
        f"Swap confirmed! You're now on {req_a_slot.label or req_a_slot.department.name} "
        f"on {req_a_slot.shift_date.strftime('%a %m/%d')}.",
        "success",
    )
    return redirect(url_for("calendar.swaps"))


@calendar_bp.route("/swaps/<int:swap_id>/decline", methods=["POST"])
@login_required
@roles_required(Role.VOLUNTEER)
def decline_swap(swap_id):
    swap = ShiftSwapRequest.query.get_or_404(swap_id)
    if swap.target_shift_request.volunteer_id != current_user.id:
        abort(403)
    if swap.status != ShiftSwapStatus.PENDING:
        flash("That swap proposal is no longer pending.", "warning")
        return redirect(url_for("calendar.swaps"))

    swap.status = ShiftSwapStatus.DECLINED
    swap.decided_at = datetime.utcnow()
    db.session.commit()
    flash("Swap declined.", "info")
    return redirect(url_for("calendar.swaps"))


@calendar_bp.route("/swaps/<int:swap_id>/cancel", methods=["POST"])
@login_required
@roles_required(Role.VOLUNTEER)
def cancel_swap(swap_id):
    swap = ShiftSwapRequest.query.get_or_404(swap_id)
    if swap.requester_id != current_user.id:
        abort(403)
    if swap.status != ShiftSwapStatus.PENDING:
        flash("That swap proposal is no longer pending.", "warning")
        return redirect(url_for("calendar.swaps"))

    swap.status = ShiftSwapStatus.CANCELLED
    swap.decided_at = datetime.utcnow()
    db.session.commit()
    flash("Swap proposal cancelled.", "info")
    return redirect(url_for("calendar.swaps"))
