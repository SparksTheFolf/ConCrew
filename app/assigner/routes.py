from datetime import datetime

from flask import Blueprint, render_template, redirect, url_for, flash, request, abort
from flask_login import login_required, current_user

from app.extensions import db
from app.models import (
    User, Role, Department, HourEntry, HourEntryStatus, AuditLog, sync_reward_claims,
    ShiftRequest, ShiftRequestStatus
)
from app.utils import roles_required, get_current_con_year

assigner_bp = Blueprint("assigner", __name__)


def _managed_department_ids():
    if current_user.role == Role.ADMIN:
        return [d.id for d in Department.query.all()]
    return [link.department_id for link in current_user.department_links]


@assigner_bp.route("/")
@login_required
@roles_required(Role.ADMIN, Role.ASSIGNER, Role.CONSTORE)
def dashboard():
    con_year = get_current_con_year()
    dept_ids = _managed_department_ids()

    volunteers = User.query.filter_by(role=Role.VOLUNTEER, active=True).order_by(User.badge_name).all()
    rows = []
    if con_year:
        for v in volunteers:
            hours = v.approved_hours(con_year.id)
            tier = v.current_tier(con_year.id)
            rows.append({"user": v, "hours": hours, "tier": tier})
        rows.sort(key=lambda r: r["hours"], reverse=True)

    pending = (
        HourEntry.query.filter(
            HourEntry.status == HourEntryStatus.PENDING,
            HourEntry.department_id.in_(dept_ids) if dept_ids else False,
        ).order_by(HourEntry.work_date).all()
        if current_user.role != Role.CONSTORE else []
    )

    pending_shift_requests = []
    if current_user.role != Role.CONSTORE and dept_ids:
        pending_shift_requests = [
            r for r in ShiftRequest.query.filter_by(status=ShiftRequestStatus.REQUESTED).all()
            if r.shift_slot.department_id in dept_ids
        ]

    departments = Department.query.filter(Department.id.in_(dept_ids)).all() if dept_ids else Department.query.all()

    return render_template(
        "assigner/dashboard.html",
        rows=rows,
        pending=pending,
        pending_shift_requests=pending_shift_requests,
        departments=departments,
        con_year=con_year,
    )


@assigner_bp.route("/quick-log/<usercode>")
@login_required
@roles_required(Role.ADMIN, Role.ASSIGNER)
def quick_log(usercode):
    """Landing point for a scanned volunteer QR code -- jumps straight to
    the log-hours form with that volunteer preselected."""
    volunteer = User.query.filter_by(usercode=usercode.upper(), role=Role.VOLUNTEER).first()
    if not volunteer:
        flash(f"No volunteer found for code {usercode}.", "danger")
        return redirect(url_for("assigner.log_hours"))
    return redirect(url_for("assigner.log_hours", volunteer_id=volunteer.id))


@assigner_bp.route("/log-hours", methods=["GET", "POST"])
@login_required
@roles_required(Role.ADMIN, Role.ASSIGNER)
def log_hours():
    con_year = get_current_con_year()
    dept_ids = _managed_department_ids()
    departments = Department.query.filter(Department.id.in_(dept_ids)).all() if dept_ids else Department.query.all()
    volunteers = User.query.filter_by(role=Role.VOLUNTEER, active=True).order_by(User.badge_name).all()
    preselect_volunteer_id = request.args.get("volunteer_id", type=int)
    preselect_department_id = request.args.get("department_id", type=int)
    prefill_work_date = request.args.get("work_date", type=str)
    prefill_hours = request.args.get("hours", type=float)
    prefill_task_description = request.args.get("task_description", type=str)

    if request.method == "POST":
        volunteer_id = int(request.form["volunteer_id"])
        department_id = int(request.form["department_id"])
        work_date = datetime.strptime(request.form["work_date"], "%Y-%m-%d").date()
        hours = float(request.form["hours"])
        task_description = request.form.get("task_description", "").strip()

        if department_id not in dept_ids and current_user.role != Role.ADMIN:
            abort(403)
        if not con_year:
            flash("No active con year is set. Ask an admin to mark one current.", "danger")
            return redirect(url_for("assigner.log_hours"))

        entry = HourEntry(
            volunteer_id=volunteer_id,
            con_year_id=con_year.id,
            department_id=department_id,
            work_date=work_date,
            hours=hours,
            task_description=task_description or None,
            status=HourEntryStatus.PENDING,
            logged_by_id=current_user.id,
        )
        db.session.add(entry)
        db.session.commit()
        flash("Hours logged, pending approval.", "success")
        return redirect(url_for("assigner.log_hours"))

    return render_template(
        "assigner/log_hours.html",
        departments=departments,
        volunteers=volunteers,
        con_year=con_year,
        preselect_volunteer_id=preselect_volunteer_id,
        preselect_department_id=preselect_department_id,
        prefill_work_date=prefill_work_date,
        prefill_hours=prefill_hours,
        prefill_task_description=prefill_task_description,
    )


@assigner_bp.route("/hours/<int:entry_id>/approve", methods=["POST"])
@login_required
@roles_required(Role.ADMIN, Role.ASSIGNER)
def approve_hours(entry_id):
    entry = HourEntry.query.get_or_404(entry_id)
    dept_ids = _managed_department_ids()
    if entry.department_id not in dept_ids and current_user.role != Role.ADMIN:
        abort(403)

    entry.status = HourEntryStatus.APPROVED
    entry.approved_by_id = current_user.id
    entry.approved_at = datetime.utcnow()
    db.session.add(AuditLog(
        actor_id=current_user.id, action="approve_hours",
        target_type="HourEntry", target_id=entry.id,
        detail=f"{entry.hours}h for volunteer {entry.volunteer_id}"
    ))
    db.session.commit()

    if entry.con_year:
        sync_reward_claims(entry.volunteer, entry.con_year)

    flash("Hours approved.", "success")
    return redirect(request.referrer or url_for("assigner.dashboard"))


@assigner_bp.route("/hours/<int:entry_id>/reject", methods=["POST"])
@login_required
@roles_required(Role.ADMIN, Role.ASSIGNER)
def reject_hours(entry_id):
    entry = HourEntry.query.get_or_404(entry_id)
    dept_ids = _managed_department_ids()
    if entry.department_id not in dept_ids and current_user.role != Role.ADMIN:
        abort(403)

    entry.status = HourEntryStatus.REJECTED
    entry.approved_by_id = current_user.id
    entry.approved_at = datetime.utcnow()
    db.session.add(AuditLog(
        actor_id=current_user.id, action="reject_hours",
        target_type="HourEntry", target_id=entry.id,
        detail=f"{entry.hours}h for volunteer {entry.volunteer_id}"
    ))
    db.session.commit()
    flash("Hours rejected.", "info")
    return redirect(request.referrer or url_for("assigner.dashboard"))
