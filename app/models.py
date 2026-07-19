import random
import string
from datetime import datetime, timedelta
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from app.extensions import db


def generate_usercode():
    """Short, human-readable unique code for badge scanning / QR lookup.
    Format: 3 letters + 4 digits, e.g. FBX4821. Regenerates on collision."""
    while True:
        code = "".join(random.choices(string.ascii_uppercase, k=3)) + \
               "".join(random.choices(string.digits, k=4))
        if not User.query.filter_by(usercode=code).first():
            return code


class Role:
    ADMIN = "admin"
    ASSIGNER = "assigner"
    CONSTORE = "constore"   # merch/con-store table staff, can fulfill claims
    VOLUNTEER = "volunteer"

    ALL = [ADMIN, ASSIGNER, CONSTORE, VOLUNTEER]
    # Roles allowed to mark a reward claim as fulfilled (single-person signoff)
    CAN_FULFILL = [ADMIN, ASSIGNER, CONSTORE]
    # Roles allowed to log/approve volunteer hours
    CAN_LOG_HOURS = [ADMIN, ASSIGNER]


class HourEntryStatus:
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ClaimStatus:
    LOCKED = "locked"       # not yet eligible (row may not even exist yet)
    ELIGIBLE = "eligible"   # threshold met, not yet claimed
    FULFILLED = "fulfilled" # physically handed out / redeemed


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=True, index=True)
    username = db.Column(db.String(50), unique=True, nullable=True, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    badge_name = db.Column(db.String(100), nullable=False)
    full_name = db.Column(db.String(150), nullable=False)
    role = db.Column(db.String(20), nullable=False, default=Role.VOLUNTEER)
    report_to_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    active = db.Column(db.Boolean, default=True, nullable=False)
    usercode = db.Column(db.String(10), unique=True, nullable=False, default=generate_usercode)
    is_kiosk_account = db.Column(db.Boolean, default=False, nullable=False)
    seen_welcome_tour = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    hour_entries = db.relationship(
        "HourEntry", back_populates="volunteer",
        foreign_keys="HourEntry.volunteer_id"
    )
    department_links = db.relationship("AssignerDepartment", back_populates="assigner")
    claims = db.relationship("RewardClaim", back_populates="volunteer",
                              foreign_keys="RewardClaim.volunteer_id")
    shift_requests = db.relationship("ShiftRequest", back_populates="volunteer",
                                      foreign_keys="ShiftRequest.volunteer_id")
    availability_blocks = db.relationship("AvailabilityBlock", back_populates="volunteer",
                                           cascade="all, delete-orphan")
    eligibility_links = db.relationship("VolunteerDepartmentEligibility", back_populates="volunteer",
                                         foreign_keys="VolunteerDepartmentEligibility.volunteer_id",
                                         cascade="all, delete-orphan")
    report_to = db.relationship("User", remote_side=[id], foreign_keys=[report_to_id], backref="direct_reports")

    def eligible_department_ids(self):
        return [link.department_id for link in self.eligibility_links]

    def has_history(self):
        """True if this user has any hours, claims, shift requests, or audit
        entries -- used to decide whether a hard delete is safe, or whether
        deactivating is the better option."""
        if HourEntry.query.filter_by(volunteer_id=self.id).first():
            return True
        if RewardClaim.query.filter_by(volunteer_id=self.id).first():
            return True
        if ShiftRequest.query.filter_by(volunteer_id=self.id).first():
            return True
        if AuditLog.query.filter_by(actor_id=self.id).first():
            return True
        return False

    def set_password(self, raw_password):
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password):
        return check_password_hash(self.password_hash, raw_password)

    def has_role(self, *roles):
        return self.role in roles

    def departments_managed(self):
        if self.role == Role.ADMIN:
            return Department.query.all()
        return [link.department for link in self.department_links]

    def approved_hours(self, con_year_id):
        total = (
            db.session.query(db.func.coalesce(db.func.sum(HourEntry.hours), 0.0))
            .filter(
                HourEntry.volunteer_id == self.id,
                HourEntry.con_year_id == con_year_id,
                HourEntry.status == HourEntryStatus.APPROVED,
            )
            .scalar()
        )
        return float(total or 0.0)

    def current_tier(self, con_year_id):
        hours = self.approved_hours(con_year_id)
        tiers = (
            RewardTier.query.filter(RewardTier.threshold_hours <= hours)
            .order_by(RewardTier.threshold_hours.desc())
            .all()
        )
        return tiers[0] if tiers else None

    def next_tier(self, con_year_id):
        hours = self.approved_hours(con_year_id)
        tier = (
            RewardTier.query.filter(RewardTier.threshold_hours > hours)
            .order_by(RewardTier.threshold_hours.asc())
            .first()
        )
        return tier

    def __repr__(self):
        return f"<User {self.badge_name} ({self.role})>"


class ConYear(db.Model):
    __tablename__ = "con_years"

    id = db.Column(db.Integer, primary_key=True)
    label = db.Column(db.String(50), unique=True, nullable=False)  # e.g. "2026"
    con_name = db.Column(db.String(150), nullable=True)  # e.g. "Furpocalypse 2026"
    contact_email = db.Column(db.String(255), nullable=True)
    is_current = db.Column(db.Boolean, default=False, nullable=False)
    start_date = db.Column(db.Date, nullable=True)
    end_date = db.Column(db.Date, nullable=True)

    def date_range(self):
        """List of dates from start_date to end_date inclusive, for building
        the shift-slot calendar grid."""
        if not self.start_date or not self.end_date:
            return []
        days = []
        d = self.start_date
        while d <= self.end_date:
            days.append(d)
            d = d + timedelta(days=1)
        return days

    def __repr__(self):
        return f"<ConYear {self.label}>"


class Department(db.Model):
    __tablename__ = "departments"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)

    assigners = db.relationship("AssignerDepartment", back_populates="department")

    def __repr__(self):
        return f"<Department {self.name}>"


class AssignerDepartment(db.Model):
    """Which departments an assigner is allowed to log/approve hours for."""
    __tablename__ = "assigner_departments"

    id = db.Column(db.Integer, primary_key=True)
    assigner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=False)

    assigner = db.relationship("User", back_populates="department_links")
    department = db.relationship("Department", back_populates="assigners")

    __table_args__ = (db.UniqueConstraint("assigner_id", "department_id"),)


class HourEntry(db.Model):
    """A single logged block of volunteer time. Only APPROVED entries count
    toward tier thresholds. Kept separate from any future shift-scheduling
    concept so edits/rejections never corrupt the audit trail."""
    __tablename__ = "hour_entries"

    id = db.Column(db.Integer, primary_key=True)
    volunteer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    con_year_id = db.Column(db.Integer, db.ForeignKey("con_years.id"), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=False)

    work_date = db.Column(db.Date, nullable=False)
    hours = db.Column(db.Float, nullable=False)  # flat hours in v1
    task_description = db.Column(db.String(255), nullable=True)

    status = db.Column(db.String(20), nullable=False, default=HourEntryStatus.PENDING)

    logged_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    approved_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    approved_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    volunteer = db.relationship("User", back_populates="hour_entries", foreign_keys=[volunteer_id])
    department = db.relationship("Department")
    con_year = db.relationship("ConYear")
    logged_by = db.relationship("User", foreign_keys=[logged_by_id])
    approved_by = db.relationship("User", foreign_keys=[approved_by_id])

    def __repr__(self):
        return f"<HourEntry {self.volunteer_id} {self.hours}h {self.status}>"


class RewardTier(db.Model):
    __tablename__ = "reward_tiers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)       # "4 Hour Tier"
    threshold_hours = db.Column(db.Float, nullable=False, unique=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)

    items = db.relationship("RewardItem", back_populates="tier", order_by="RewardItem.id")

    def __repr__(self):
        return f"<RewardTier {self.name} ({self.threshold_hours}h)>"


class RewardItem(db.Model):
    __tablename__ = "reward_items"

    id = db.Column(db.Integer, primary_key=True)
    tier_id = db.Column(db.Integer, db.ForeignKey("reward_tiers.id"), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    description = db.Column(db.String(255), nullable=True)
    # e.g. "staff_eligibility" items don't get physically fulfilled at a table,
    # they just flip a flag admins can see -- still tracked the same way.

    tier = db.relationship("RewardTier", back_populates="items")

    def __repr__(self):
        return f"<RewardItem {self.name}>"


class RewardClaim(db.Model):
    """Tracks an individual volunteer's eligibility + fulfillment state for
    one reward item, scoped to a con year."""
    __tablename__ = "reward_claims"

    id = db.Column(db.Integer, primary_key=True)
    volunteer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    reward_item_id = db.Column(db.Integer, db.ForeignKey("reward_items.id"), nullable=False)
    con_year_id = db.Column(db.Integer, db.ForeignKey("con_years.id"), nullable=False)

    status = db.Column(db.String(20), nullable=False, default=ClaimStatus.ELIGIBLE)

    fulfilled_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    fulfilled_at = db.Column(db.DateTime, nullable=True)
    notes = db.Column(db.String(255), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    volunteer = db.relationship("User", back_populates="claims", foreign_keys=[volunteer_id])
    reward_item = db.relationship("RewardItem")
    fulfilled_by = db.relationship("User", foreign_keys=[fulfilled_by_id])
    con_year = db.relationship("ConYear")

    __table_args__ = (db.UniqueConstraint("volunteer_id", "reward_item_id", "con_year_id"),)

    def __repr__(self):
        return f"<RewardClaim vol={self.volunteer_id} item={self.reward_item_id} {self.status}>"


class ShiftRequestStatus:
    REQUESTED = "requested"
    APPROVED = "approved"
    DENIED = "denied"
    CANCELLED = "cancelled"
    LEAVE_REQUESTED = "leave_requested"
    LEAVE_DENIED = "leave_denied"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    NO_SHOW = "no_show"


class ShiftSwapStatus:
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    CANCELLED = "cancelled"


class ShiftSlot(db.Model):
    """A predefined block of time on a given con day that volunteers can
    request to work. Admins/assigners create these ahead of time (the
    'boxes' on the calendar); capacity is how many volunteers can fill it."""
    __tablename__ = "shift_slots"

    id = db.Column(db.Integer, primary_key=True)
    con_year_id = db.Column(db.Integer, db.ForeignKey("con_years.id"), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=False)

    shift_date = db.Column(db.Date, nullable=False)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    capacity = db.Column(db.Integer, nullable=False, default=1)
    label = db.Column(db.String(150), nullable=True)  # e.g. "Registration - Morning"

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    department = db.relationship("Department")
    con_year = db.relationship("ConYear")
    created_by = db.relationship("User")
    requests = db.relationship("ShiftRequest", back_populates="shift_slot")

    def hours(self):
        """Duration of the slot in hours (float), used for display only --
        actual worked hours still get logged separately via HourEntry."""
        today = datetime(2000, 1, 1)
        start = datetime.combine(today, self.start_time)
        end = datetime.combine(today, self.end_time)
        return round((end - start).total_seconds() / 3600, 2)

    def approved_count(self):
        return sum(1 for r in self.requests if r.status == ShiftRequestStatus.APPROVED)

    def open_spots(self):
        return max(self.capacity - self.approved_count(), 0)

    def is_full(self):
        return self.open_spots() <= 0

    def __repr__(self):
        return f"<ShiftSlot {self.shift_date} {self.start_time}-{self.end_time}>"


class ShiftRequest(db.Model):
    """A volunteer's request to work a specific ShiftSlot. Requires
    admin/assigner approval so the calendar reflects who's actually
    confirmed vs. just interested."""
    __tablename__ = "shift_requests"

    id = db.Column(db.Integer, primary_key=True)
    shift_slot_id = db.Column(db.Integer, db.ForeignKey("shift_slots.id"), nullable=False)
    volunteer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    status = db.Column(db.String(20), nullable=False, default=ShiftRequestStatus.REQUESTED)
    requested_at = db.Column(db.DateTime, default=datetime.utcnow)

    decided_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    decided_at = db.Column(db.DateTime, nullable=True)
    checked_in_at = db.Column(db.DateTime, nullable=True)
    checked_out_at = db.Column(db.DateTime, nullable=True)
    notes = db.Column(db.String(255), nullable=True)
    reminder_sent_at = db.Column(db.DateTime, nullable=True)

    shift_slot = db.relationship("ShiftSlot", back_populates="requests")
    volunteer = db.relationship("User", back_populates="shift_requests", foreign_keys=[volunteer_id])
    decided_by = db.relationship("User", foreign_keys=[decided_by_id])

    __table_args__ = (db.UniqueConstraint("shift_slot_id", "volunteer_id"),)

    def __repr__(self):
        return f"<ShiftRequest vol={self.volunteer_id} slot={self.shift_slot_id} {self.status}>"

    def scheduled_start(self):
        return datetime.combine(self.shift_slot.shift_date, self.shift_slot.start_time)

    def scheduled_end(self):
        return datetime.combine(self.shift_slot.shift_date, self.shift_slot.end_time)

    def can_check_in(self, now=None):
        now = now or datetime.now()
        window_open = self.scheduled_start() - timedelta(hours=1)
        return self.status == ShiftRequestStatus.APPROVED and window_open <= now < self.scheduled_end()

    def is_active_shift(self):
        return self.status in (ShiftRequestStatus.APPROVED, ShiftRequestStatus.IN_PROGRESS, ShiftRequestStatus.LEAVE_REQUESTED, ShiftRequestStatus.LEAVE_DENIED)

    def is_no_show(self, now=None):
        """True if this was confirmed but the volunteer never checked in and
        the shift is now over -- used to flag likely no-shows for staff to
        review and, if confirmed, free the slot back up."""
        now = now or datetime.now()
        return (
            self.status == ShiftRequestStatus.APPROVED
            and self.checked_in_at is None
            and now > self.scheduled_end()
        )


class ShiftSwapRequest(db.Model):
    """A volunteer-initiated proposal to trade one confirmed shift for
    another volunteer's confirmed shift. Resolved entirely between the two
    volunteers (accept/decline) -- staff aren't in the loop unless
    something goes wrong, since both shifts are already approved."""
    __tablename__ = "shift_swap_requests"

    id = db.Column(db.Integer, primary_key=True)
    requester_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    requester_shift_request_id = db.Column(db.Integer, db.ForeignKey("shift_requests.id"), nullable=False)
    target_shift_request_id = db.Column(db.Integer, db.ForeignKey("shift_requests.id"), nullable=False)

    status = db.Column(db.String(20), nullable=False, default=ShiftSwapStatus.PENDING)
    note = db.Column(db.String(255), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    decided_at = db.Column(db.DateTime, nullable=True)

    requester = db.relationship("User", foreign_keys=[requester_id])
    requester_shift_request = db.relationship("ShiftRequest", foreign_keys=[requester_shift_request_id])
    target_shift_request = db.relationship("ShiftRequest", foreign_keys=[target_shift_request_id])

    def target_volunteer(self):
        return self.target_shift_request.volunteer

    def __repr__(self):
        return f"<ShiftSwapRequest {self.requester_id} -> slot {self.target_shift_request_id} ({self.status})>"


class AvailabilityBlock(db.Model):
    """A free-form window of time a volunteer has marked themselves available,
    drawn via click-and-drag on the calendar. Distinct from ShiftSlot/
    ShiftRequest (predefined boxes staff post) -- this is the volunteer
    proactively saying 'I'm around then', for staff to reference when
    filling shifts."""
    __tablename__ = "availability_blocks"

    id = db.Column(db.Integer, primary_key=True)
    volunteer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    con_year_id = db.Column(db.Integer, db.ForeignKey("con_years.id"), nullable=False)
    avail_date = db.Column(db.Date, nullable=False)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    volunteer = db.relationship("User", back_populates="availability_blocks")
    con_year = db.relationship("ConYear")

    def __repr__(self):
        return f"<AvailabilityBlock vol={self.volunteer_id} {self.avail_date} {self.start_time}-{self.end_time}>"


class VolunteerDepartmentEligibility(db.Model):
    """Which departments a volunteer has been approved to pick up open
    shifts for. A volunteer with no row for a department shouldn't see or
    request that department's shift slots on the calendar."""
    __tablename__ = "volunteer_department_eligibility"

    id = db.Column(db.Integer, primary_key=True)
    volunteer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=False)
    granted_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    volunteer = db.relationship("User", back_populates="eligibility_links", foreign_keys=[volunteer_id])
    department = db.relationship("Department")
    granted_by = db.relationship("User", foreign_keys=[granted_by_id])

    __table_args__ = (db.UniqueConstraint("volunteer_id", "department_id"),)


class AuditLog(db.Model):
    """Lightweight append-only trail for the actions admins most care about:
    hour approvals/rejections and reward fulfillment."""
    __tablename__ = "audit_log"

    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    action = db.Column(db.String(50), nullable=False)  # e.g. "approve_hours", "fulfill_claim"
    target_type = db.Column(db.String(50), nullable=False)  # "HourEntry" / "RewardClaim"
    target_id = db.Column(db.Integer, nullable=False)
    detail = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    actor = db.relationship("User")


def sync_reward_claims(volunteer: User, con_year: ConYear):
    """Ensure a RewardClaim row exists (status=ELIGIBLE) for every RewardItem
    at or below the volunteer's current tier for this con year. Never
    downgrades or removes existing claims -- once eligible/fulfilled, it
    stays that way even if hours are later corrected downward, since that's
    an edge case staff should resolve manually via the admin panel."""
    tier = volunteer.current_tier(con_year.id)
    if tier is None:
        return []

    eligible_tiers = RewardTier.query.filter(
        RewardTier.threshold_hours <= tier.threshold_hours
    ).all()
    item_ids = [item.id for t in eligible_tiers for item in t.items]

    existing = {
        c.reward_item_id
        for c in RewardClaim.query.filter_by(
            volunteer_id=volunteer.id, con_year_id=con_year.id
        ).all()
    }

    created = []
    for item_id in item_ids:
        if item_id not in existing:
            claim = RewardClaim(
                volunteer_id=volunteer.id,
                reward_item_id=item_id,
                con_year_id=con_year.id,
                status=ClaimStatus.ELIGIBLE,
            )
            db.session.add(claim)
            created.append(claim)

    if created:
        db.session.commit()
    return created
