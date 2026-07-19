from datetime import datetime

from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_user

from app.extensions import db
from app.models import User, Role, ConYear
from app.utils import setup_is_complete

setup_bp = Blueprint("setup", __name__)


@setup_bp.route("/setup", methods=["GET", "POST"])
def setup():
    if setup_is_complete():
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        con_name = request.form.get("con_name", "").strip()
        label = request.form.get("label", "").strip()
        contact_email = request.form.get("contact_email", "").strip()
        start_date = request.form.get("start_date", "").strip()
        end_date = request.form.get("end_date", "").strip()

        admin_email = request.form.get("admin_email", "").strip().lower()
        admin_name = request.form.get("admin_name", "").strip()
        admin_badge = request.form.get("admin_badge", "").strip()
        admin_password = request.form.get("admin_password", "")

        errors = []
        if not con_name or not label:
            errors.append("Con name and year label are required.")
        if not admin_email or not admin_password or not admin_name or not admin_badge:
            errors.append("All admin account fields are required.")
        if len(admin_password) < 8:
            errors.append("Admin password must be at least 8 characters.")

        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("setup/setup.html", form=request.form)

        con_year = ConYear(
            label=label,
            con_name=con_name,
            contact_email=contact_email or None,
            is_current=True,
            start_date=datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else None,
            end_date=datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else None,
        )
        db.session.add(con_year)

        admin = User(
            email=admin_email,
            badge_name=admin_badge,
            full_name=admin_name,
            role=Role.ADMIN,
        )
        admin.set_password(admin_password)
        db.session.add(admin)
        db.session.commit()

        login_user(admin)
        flash(f"Welcome! {con_name} is set up.", "success")
        return redirect(url_for("index"))

    return render_template("setup/setup.html", form={})
