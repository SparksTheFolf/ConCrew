import io

import qrcode
from flask import Blueprint, render_template, redirect, url_for, flash, request, Response, current_app, jsonify
from flask_login import login_required, current_user

from app.extensions import db
from app.models import User

profile_bp = Blueprint("profile", __name__)


@profile_bp.route("/", methods=["GET", "POST"])
@login_required
def home():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        badge_name = request.form.get("badge_name", "").strip()
        email = request.form.get("email", "").strip().lower() or None
        new_password = request.form.get("new_password", "")

        if not full_name or not badge_name:
            flash("Name and badge name can't be blank.", "danger")
            return redirect(url_for("profile.home"))

        if email and email != current_user.email:
            if User.query.filter(User.email == email, User.id != current_user.id).first():
                flash("That email is already in use.", "danger")
                return redirect(url_for("profile.home"))
            current_user.email = email

        current_user.full_name = full_name
        current_user.badge_name = badge_name

        if new_password:
            if len(new_password) < 8:
                flash("New password must be at least 8 characters.", "danger")
                return redirect(url_for("profile.home"))
            current_user.set_password(new_password)

        db.session.commit()
        flash("Profile updated.", "success")
        return redirect(url_for("profile.home"))

    return render_template("profile/home.html")


@profile_bp.route("/welcome-tour/dismiss", methods=["POST"])
@login_required
def dismiss_welcome_tour():
    """Called once the first time a user closes the auto-shown welcome
    popup, so it doesn't pop up again on their next login."""
    if not current_user.seen_welcome_tour:
        current_user.seen_welcome_tour = True
        db.session.commit()
    return jsonify({"ok": True})


@profile_bp.route("/qrcode.png")
@login_required
def qrcode_image():
    # Encodes a quick-log lookup URL so staff scanning it jump straight to
    # pre-filled hour logging for this volunteer.
    target_url = url_for("assigner.quick_log", usercode=current_user.usercode, _external=True)
    img = qrcode.make(target_url, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(buf.getvalue(), mimetype="image/png")
