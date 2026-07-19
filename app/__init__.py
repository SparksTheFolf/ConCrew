import os
from datetime import datetime
from flask import Flask, redirect, url_for, request
from flask_login import current_user

from config import Config
from app.extensions import db, migrate, login_manager, csrf


def create_app(config_class=Config):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_class)

    os.makedirs(app.instance_path, exist_ok=True)

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)

    from app.models import User

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from app.auth.routes import auth_bp
    from app.admin.routes import admin_bp
    from app.assigner.routes import assigner_bp
    from app.volunteer.routes import volunteer_bp
    from app.claims.routes import claims_bp
    from app.setup.routes import setup_bp
    from app.calendar.routes import calendar_bp
    from app.profile.routes import profile_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(assigner_bp, url_prefix="/assigner")
    app.register_blueprint(volunteer_bp, url_prefix="/volunteer")
    app.register_blueprint(claims_bp, url_prefix="/claims")
    app.register_blueprint(setup_bp)
    app.register_blueprint(calendar_bp, url_prefix="/calendar")
    app.register_blueprint(profile_bp, url_prefix="/profile")

    from app.utils import setup_is_complete

    @app.before_request
    def require_setup():
        if request.endpoint in (None, "setup.setup", "static"):
            return None
        if not setup_is_complete():
            return redirect(url_for("setup.setup"))
        return None

    @app.context_processor
    def inject_footer_info():
        return {
            "app_name": app.config.get("APP_NAME"),
            "app_version": app.config.get("APP_VERSION"),
            "current_year": datetime.utcnow().year,
        }

    @app.route("/")
    def index():
        if not current_user.is_authenticated:
            return redirect(url_for("auth.login"))
        if current_user.role == "admin":
            return redirect(url_for("admin.dashboard"))
        if current_user.role in ("assigner", "constore"):
            return redirect(url_for("assigner.dashboard"))
        return redirect(url_for("volunteer.dashboard"))

    @app.cli.command("send-shift-reminders")
    def send_shift_reminders_command():
        """Email volunteers whose approved shift starts tomorrow. Intended to
        be run once a day via cron / Windows Task Scheduler, e.g.:
        flask send-shift-reminders"""
        from app.utils import send_pending_shift_reminders
        sent, failed, skipped_no_email = send_pending_shift_reminders()
        print(f"Shift reminders: sent={sent} failed={failed} skipped_no_email={skipped_no_email}")

    return app
