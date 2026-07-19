from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user

from app.models import User
from app.auth.forms import LoginForm

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    form = LoginForm()
    if form.validate_on_submit():
        identifier = form.identifier.data.strip().lower()
        user = User.query.filter_by(email=identifier).first() or User.query.filter_by(username=identifier).first()
        if user is None or not user.check_password(form.password.data) or not user.active:
            flash("Invalid email or password.", "danger")
            return redirect(url_for("auth.login"))
        login_user(user, remember=form.remember.data)
        next_page = request.args.get("next")
        return redirect(next_page or url_for("index"))

    return render_template("auth/login.html", form=form)


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out.", "info")
    return redirect(url_for("auth.login"))
