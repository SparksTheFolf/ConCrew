import csv
import io
from datetime import datetime

from flask import Blueprint, render_template, redirect, url_for, flash, request, Response, abort
from flask_login import login_required, current_user

from app.extensions import db
from app.models import (
    User, Role, ConYear, Department, RewardTier, RewardItem, RewardClaim,
    HourEntry, HourEntryStatus, ClaimStatus, AuditLog, AssignerDepartment, sync_reward_claims,
    ShiftRequest, ShiftRequestStatus, ShiftSlot, AvailabilityBlock, VolunteerDepartmentEligibility
)
from app.utils import (
    roles_required, get_current_con_year, generate_random_password, send_mass_email,
    send_pending_shift_reminders,
)

admin_bp = Blueprint("admin", __name__)


@admin_bp.route("/")
@login_required
@roles_required(Role.ADMIN)
def dashboard():
    con_year = get_current_con_year()
    volunteers = User.query.filter_by(role=Role.VOLUNTEER, active=True).order_by(User.badge_name).all()

    rows = []
    if con_year:
        for v in volunteers:
            hours = v.approved_hours(con_year.id)
            tier = v.current_tier(con_year.id)
            next_tier = v.next_tier(con_year.id)
            remaining = round(next_tier.threshold_hours - hours, 2) if next_tier else 0
            rows.append({
                "user": v,
                "hours": hours,
                "tier": tier,
                "next_tier": next_tier,
                "remaining": remaining,
            })
        rows.sort(key=lambda r: r["hours"], reverse=True)

    pending_count = HourEntry.query.filter_by(status=HourEntryStatus.PENDING).count()
    eligible_unclaimed = RewardClaim.query.filter_by(status=ClaimStatus.ELIGIBLE).count()
    pending_shift_requests = ShiftRequest.query.filter(
        ShiftRequest.status.in_([ShiftRequestStatus.REQUESTED, ShiftRequestStatus.LEAVE_REQUESTED])
    ).count()

    no_show_count = 0
    in_progress_requests = []
    upcoming_requests = []
    if con_year:
        now = datetime.now()
        approved = (
            ShiftRequest.query
            .join(ShiftSlot, ShiftRequest.shift_slot_id == ShiftSlot.id)
            .filter(ShiftRequest.status == ShiftRequestStatus.APPROVED, ShiftSlot.con_year_id == con_year.id)
            .all()
        )
        no_show_count = sum(1 for r in approved if r.is_no_show())
        upcoming_requests = sorted(
            (r for r in approved if r.scheduled_start() > now),
            key=lambda r: r.scheduled_start(),
        )[:5]

        in_progress_requests = (
            ShiftRequest.query
            .join(ShiftSlot, ShiftRequest.shift_slot_id == ShiftSlot.id)
            .filter(ShiftRequest.status == ShiftRequestStatus.IN_PROGRESS, ShiftSlot.con_year_id == con_year.id)
            .all()
        )
        in_progress_requests.sort(key=lambda r: r.checked_in_at or datetime.min)

    return render_template(
        "admin/dashboard.html",
        con_year=con_year,
        rows=rows,
        pending_count=pending_count,
        eligible_unclaimed=eligible_unclaimed,
        pending_shift_requests=pending_shift_requests,
        no_show_count=no_show_count,
        in_progress_requests=in_progress_requests,
        upcoming_requests=upcoming_requests,
    )


@admin_bp.route("/users")
@login_required
@roles_required(Role.ADMIN)
def users():
    all_users = User.query.order_by(User.role, User.badge_name).all()
    return render_template("admin/users.html", users=all_users)


@admin_bp.route("/mass-email", methods=["GET", "POST"])
@login_required
@roles_required(Role.ADMIN)
def mass_email():
    departments = Department.query.order_by(Department.name).all()
    all_users = User.query.filter_by(active=True).order_by(User.role, User.badge_name).all()

    if request.method == "POST":
        target_mode = request.form.get("target_mode", "")
        subject = request.form.get("subject", "").strip()
        body = request.form.get("body", "").strip()

        if not subject or not body:
            flash("Subject and message body are required.", "danger")
            return redirect(url_for("admin.mass_email"))

        recipients = []
        description = ""

        if target_mode == "roles":
            roles = [r for r in request.form.getlist("roles") if r in Role.ALL]
            if not roles:
                flash("Select at least one role.", "danger")
                return redirect(url_for("admin.mass_email"))
            recipients = User.query.filter(User.role.in_(roles), User.active.is_(True)).all()
            description = f"roles={','.join(roles)}"

        elif target_mode == "departments":
            dept_ids = []
            for raw in request.form.getlist("department_ids"):
                try:
                    dept_ids.append(int(raw))
                except ValueError:
                    continue
            if not dept_ids:
                flash("Select at least one department.", "danger")
                return redirect(url_for("admin.mass_email"))

            volunteer_ids = {
                link.volunteer_id for link in
                VolunteerDepartmentEligibility.query.filter(
                    VolunteerDepartmentEligibility.department_id.in_(dept_ids)
                ).all()
            }
            assigner_ids = {
                link.assigner_id for link in
                AssignerDepartment.query.filter(
                    AssignerDepartment.department_id.in_(dept_ids)
                ).all()
            }
            user_ids = volunteer_ids | assigner_ids
            recipients = User.query.filter(User.id.in_(user_ids), User.active.is_(True)).all() if user_ids else []
            dept_names = [d.name for d in Department.query.filter(Department.id.in_(dept_ids)).all()]
            description = f"departments={','.join(dept_names)}"

        elif target_mode == "users":
            user_ids = []
            for raw in request.form.getlist("user_ids"):
                try:
                    user_ids.append(int(raw))
                except ValueError:
                    continue
            if not user_ids:
                flash("Select at least one user.", "danger")
                return redirect(url_for("admin.mass_email"))
            recipients = User.query.filter(User.id.in_(user_ids), User.active.is_(True)).all()
            description = f"user_ids={','.join(str(i) for i in user_ids)}"

        else:
            flash("Choose who to send this email to.", "danger")
            return redirect(url_for("admin.mass_email"))

        recipients_with_email = [u for u in recipients if u.email]
        skipped_no_email = len(recipients) - len(recipients_with_email)

        if not recipients_with_email:
            flash("No selected recipients have an email address on file.", "warning")
            return redirect(url_for("admin.mass_email"))

        sent, failed = send_mass_email(recipients_with_email, subject, body)

        db.session.add(AuditLog(
            actor_id=current_user.id,
            action="mass_email",
            target_type="User",
            target_id=0,
            detail=f"subject={subject!r}; {description}; sent={len(sent)}; failed={len(failed)}; no_email={skipped_no_email}",
        ))
        db.session.commit()

        if sent:
            flash(f"Sent to {len(sent)} recipient(s).", "success")
        if failed:
            flash(
                f"Failed to send to {len(failed)} recipient(s): " + ", ".join(u.badge_name for u, _ in failed),
                "warning",
            )
        if skipped_no_email:
            flash(f"Skipped {skipped_no_email} selected user(s) with no email on file.", "warning")

        return redirect(url_for("admin.mass_email"))

    return render_template(
        "admin/mass_email.html",
        departments=departments,
        roles=Role.ALL,
        all_users=all_users,
    )


def _selected_user_ids_from_form():
    selected = []
    for raw in request.form.getlist("user_ids"):
        try:
            selected.append(int(raw))
        except ValueError:
            continue
    return sorted(set(selected))


def _delete_volunteer_data_and_account(user):
    """Hard-delete a volunteer account plus volunteer-owned data rows.
    Returns (ok: bool, reason: str)."""
    if user.role != Role.VOLUNTEER:
        return False, "not_volunteer"

    # Guard against deleting accounts that acted as staff/system actors.
    if AuditLog.query.filter_by(actor_id=user.id).first():
        return False, "has_audit_actor_refs"
    if HourEntry.query.filter_by(logged_by_id=user.id).first():
        return False, "has_logged_by_refs"
    if ShiftSlot.query.filter_by(created_by_id=user.id).first():
        return False, "has_shift_creator_refs"
    if AssignerDepartment.query.filter_by(assigner_id=user.id).first():
        return False, "has_assigner_refs"

    # Null out optional references to this user from other rows.
    HourEntry.query.filter_by(approved_by_id=user.id).update({HourEntry.approved_by_id: None})
    RewardClaim.query.filter_by(fulfilled_by_id=user.id).update({RewardClaim.fulfilled_by_id: None})
    ShiftRequest.query.filter_by(decided_by_id=user.id).update({ShiftRequest.decided_by_id: None})
    VolunteerDepartmentEligibility.query.filter_by(granted_by_id=user.id).update(
        {VolunteerDepartmentEligibility.granted_by_id: None}
    )
    User.query.filter_by(report_to_id=user.id).update({User.report_to_id: None})

    # Delete rows owned by this volunteer account.
    HourEntry.query.filter_by(volunteer_id=user.id).delete()
    RewardClaim.query.filter_by(volunteer_id=user.id).delete()
    ShiftRequest.query.filter_by(volunteer_id=user.id).delete()
    AvailabilityBlock.query.filter_by(volunteer_id=user.id).delete()
    VolunteerDepartmentEligibility.query.filter_by(volunteer_id=user.id).delete()

    db.session.delete(user)
    return True, "deleted"


@admin_bp.route("/users/bulk-export.csv", methods=["POST"])
@login_required
@roles_required(Role.ADMIN)
def bulk_export_users_csv():
    user_ids = _selected_user_ids_from_form()
    if not user_ids:
        flash("Select at least one user to export.", "warning")
        return redirect(url_for("admin.users"))

    users = User.query.filter(User.id.in_(user_ids)).order_by(User.badge_name).all()
    con_year = get_current_con_year()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "User ID", "Badge Name", "Full Name", "Role", "Email", "Username",
        "Volunteer Code", "Active", "Report To", "Created At",
        "Approved Hours (Current Con)", "Hour Entries", "Reward Claims",
        "Shift Requests", "Availability Blocks",
    ])

    for u in users:
        approved_hours = u.approved_hours(con_year.id) if con_year and u.role == Role.VOLUNTEER else ""
        writer.writerow([
            u.id,
            u.badge_name,
            u.full_name,
            u.role,
            u.email or "",
            u.username or "",
            u.usercode,
            "yes" if u.active else "no",
            u.report_to.badge_name if u.report_to else "",
            u.created_at.isoformat() if u.created_at else "",
            approved_hours,
            HourEntry.query.filter_by(volunteer_id=u.id).count(),
            RewardClaim.query.filter_by(volunteer_id=u.id).count(),
            ShiftRequest.query.filter_by(volunteer_id=u.id).count(),
            AvailabilityBlock.query.filter_by(volunteer_id=u.id).count(),
        ])

    db.session.add(AuditLog(
        actor_id=current_user.id,
        action="bulk_export_users",
        target_type="User",
        target_id=0,
        detail=f"exported_ids={','.join(str(i) for i in user_ids)}",
    ))
    db.session.commit()

    filename = f"volunteer_user_data_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@admin_bp.route("/users/bulk-delete", methods=["POST"])
@login_required
@roles_required(Role.ADMIN)
def bulk_delete_users():
    user_ids = _selected_user_ids_from_form()
    if not user_ids:
        flash("Select at least one user to delete.", "warning")
        return redirect(url_for("admin.users"))

    users = User.query.filter(User.id.in_(user_ids)).all()
    deleted = []
    skipped = []

    for user in users:
        if user.id == current_user.id:
            skipped.append(f"{user.badge_name} (current user)")
            continue

        ok, reason = _delete_volunteer_data_and_account(user)
        if ok:
            deleted.append(user.badge_name)
            continue

        if reason == "not_volunteer":
            skipped.append(f"{user.badge_name} (not volunteer)")
        elif reason == "has_audit_actor_refs":
            skipped.append(f"{user.badge_name} (has audit history as actor)")
        elif reason == "has_logged_by_refs":
            skipped.append(f"{user.badge_name} (logged staff hour entries)")
        elif reason == "has_shift_creator_refs":
            skipped.append(f"{user.badge_name} (created shift slots)")
        elif reason == "has_assigner_refs":
            skipped.append(f"{user.badge_name} (assigner department links)")
        else:
            skipped.append(f"{user.badge_name} (unknown reason)")

    db.session.add(AuditLog(
        actor_id=current_user.id,
        action="bulk_delete_volunteer_data",
        target_type="User",
        target_id=0,
        detail=f"deleted={','.join(deleted) if deleted else 'none'}",
    ))
    db.session.commit()

    if deleted:
        flash(f"Deleted {len(deleted)} volunteer account(s): {', '.join(deleted)}", "success")
    if skipped:
        flash(f"Skipped {len(skipped)} account(s): {', '.join(skipped)}", "warning")
    if not deleted and not skipped:
        flash("No matching users found for the selected IDs.", "info")

    return redirect(url_for("admin.users"))


@admin_bp.route("/users/new", methods=["GET", "POST"])
@login_required
@roles_required(Role.ADMIN)
def new_user():
    departments = Department.query.order_by(Department.name).all()
    report_to_options = User.query.filter(
        User.active.is_(True),
        User.role.in_([Role.ADMIN, Role.ASSIGNER, Role.CONSTORE]),
    ).order_by(User.badge_name).all()
    generated_credentials = None

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        badge_name = request.form.get("badge_name", "").strip()
        role = request.form.get("role", Role.VOLUNTEER)
        report_to_id_raw = request.form.get("report_to_id", "").strip()
        account_type = request.form.get("account_type", "email")  # "email" or "kiosk"
        dept_ids = request.form.getlist("department_ids")

        if not full_name or not badge_name or role not in Role.ALL:
            flash("Name, badge name, and a valid role are required.", "danger")
            return redirect(url_for("admin.new_user"))

        report_to_id = None
        if role == Role.VOLUNTEER:
            if not report_to_id_raw:
                flash("A 'Report To' person is required for new volunteers.", "danger")
                return redirect(url_for("admin.new_user"))
            try:
                report_to_id = int(report_to_id_raw)
            except ValueError:
                flash("Invalid 'Report To' selection.", "danger")
                return redirect(url_for("admin.new_user"))

            report_to_user = User.query.filter(
                User.id == report_to_id,
                User.active.is_(True),
                User.role.in_([Role.ADMIN, Role.ASSIGNER, Role.CONSTORE]),
            ).first()
            if not report_to_user:
                flash("Selected 'Report To' person is not valid.", "danger")
                return redirect(url_for("admin.new_user"))

        user = User(full_name=full_name, badge_name=badge_name, role=role, report_to_id=report_to_id)

        if account_type == "kiosk":
            # No real email required -- generate a username + password for
            # kiosk-style login (e.g. a shared check-in tablet).
            base = "".join(c for c in badge_name.lower() if c.isalnum()) or "volunteer"
            username = base
            suffix = 1
            while User.query.filter_by(username=username).first():
                suffix += 1
                username = f"{base}{suffix}"
            password = generate_random_password()
            user.username = username
            user.is_kiosk_account = True
            user.set_password(password)
            generated_credentials = {"username": username, "password": password}
        else:
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            if not email or not password:
                flash("Email and password are required for a standard account.", "danger")
                return redirect(url_for("admin.new_user"))
            if len(password) < 8:
                flash("Password must be at least 8 characters.", "danger")
                return redirect(url_for("admin.new_user"))
            if User.query.filter_by(email=email).first():
                flash("That email is already in use.", "danger")
                return redirect(url_for("admin.new_user"))
            user.email = email
            user.set_password(password)

        db.session.add(user)
        db.session.commit()

        if role == Role.ASSIGNER:
            for dept_id in dept_ids:
                db.session.add(AssignerDepartment(assigner_id=user.id, department_id=int(dept_id)))
            db.session.commit()

        db.session.add(AuditLog(
            actor_id=current_user.id, action="create_user",
            target_type="User", target_id=user.id,
            detail=f"{user.badge_name} ({role})"
        ))
        db.session.commit()

        if generated_credentials:
            return render_template(
                "admin/user_created.html", user=user, credentials=generated_credentials
            )

        flash(f"Created account for {user.badge_name}.", "success")
        return redirect(url_for("admin.users"))

    return render_template(
        "admin/new_user.html",
        departments=departments,
        roles=Role.ALL,
        report_to_options=report_to_options,
    )


@admin_bp.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@login_required
@roles_required(Role.ADMIN)
def edit_user(user_id):
    user = User.query.get_or_404(user_id)
    departments = Department.query.order_by(Department.name).all()
    report_to_options = User.query.filter(
        User.active.is_(True),
        User.role.in_([Role.ADMIN, Role.ASSIGNER, Role.CONSTORE]),
        User.id != user.id,
    ).order_by(User.badge_name).all()
    current_dept_ids = {link.department_id for link in user.department_links}

    if request.method == "POST":
        role = request.form.get("role", user.role)
        report_to_id_raw = request.form.get("report_to_id", "").strip()
        dept_ids = request.form.getlist("department_ids")

        if role not in Role.ALL:
            flash("Invalid role.", "danger")
            return redirect(url_for("admin.edit_user", user_id=user.id))

        if user.role == Role.ADMIN and role != Role.ADMIN:
            active_admins = User.query.filter_by(role=Role.ADMIN, active=True).count()
            if active_admins <= 1:
                flash("Can't change the role of the last active admin.", "danger")
                return redirect(url_for("admin.edit_user", user_id=user.id))

        report_to_id = None
        if role == Role.VOLUNTEER:
            if not report_to_id_raw:
                flash("A 'Report To' person is required for volunteers.", "danger")
                return redirect(url_for("admin.edit_user", user_id=user.id))
            try:
                report_to_id = int(report_to_id_raw)
            except ValueError:
                flash("Invalid 'Report To' selection.", "danger")
                return redirect(url_for("admin.edit_user", user_id=user.id))
            if report_to_id == user.id:
                flash("A user can't report to themselves.", "danger")
                return redirect(url_for("admin.edit_user", user_id=user.id))

            report_to_user = User.query.filter(
                User.id == report_to_id,
                User.active.is_(True),
                User.role.in_([Role.ADMIN, Role.ASSIGNER, Role.CONSTORE]),
            ).first()
            if not report_to_user:
                flash("Selected 'Report To' person is not valid.", "danger")
                return redirect(url_for("admin.edit_user", user_id=user.id))

        old_role = user.role
        user.role = role
        user.report_to_id = report_to_id

        # Anyone who reported to this user needs to be reassigned by the admin
        # once this user is no longer a valid supervisor role.
        if role not in (Role.ADMIN, Role.ASSIGNER, Role.CONSTORE):
            User.query.filter_by(report_to_id=user.id).update({User.report_to_id: None})

        if role == Role.ASSIGNER:
            AssignerDepartment.query.filter_by(assigner_id=user.id).delete()
            for dept_id in dept_ids:
                db.session.add(AssignerDepartment(assigner_id=user.id, department_id=int(dept_id)))
        elif old_role == Role.ASSIGNER:
            AssignerDepartment.query.filter_by(assigner_id=user.id).delete()

        db.session.add(AuditLog(
            actor_id=current_user.id, action="edit_user",
            target_type="User", target_id=user.id,
            detail=f"{user.badge_name}: role {old_role} -> {role}"
        ))
        db.session.commit()

        flash(f"Updated {user.badge_name}.", "success")
        return redirect(url_for("admin.users"))

    return render_template(
        "admin/edit_user.html",
        user=user,
        departments=departments,
        roles=Role.ALL,
        report_to_options=report_to_options,
        current_dept_ids=current_dept_ids,
    )


@admin_bp.route("/convention", methods=["GET", "POST"])
@login_required
@roles_required(Role.ADMIN)
def convention_settings():
    con_years = ConYear.query.order_by(ConYear.label.desc()).all()
    current_con = get_current_con_year()

    if request.method == "POST":
        action = request.form.get("action", "").strip()

        if action == "update_current_con":
            con_year_id = request.form.get("con_year_id", type=int)
            con_year = ConYear.query.get_or_404(con_year_id)

            con_name = request.form.get("con_name", "").strip()
            label = request.form.get("label", "").strip()
            contact_email = request.form.get("contact_email", "").strip() or None
            start_date_raw = request.form.get("start_date", "").strip()
            end_date_raw = request.form.get("end_date", "").strip()

            if not con_name or not label:
                flash("Con name and year label are required.", "danger")
                return redirect(url_for("admin.convention_settings"))

            existing_label = ConYear.query.filter(ConYear.label == label, ConYear.id != con_year.id).first()
            if existing_label:
                flash("Year label must be unique.", "danger")
                return redirect(url_for("admin.convention_settings"))

            try:
                start_date = datetime.strptime(start_date_raw, "%Y-%m-%d").date() if start_date_raw else None
                end_date = datetime.strptime(end_date_raw, "%Y-%m-%d").date() if end_date_raw else None
            except ValueError:
                flash("Invalid date format.", "danger")
                return redirect(url_for("admin.convention_settings"))

            if start_date and end_date and end_date < start_date:
                flash("End date cannot be before start date.", "danger")
                return redirect(url_for("admin.convention_settings"))

            con_year.con_name = con_name
            con_year.label = label
            con_year.contact_email = contact_email
            con_year.start_date = start_date
            con_year.end_date = end_date

            db.session.add(AuditLog(
                actor_id=current_user.id,
                action="update_convention_info",
                target_type="ConYear",
                target_id=con_year.id,
                detail=f"Updated {con_year.label}",
            ))
            db.session.commit()
            flash("Convention info updated.", "success")
            return redirect(url_for("admin.convention_settings"))

        if action == "switch_current_con":
            selected_id = request.form.get("current_con_year_id", type=int)
            selected = ConYear.query.get_or_404(selected_id)

            ConYear.query.update({ConYear.is_current: False})
            selected.is_current = True

            db.session.add(AuditLog(
                actor_id=current_user.id,
                action="switch_current_convention",
                target_type="ConYear",
                target_id=selected.id,
                detail=f"Set current: {selected.label}",
            ))
            db.session.commit()
            flash(f"Current convention set to {selected.label}.", "success")
            return redirect(url_for("admin.convention_settings"))

        if action == "create_con_year":
            con_name = request.form.get("new_con_name", "").strip()
            label = request.form.get("new_label", "").strip()
            contact_email = request.form.get("new_contact_email", "").strip() or None
            start_date_raw = request.form.get("new_start_date", "").strip()
            end_date_raw = request.form.get("new_end_date", "").strip()

            if not con_name or not label:
                flash("New convention requires name and label.", "danger")
                return redirect(url_for("admin.convention_settings"))
            if ConYear.query.filter_by(label=label).first():
                flash("That label already exists.", "danger")
                return redirect(url_for("admin.convention_settings"))

            try:
                start_date = datetime.strptime(start_date_raw, "%Y-%m-%d").date() if start_date_raw else None
                end_date = datetime.strptime(end_date_raw, "%Y-%m-%d").date() if end_date_raw else None
            except ValueError:
                flash("Invalid date format for new convention.", "danger")
                return redirect(url_for("admin.convention_settings"))

            if start_date and end_date and end_date < start_date:
                flash("New convention end date cannot be before start date.", "danger")
                return redirect(url_for("admin.convention_settings"))

            con = ConYear(
                con_name=con_name,
                label=label,
                contact_email=contact_email,
                start_date=start_date,
                end_date=end_date,
                is_current=False,
            )
            db.session.add(con)
            db.session.flush()
            db.session.add(AuditLog(
                actor_id=current_user.id,
                action="create_convention",
                target_type="ConYear",
                target_id=con.id,
                detail=f"Created {label}",
            ))
            db.session.commit()
            flash("Convention year created.", "success")
            return redirect(url_for("admin.convention_settings"))

        if action == "update_departments":
            departments = Department.query.order_by(Department.name).all()
            deleted_count = 0

            for dept in departments:
                delete_flag = request.form.get(f"delete_department_{dept.id}") == "on"
                if delete_flag:
                    has_hours = HourEntry.query.filter_by(department_id=dept.id).first() is not None
                    has_slots = ShiftSlot.query.filter_by(department_id=dept.id).first() is not None
                    has_assigners = AssignerDepartment.query.filter_by(department_id=dept.id).first() is not None
                    has_eligibility = VolunteerDepartmentEligibility.query.filter_by(department_id=dept.id).first() is not None

                    if has_hours or has_slots or has_assigners or has_eligibility:
                        flash(f"Cannot delete department '{dept.name}' because it is in use.", "warning")
                        continue

                    db.session.delete(dept)
                    deleted_count += 1
                    continue

                new_name = request.form.get(f"department_name_{dept.id}", "").strip()
                if not new_name:
                    flash("Department name cannot be blank.", "danger")
                    db.session.rollback()
                    return redirect(url_for("admin.convention_settings"))
                dept.name = new_name

            new_departments_raw = request.form.get("new_departments", "").strip()
            if new_departments_raw:
                existing_names = {d.name.lower() for d in Department.query.all()}
                for line in new_departments_raw.splitlines():
                    name = line.strip()
                    if not name:
                        continue
                    if name.lower() in existing_names:
                        flash(f"Department '{name}' already exists.", "warning")
                        continue
                    db.session.add(Department(name=name))
                    existing_names.add(name.lower())

            db.session.add(AuditLog(
                actor_id=current_user.id,
                action="update_departments",
                target_type="Department",
                target_id=0,
                detail=f"Updated departments; deleted={deleted_count}",
            ))
            db.session.commit()
            flash("Departments updated.", "success")
            return redirect(url_for("admin.convention_settings"))

        flash("Unknown settings action.", "danger")
        return redirect(url_for("admin.convention_settings"))

    departments = Department.query.order_by(Department.name).all()
    return render_template(
        "admin/convention_settings.html",
        con_years=con_years,
        current_con=current_con,
        departments=departments,
    )


@admin_bp.route("/hours/pending")
@login_required
@roles_required(Role.ADMIN)
def pending_hours():
    entries = HourEntry.query.filter_by(status=HourEntryStatus.PENDING).order_by(HourEntry.work_date).all()
    return render_template("admin/pending_hours.html", entries=entries)


@admin_bp.route("/claims")
@login_required
@roles_required(Role.ADMIN, Role.ASSIGNER, Role.CONSTORE)
def claims_matrix():
    """Fulfillment visibility, gated behind a name search so a table full of
    every volunteer's claims doesn't dump onto the screen at once."""
    con_year = get_current_con_year()
    status_filter = request.args.get("status")
    search_query = request.args.get("q", "").strip()

    claims = []
    if search_query:
        query = RewardClaim.query.join(User, RewardClaim.volunteer_id == User.id).filter(
            db.or_(
                User.badge_name.ilike(f"%{search_query}%"),
                User.full_name.ilike(f"%{search_query}%"),
                User.usercode.ilike(f"%{search_query}%"),
            )
        )
        if con_year:
            query = query.filter(RewardClaim.con_year_id == con_year.id)
        if status_filter:
            query = query.filter(RewardClaim.status == status_filter)
        claims = query.order_by(User.badge_name).all()

    return render_template(
        "admin/claims.html", claims=claims, con_year=con_year,
        status_filter=status_filter, search_query=search_query,
    )


@admin_bp.route("/tiers")
@login_required
@roles_required(Role.ADMIN)
def tiers():
    all_tiers = RewardTier.query.order_by(RewardTier.threshold_hours).all()
    return render_template("admin/tiers.html", tiers=all_tiers)


@admin_bp.route("/tiers/update", methods=["POST"])
@login_required
@roles_required(Role.ADMIN)
def update_tiers():
    all_tiers = RewardTier.query.order_by(RewardTier.threshold_hours).all()
    submitted_thresholds = []
    kept_tiers = []
    deleted_tier_count = 0

    for tier in all_tiers:
        delete_tier = request.form.get(f"delete_tier_{tier.id}") == "on"
        if delete_tier:
            existing_claim = (
                RewardClaim.query
                .join(RewardItem, RewardClaim.reward_item_id == RewardItem.id)
                .filter(RewardItem.tier_id == tier.id)
                .first()
            )
            if existing_claim:
                flash(
                    f"Cannot delete tier '{tier.name}' because claims already exist for one or more of its items.",
                    "warning",
                )
            else:
                for item in tier.items:
                    db.session.delete(item)
                db.session.delete(tier)
                deleted_tier_count += 1
                continue

        name = request.form.get(f"tier_name_{tier.id}", "").strip()
        threshold_raw = request.form.get(f"tier_threshold_{tier.id}", "").strip()

        if not name:
            flash("Tier name cannot be blank.", "danger")
            return redirect(url_for("admin.tiers"))

        try:
            threshold = float(threshold_raw)
        except (TypeError, ValueError):
            flash(f"Threshold for {tier.name} must be a number.", "danger")
            return redirect(url_for("admin.tiers"))

        if threshold < 0:
            flash(f"Threshold for {tier.name} cannot be negative.", "danger")
            return redirect(url_for("admin.tiers"))

        submitted_thresholds.append((tier.id, threshold))
        kept_tiers.append(tier)
        tier.name = name
        tier.threshold_hours = threshold

    new_tier_name = request.form.get("new_tier_name", "").strip()
    new_tier_threshold_raw = request.form.get("new_tier_threshold", "").strip()
    new_tier_items_raw = request.form.get("new_tier_items", "").strip()

    new_tier_provided = bool(new_tier_name or new_tier_threshold_raw or new_tier_items_raw)
    if new_tier_provided:
        if not new_tier_name:
            flash("New tier name is required when adding a tier.", "danger")
            db.session.rollback()
            return redirect(url_for("admin.tiers"))

        try:
            new_tier_threshold = float(new_tier_threshold_raw)
        except (TypeError, ValueError):
            flash("New tier threshold must be a number.", "danger")
            db.session.rollback()
            return redirect(url_for("admin.tiers"))

        if new_tier_threshold < 0:
            flash("New tier threshold cannot be negative.", "danger")
            db.session.rollback()
            return redirect(url_for("admin.tiers"))

        submitted_thresholds.append((0, new_tier_threshold))

    threshold_values = [threshold for _, threshold in submitted_thresholds]
    if len(threshold_values) != len(set(threshold_values)):
        flash("Tier thresholds must be unique.", "danger")
        db.session.rollback()
        return redirect(url_for("admin.tiers"))

    for tier in kept_tiers:
        for item in tier.items:
            delete_flag = request.form.get(f"delete_item_{item.id}") == "on"
            if delete_flag:
                existing_claim = RewardClaim.query.filter_by(reward_item_id=item.id).first()
                if existing_claim:
                    flash(
                        f"Cannot delete item '{item.name}' because claims already exist for it.",
                        "warning",
                    )
                    continue
                db.session.delete(item)
                continue

            item_name = request.form.get(f"item_name_{item.id}", "").strip()
            item_desc = request.form.get(f"item_desc_{item.id}", "").strip() or None
            if not item_name:
                flash("Reward item name cannot be blank.", "danger")
                db.session.rollback()
                return redirect(url_for("admin.tiers"))
            item.name = item_name
            item.description = item_desc

        new_items_raw = request.form.get(f"new_items_{tier.id}", "").strip()
        if new_items_raw:
            for line in new_items_raw.splitlines():
                raw = line.strip()
                if not raw:
                    continue
                if "|" in raw:
                    name_part, desc_part = raw.split("|", 1)
                    item_name = name_part.strip()
                    item_desc = desc_part.strip() or None
                else:
                    item_name = raw
                    item_desc = None

                if not item_name:
                    flash("New reward item name cannot be blank.", "danger")
                    db.session.rollback()
                    return redirect(url_for("admin.tiers"))

                db.session.add(RewardItem(tier_id=tier.id, name=item_name, description=item_desc))

    if new_tier_provided:
        new_tier = RewardTier(name=new_tier_name, threshold_hours=new_tier_threshold)
        db.session.add(new_tier)
        db.session.flush()

        if new_tier_items_raw:
            for line in new_tier_items_raw.splitlines():
                raw = line.strip()
                if not raw:
                    continue
                if "|" in raw:
                    name_part, desc_part = raw.split("|", 1)
                    item_name = name_part.strip()
                    item_desc = desc_part.strip() or None
                else:
                    item_name = raw
                    item_desc = None

                if not item_name:
                    flash("New reward item name cannot be blank.", "danger")
                    db.session.rollback()
                    return redirect(url_for("admin.tiers"))

                db.session.add(RewardItem(tier_id=new_tier.id, name=item_name, description=item_desc))

    db.session.add(AuditLog(
        actor_id=current_user.id,
        action="update_reward_tiers",
        target_type="RewardTier",
        target_id=0,
        detail=f"Edited reward tiers/items; deleted_tiers={deleted_tier_count}; added_new_tier={new_tier_provided}",
    ))
    db.session.commit()

    con_year = get_current_con_year()
    if con_year:
        volunteers = User.query.filter_by(role=Role.VOLUNTEER, active=True).all()
        for volunteer in volunteers:
            sync_reward_claims(volunteer, con_year)

    flash("Reward tiers updated.", "success")
    return redirect(url_for("admin.tiers"))


@admin_bp.route("/reports/export.csv")
@login_required
@roles_required(Role.ADMIN)
def export_csv():
    con_year = get_current_con_year()
    volunteers = User.query.filter_by(role=Role.VOLUNTEER).order_by(User.badge_name).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Badge Name", "Full Name", "Email", "Approved Hours", "Current Tier", "Con Year"])
    for v in volunteers:
        hours = v.approved_hours(con_year.id) if con_year else 0
        tier = v.current_tier(con_year.id) if con_year else None
        writer.writerow([v.badge_name, v.full_name, v.email, hours, tier.name if tier else "", con_year.label if con_year else ""])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=volunteer_hours_export.csv"},
    )


@admin_bp.route("/history")
@login_required
@roles_required(Role.ADMIN, Role.ASSIGNER)
def history():
    search_query = request.args.get("q", "").strip()
    action_filter = request.args.get("action", "").strip()

    query = AuditLog.query.join(User, AuditLog.actor_id == User.id)

    if search_query:
        query = query.filter(
            db.or_(
                User.badge_name.ilike(f"%{search_query}%"),
                AuditLog.detail.ilike(f"%{search_query}%"),
            )
        )
    if action_filter:
        query = query.filter(AuditLog.action == action_filter)

    entries = query.order_by(AuditLog.created_at.desc()).limit(300).all()
    action_types = [row[0] for row in db.session.query(AuditLog.action).distinct().all()]

    return render_template(
        "admin/history.html", entries=entries, search_query=search_query,
        action_filter=action_filter, action_types=action_types,
    )


@admin_bp.route("/users/<int:user_id>/reset-password", methods=["POST"])
@login_required
@roles_required(Role.ADMIN)
def reset_password(user_id):
    user = User.query.get_or_404(user_id)
    new_password = generate_random_password()
    user.set_password(new_password)
    db.session.add(AuditLog(
        actor_id=current_user.id, action="reset_password",
        target_type="User", target_id=user.id, detail=f"{user.badge_name}"
    ))
    db.session.commit()
    return render_template(
        "admin/user_created.html", user=user,
        credentials={"username": user.username or user.email, "password": new_password},
        is_reset=True,
    )


@admin_bp.route("/users/<int:user_id>/deactivate", methods=["POST"])
@login_required
@roles_required(Role.ADMIN)
def deactivate_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash("You can't deactivate your own account.", "danger")
        return redirect(url_for("admin.users"))
    user.active = False
    db.session.add(AuditLog(
        actor_id=current_user.id, action="deactivate_user",
        target_type="User", target_id=user.id, detail=f"{user.badge_name}"
    ))
    db.session.commit()
    flash(f"{user.badge_name} deactivated -- they can no longer log in.", "info")
    return redirect(url_for("admin.users"))


@admin_bp.route("/users/<int:user_id>/activate", methods=["POST"])
@login_required
@roles_required(Role.ADMIN)
def activate_user(user_id):
    user = User.query.get_or_404(user_id)
    user.active = True
    db.session.add(AuditLog(
        actor_id=current_user.id, action="activate_user",
        target_type="User", target_id=user.id, detail=f"{user.badge_name}"
    ))
    db.session.commit()
    flash(f"{user.badge_name} reactivated.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.route("/users/<int:user_id>/delete", methods=["POST"])
@login_required
@roles_required(Role.ADMIN)
def delete_user(user_id):
    user = User.query.get_or_404(user_id)

    if user.id == current_user.id:
        flash("You can't delete your own account.", "danger")
        return redirect(url_for("admin.users"))

    if user.role == Role.ADMIN and User.query.filter_by(role=Role.ADMIN, active=True).count() <= 1:
        flash("Can't delete the last active admin account.", "danger")
        return redirect(url_for("admin.users"))

    if user.has_history():
        flash(
            f"{user.badge_name} has logged hours, claims, requests, or history on record -- "
            "deactivate instead of deleting to keep those records intact.",
            "danger",
        )
        return redirect(url_for("admin.users"))

    badge_name = user.badge_name
    User.query.filter_by(report_to_id=user.id).update({User.report_to_id: None})
    AssignerDepartment.query.filter_by(assigner_id=user.id).delete()
    VolunteerDepartmentEligibility.query.filter_by(volunteer_id=user.id).delete()
    db.session.delete(user)
    db.session.add(AuditLog(
        actor_id=current_user.id, action="delete_user",
        target_type="User", target_id=user_id, detail=badge_name
    ))
    db.session.commit()
    flash(f"{badge_name} deleted.", "info")
    return redirect(url_for("admin.users"))


@admin_bp.route("/eligibility", methods=["GET", "POST"])
@login_required
@roles_required(Role.ADMIN, Role.ASSIGNER)
def eligibility():
    """Matrix of volunteers x departments -- controls which open shifts a
    volunteer can see/request on the calendar."""
    managed_dept_ids = _managed_department_ids_for_admin()
    departments = Department.query.filter(Department.id.in_(managed_dept_ids)).order_by(Department.name).all() \
        if managed_dept_ids else []
    volunteers = User.query.filter_by(role=Role.VOLUNTEER, active=True).order_by(User.badge_name).all()

    if request.method == "POST":
        checked_pairs = set()
        for key in request.form:
            if key.startswith("elig_"):
                _, vol_id, dept_id = key.split("_")
                checked_pairs.add((int(vol_id), int(dept_id)))

        for v in volunteers:
            for d in departments:
                pair = (v.id, d.id)
                existing = VolunteerDepartmentEligibility.query.filter_by(
                    volunteer_id=v.id, department_id=d.id
                ).first()
                if pair in checked_pairs and not existing:
                    db.session.add(VolunteerDepartmentEligibility(
                        volunteer_id=v.id, department_id=d.id, granted_by_id=current_user.id
                    ))
                elif pair not in checked_pairs and existing:
                    db.session.delete(existing)
        db.session.commit()
        flash("Eligibility updated.", "success")
        return redirect(url_for("admin.eligibility"))

    current_pairs = {
        (link.volunteer_id, link.department_id)
        for link in VolunteerDepartmentEligibility.query.filter(
            VolunteerDepartmentEligibility.department_id.in_(managed_dept_ids)
        ).all()
    } if managed_dept_ids else set()

    return render_template(
        "admin/eligibility.html", departments=departments, volunteers=volunteers, current_pairs=current_pairs
    )


def _managed_department_ids_for_admin():
    if current_user.role == Role.ADMIN:
        return [d.id for d in Department.query.all()]
    return [link.department_id for link in current_user.department_links]


@admin_bp.route("/recalc/<int:user_id>", methods=["POST"])
@login_required
@roles_required(Role.ADMIN)
def recalc_claims(user_id):
    """Manually re-run claim eligibility sync for a single volunteer (useful
    after correcting hours)."""
    con_year = get_current_con_year()
    user = User.query.get_or_404(user_id)
    if con_year:
        created = sync_reward_claims(user, con_year)
        flash(f"Synced eligibility for {user.badge_name}: {len(created)} new claim(s).", "success")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/no-shows")
@login_required
@roles_required(Role.ADMIN, Role.ASSIGNER)
def no_shows():
    """Approved shifts that are over but were never checked into -- staff
    can review these and, if confirmed, mark them so the slot opens back up
    for reassignment."""
    con_year = get_current_con_year()
    managed_dept_ids = _managed_department_ids_for_admin()
    candidates = []

    if con_year and managed_dept_ids:
        now = datetime.now()
        approved = (
            ShiftRequest.query
            .join(ShiftSlot, ShiftRequest.shift_slot_id == ShiftSlot.id)
            .filter(
                ShiftRequest.status == ShiftRequestStatus.APPROVED,
                ShiftSlot.con_year_id == con_year.id,
                ShiftSlot.department_id.in_(managed_dept_ids),
            )
            .all()
        )
        candidates = [r for r in approved if r.is_no_show(now)]
        candidates.sort(key=lambda r: r.scheduled_end(), reverse=True)

    return render_template("admin/no_shows.html", con_year=con_year, requests=candidates)


@admin_bp.route("/requests/<int:request_id>/mark-no-show", methods=["POST"])
@login_required
@roles_required(Role.ADMIN, Role.ASSIGNER)
def mark_no_show(request_id):
    req = ShiftRequest.query.get_or_404(request_id)
    if current_user.role != Role.ADMIN and req.shift_slot.department_id not in _managed_department_ids_for_admin():
        abort(403)

    if not req.is_no_show():
        flash("That shift request is no longer eligible to be marked as a no-show.", "warning")
        return redirect(url_for("admin.no_shows"))

    req.status = ShiftRequestStatus.NO_SHOW
    req.decided_by_id = current_user.id
    req.decided_at = datetime.utcnow()
    db.session.add(AuditLog(
        actor_id=current_user.id, action="mark_no_show",
        target_type="ShiftRequest", target_id=req.id,
        detail=f"{req.volunteer.badge_name} -> {req.shift_slot.shift_date} {req.shift_slot.start_time}-{req.shift_slot.end_time}",
    ))
    db.session.commit()
    flash(f"Marked {req.volunteer.badge_name} as a no-show; the slot is open again.", "info")
    return redirect(url_for("admin.no_shows"))


@admin_bp.route("/shift-reminders/send", methods=["POST"])
@login_required
@roles_required(Role.ADMIN)
def send_shift_reminders():
    sent, failed, skipped_no_email = send_pending_shift_reminders()
    db.session.add(AuditLog(
        actor_id=current_user.id, action="send_shift_reminders",
        target_type="ShiftRequest", target_id=0,
        detail=f"sent={sent}; failed={failed}; skipped_no_email={skipped_no_email}",
    ))
    db.session.commit()

    if sent:
        flash(f"Sent {sent} shift reminder(s) for tomorrow's shifts.", "success")
    else:
        flash("No shift reminders needed right now.", "info")
    if failed:
        flash(f"{failed} reminder(s) failed to send.", "warning")
    return redirect(url_for("admin.dashboard"))
