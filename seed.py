"""
Seeds the database with:
- The current con year
- A handful of departments
- The reward tiers/items exactly as specified
- Admin, assigner, constore, and ~50 volunteer test accounts
- A full slate of shift slots across every department for every con day

Run with:  python seed.py
Safe to re-run -- it checks for existing rows before inserting.
"""
import random
from datetime import date, time, timedelta

from werkzeug.security import generate_password_hash

from app import create_app
from app.extensions import db
from app.models import (
    User, Role, ConYear, Department, RewardTier, RewardItem, AssignerDepartment, ShiftSlot,
    AvailabilityBlock, VolunteerDepartmentEligibility
)

app = create_app()


def get_or_create(model, defaults=None, **kwargs):
    instance = model.query.filter_by(**kwargs).first()
    if instance:
        return instance, False
    params = dict(kwargs)
    params.update(defaults or {})
    instance = model(**params)
    db.session.add(instance)
    db.session.commit()
    return instance, True


def seed():
    with app.app_context():
        db.create_all()

        con_year, _ = get_or_create(
            ConYear, label="2026",
            defaults={
                "is_current": True,
                "con_name": "Furpocalypse 2026",
                "contact_email": "volunteer@furpocalypse.org",
                "start_date": date(2026, 7, 16),
                "end_date": date(2026, 7, 20),
            },
        )

        dept_names = ["Registration", "Con Ops", "Con Store", "Tech/AV", "Art Show", "Gaming", "Security"]
        departments = {}
        for name in dept_names:
            d, _ = get_or_create(Department, name=name)
            departments[name] = d

        # ---- Reward tiers (flat hours, v1) ----
        tier4, _ = get_or_create(RewardTier, threshold_hours=4, defaults={"name": "4 Hour Tier", "sort_order": 1})
        tier8, _ = get_or_create(RewardTier, threshold_hours=8, defaults={"name": "8 Hour Tier", "sort_order": 2})
        tier12, _ = get_or_create(RewardTier, threshold_hours=12, defaults={"name": "12 Hour Tier", "sort_order": 3})
        tier24, _ = get_or_create(RewardTier, threshold_hours=24, defaults={"name": "24 Hour Tier", "sort_order": 4})

        # Each tier only lists the NEW items introduced at that tier --
        # sync_reward_claims() automatically cascades lower-tier items too.
        tier_items = {
            tier4: [
                ("Special Volunteer Lanyard/Patch/Badge", "Choice of lanyard, patch, or badge"),
            ],
            tier8: [
                ("Event T-Shirt (current year)", None),
            ],
            tier12: [
                ("Convention Hoodie", "Free"),
                ("$10 Hotel Restaurant Voucher", None),
            ],
            tier24: [
                ("Event Glass", None),
                ("Con-Themed Lanyard & Patch (current year)", None),
                ("Complimentary Basic Registration (next year)", None),
                ("Staff Application Eligibility (next year)", "Shared staff room or early access to a personally owned room"),
            ],
        }

        for tier, items in tier_items.items():
            for name, desc in items:
                get_or_create(RewardItem, tier_id=tier.id, name=name, defaults={"description": desc})

        # ---- Sample accounts (change passwords immediately in production!) ----
        default_pw_hash = generate_password_hash("changeme")

        admin, created = get_or_create(
            User, email="admin@example.com",
            defaults={
                "badge_name": "Admin",
                "full_name": "Con Admin",
                "role": Role.ADMIN,
                "password_hash": default_pw_hash,
            },
        )

        assigner, created = get_or_create(
            User, email="assigner@example.com",
            defaults={
                "badge_name": "DeptLead",
                "full_name": "Department Lead",
                "role": Role.ASSIGNER,
                "password_hash": default_pw_hash,
            },
        )
        if created:
            get_or_create(AssignerDepartment, assigner_id=assigner.id, department_id=departments["Con Ops"].id)

        constore, created = get_or_create(
            User, email="constore@example.com",
            defaults={
                "badge_name": "StoreStaff",
                "full_name": "Con Store Staff",
                "role": Role.CONSTORE,
                "password_hash": default_pw_hash,
            },
        )

        volunteer, created = get_or_create(
            User, email="volunteer@example.com",
            defaults={
                "badge_name": "TestFox",
                "full_name": "Test Volunteer",
                "role": Role.VOLUNTEER,
                "password_hash": default_pw_hash,
            },
        )

        # ---- Extra volunteers so search/calendar features have data to show ----
        badge_names = [
            "SkyPup", "EmberWolf", "SilverFox", "NovaCat", "EchoOtter",
            "BlueJay", "NightRaven", "PixelPup", "ShadowFox", "Comet",
            "Aurora", "Cobalt", "LunaWolf", "Rocket", "CopperFox",
            "SnowLeopard", "StormPaw", "Rex", "Sparky", "Bolt",
            "Zephyr", "Quartz", "Atlas", "Jasper", "Maple",
            "Onyx", "Willow", "Harbor", "River", "Birch",
            "Koda", "Blaze", "Ash", "Tango", "Mango",
            "Frost", "Flare", "Nimbus", "Orion", "Scout",
            "Cinder", "Pebble", "Indigo", "Piper", "Remy",
            "Dakota", "Phoenix", "Kodiak", "Finn", "Violet",
        ]

        extra_volunteers = []
        for i, badge in enumerate(badge_names, start=2):
            v, _ = get_or_create(
                User,
                email=f"volunteer{i}@example.com",
                defaults={
                    "badge_name": badge,
                    "full_name": f"{badge} Volunteer",
                    "role": Role.VOLUNTEER,
                    "password_hash": default_pw_hash,
                },
            )
            extra_volunteers.append(v)

        all_volunteers = [volunteer] + extra_volunteers

        # ---- Random shift eligibility so auto-assign and the calendar have
        # a real candidate pool to work with, instead of everyone being
        # eligible for nothing. Skips anyone who already has any grant at
        # all -- the departments picked are random, so checking per
        # (volunteer, department) instead would let every re-run roll a
        # fresh subset and keep adding more on top of what's already there.
        department_list = list(departments.values())
        eligibility_created = 0
        for v in all_volunteers:
            if VolunteerDepartmentEligibility.query.filter_by(volunteer_id=v.id).first():
                continue
            for dept in random.sample(department_list, random.randint(1, 3)):
                db.session.add(VolunteerDepartmentEligibility(
                    volunteer_id=v.id, department_id=dept.id, granted_by_id=admin.id,
                ))
                eligibility_created += 1
        db.session.commit()

        # ---- Sample shift slots on the calendar, across every department ----
        shift_templates = [
            ("Registration", "Morning Registration", time(8, 0), time(12, 0), 6),
            ("Registration", "Afternoon Registration", time(12, 0), time(16, 0), 6),
            ("Registration", "Evening Registration", time(16, 0), time(20, 0), 4),

            ("Con Ops", "Operations", time(8, 0), time(12, 0), 5),
            ("Con Ops", "Operations", time(12, 0), time(16, 0), 5),
            ("Con Ops", "Operations", time(16, 0), time(20, 0), 5),
            ("Con Ops", "Late Night Ops", time(20, 0), time(23, 59), 3),

            ("Con Store", "Store", time(10, 0), time(14, 0), 4),
            ("Con Store", "Store", time(14, 0), time(18, 0), 4),
            ("Con Store", "Store", time(18, 0), time(22, 0), 3),

            ("Tech/AV", "AV Support", time(9, 0), time(13, 0), 4),
            ("Tech/AV", "AV Support", time(13, 0), time(17, 0), 4),
            ("Tech/AV", "Main Events", time(17, 0), time(22, 0), 5),

            ("Gaming", "Gaming Room", time(10, 0), time(14, 0), 4),
            ("Gaming", "Gaming Room", time(14, 0), time(18, 0), 4),
            ("Gaming", "Gaming Room", time(18, 0), time(22, 0), 4),

            ("Art Show", "Art Show", time(10, 0), time(14, 0), 3),
            ("Art Show", "Art Show", time(14, 0), time(18, 0), 3),

            ("Security", "Day Security", time(8, 0), time(14, 0), 6),
            ("Security", "Evening Security", time(14, 0), time(20, 0), 6),
            ("Security", "Night Security", time(20, 0), time(23, 59), 5),
        ]

        current = con_year.start_date
        shifts_created = 0
        while current <= con_year.end_date:
            for dept_name, label, start, end, capacity in shift_templates:
                _, created = get_or_create(
                    ShiftSlot,
                    con_year_id=con_year.id,
                    shift_date=current,
                    start_time=start,
                    end_time=end,
                    department_id=departments[dept_name].id,
                    defaults={
                        "label": f"{label} ({current.strftime('%a')})",
                        "capacity": capacity,
                        "created_by_id": admin.id,
                    },
                )
                if created:
                    shifts_created += 1
            current += timedelta(days=1)

        # ---- Random 4-hour availability windows so the calendar's
        # "who's around" view has data to show. One block per volunteer per
        # con day; skips volunteers who already have one for that day so
        # re-running this script doesn't pile up duplicates.
        availability_created = 0
        for day in con_year.date_range():
            for v in all_volunteers:
                existing = AvailabilityBlock.query.filter_by(
                    volunteer_id=v.id, con_year_id=con_year.id, avail_date=day
                ).first()
                if existing:
                    continue
                start_hour = random.randint(6, 19)
                db.session.add(AvailabilityBlock(
                    volunteer_id=v.id,
                    con_year_id=con_year.id,
                    avail_date=day,
                    start_time=time(start_hour, 0),
                    end_time=time(start_hour + 4, 0),
                ))
                availability_created += 1
        db.session.commit()

        print("Seed complete.")
        print(f"  Con: {con_year.con_name} ({con_year.start_date} - {con_year.end_date})")
        print(f"  Departments: {len(departments)}")
        print(f"  Volunteers: {1 + len(extra_volunteers)} (plus admin/assigner/constore)")
        print(f"  Shift slots: {shifts_created} created this run "
              f"({len(shift_templates)} templates/day x "
              f"{(con_year.end_date - con_year.start_date).days + 1} days)")
        print(f"  Shift eligibility grants: {eligibility_created} created this run (1-3 depts/volunteer)")
        print(f"  Availability blocks: {availability_created} created this run (4hr window/volunteer/day)")
        print("  admin@example.com / changeme")
        print("  assigner@example.com / changeme  (manages Con Ops)")
        print("  constore@example.com / changeme  (can fulfill claims only)")
        print("  volunteer@example.com / changeme  (usercode:", volunteer.usercode, ")")
        print(f"  volunteer2@example.com .. volunteer{1 + len(badge_names)}@example.com / changeme")


if __name__ == "__main__":
    seed()
