from datetime import datetime, timedelta

from flask import Blueprint, render_template, redirect, url_for, flash, request, session, current_app
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash

from app.extensions import db
from app.models import User, LoginCode, generate_login_code
from app.auth.forms import LoginForm, EmailCodeRequestForm, EmailCodeVerifyForm
from app.utils import send_login_code_email

auth_bp = Blueprint("auth", __name__)
PENDING_LOGIN_CODE_USER_ID = "pending_login_code_user_id"


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


@auth_bp.route("/login/code", methods=["GET", "POST"])
def login_code_request():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    form = EmailCodeRequestForm()
    if form.validate_on_submit():
        identifier = form.email.data.strip().lower()
        user = User.query.filter_by(email=identifier).first()

        if user and user.active:
            cooldown = current_app.config.get("LOGIN_CODE_RESEND_COOLDOWN_SECONDS", 60)
            recent = (
                LoginCode.query.filter_by(user_id=user.id)
                .order_by(LoginCode.created_at.desc())
                .first()
            )
            if not recent or (datetime.utcnow() - recent.created_at).total_seconds() >= cooldown:
                ttl = current_app.config.get("LOGIN_CODE_TTL_SECONDS", 600)
                raw_code = generate_login_code()
                login_code = LoginCode(
                    user_id=user.id,
                    code_hash=generate_password_hash(raw_code),
                    expires_at=datetime.utcnow() + timedelta(seconds=ttl),
                )
                db.session.add(login_code)
                db.session.commit()
                send_login_code_email(user, raw_code)
            session[PENDING_LOGIN_CODE_USER_ID] = user.id

        # Same message and redirect whether or not the account exists, so this
        # can't be used to probe which emails are registered.
        flash("If that email is associated with an account, a login code has been sent.", "info")
        return redirect(url_for("auth.login_code_verify"))

    return render_template("auth/login_code_request.html", form=form)


@auth_bp.route("/login/code/verify", methods=["GET", "POST"])
def login_code_verify():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    form = EmailCodeVerifyForm()
    if form.validate_on_submit():
        user_id = session.get(PENDING_LOGIN_CODE_USER_ID)
        max_attempts = current_app.config.get("LOGIN_CODE_MAX_ATTEMPTS", 5)

        login_code = None
        if user_id:
            login_code = (
                LoginCode.query.filter_by(user_id=user_id, consumed_at=None)
                .order_by(LoginCode.created_at.desc())
                .first()
            )

        valid = False
        if login_code and login_code.is_valid() and login_code.attempts < max_attempts:
            if login_code.check_code(form.code.data.strip()):
                valid = True
            else:
                login_code.attempts += 1
                db.session.commit()

        if valid:
            user = User.query.get(user_id)
            if user and user.active:
                login_code.consumed_at = datetime.utcnow()
                db.session.commit()
                session.pop(PENDING_LOGIN_CODE_USER_ID, None)
                login_user(user)
                next_page = request.args.get("next")
                return redirect(next_page or url_for("index"))

        flash("Invalid or expired code. Please request a new one.", "danger")
        return redirect(url_for("auth.login_code_verify"))

    return render_template("auth/login_code_verify.html", form=form)


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out.", "info")
    return redirect(url_for("auth.login"))
