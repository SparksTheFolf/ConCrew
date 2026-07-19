from functools import wraps
import smtplib
from datetime import date, datetime, timedelta
from email.message import EmailMessage

from flask import abort, current_app
from flask_login import current_user

from app.extensions import db


def roles_required(*roles):
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated or current_user.role not in roles:
                abort(403)
            return fn(*args, **kwargs)
        return wrapped
    return decorator


def get_current_con_year():
    from app.models import ConYear
    return ConYear.query.filter_by(is_current=True).first()


def generate_random_password(length=10):
    import random
    import string
    # Avoid ambiguous characters (0/O, 1/l/I) since this may get handwritten/printed for kiosk logins
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"
    return "".join(random.choices(alphabet, k=length))


def setup_is_complete():
    """True once at least one ConYear and one admin user exist. Used to
    gate the first-run setup wizard."""
    from app.models import User, ConYear, Role
    return ConYear.query.first() is not None and User.query.filter_by(role=Role.ADMIN).first() is not None


def _shift_notification_text(volunteer, slot, intro="You have a shift update."):
    """Plain-text fallback -- keep this alongside the HTML version and send
    both parts (multipart/alternative) so clients that block HTML still get
    something readable."""
    location = slot.label or slot.department.name
    return (
        f"Hello {volunteer.badge_name},\n\n"
        f"{intro}\n\n"
        f"Shift: {location}\n"
        f"Department: {slot.department.name}\n"
        f"Date: {slot.shift_date}\n"
        f"Time: {slot.start_time.strftime('%H:%M')} - {slot.end_time.strftime('%H:%M')}\n"
        f"Hours: {slot.hours()}\n\n"
        f"Please open the calendar for details.\n"
    )


def _shift_notification_html(volunteer, slot, calendar_url="#", intro="Here's an update on one of your volunteer shifts."):
    """HTML email version. Uses inline styles and table-based layout since
    that's still the most reliable way to get consistent rendering across
    Gmail, Outlook, Apple Mail, etc. -- <style> blocks and modern CSS
    (flexbox/grid) get stripped or ignored by several major clients."""
    location = slot.label or slot.department.name
    con_name = getattr(slot.con_year, "con_name", None) or "the convention"
    contact_email = getattr(slot.con_year, "contact_email", None)

    return f"""\
<!doctype html>
<html>
<body style="margin:0; padding:0; background-color:#f2f2f5; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f2f2f5; padding:32px 0;">
    <tr>
      <td align="center">
        <table role="presentation" width="480" cellpadding="0" cellspacing="0" style="background-color:#ffffff; border-radius:10px; overflow:hidden; box-shadow:0 2px 8px rgba(0,0,0,0.06);">

          <!-- Header -->
          <tr>
            <td style="background-color:#212529; padding:24px 32px;">
              <span style="font-size:20px; font-weight:600; color:#ffffff;">ConCrew - {con_name}</span><br>
              <span style="font-size:13px; color:#adb5bd;">Volunteer Shift Update</span>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="padding:32px;">
              <p style="margin:0 0 8px; font-size:16px; color:#212529;">
                Hi {volunteer.badge_name},
              </p>
              <p style="margin:0 0 24px; font-size:14px; color:#495057; line-height:1.5;">
                {intro}
              </p>

              <!-- Shift details card -->
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
                     style="background-color:#f8f9fa; border-radius:8px; border:1px solid #e9ecef;">
                <tr>
                  <td style="padding:20px 24px;">
                    <p style="margin:0 0 4px; font-size:15px; font-weight:600; color:#212529;">
                      {location}
                    </p>
                    <p style="margin:0 0 16px; font-size:13px; color:#868e96;">
                      {slot.department.name}
                    </p>

                    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="font-size:14px; color:#495057;">
                      <tr>
                        <td style="padding:4px 0; width:80px; color:#868e96;">Date</td>
                        <td style="padding:4px 0; font-weight:500;">{slot.shift_date.strftime('%A, %B') + f' {slot.shift_date.day}, ' + slot.shift_date.strftime('%Y') if hasattr(slot.shift_date, 'strftime') else slot.shift_date}</td>
                      </tr>
                      <tr>
                        <td style="padding:4px 0; color:#868e96;">Time</td>
                        <td style="padding:4px 0; font-weight:500;">{slot.start_time.strftime('%H:%M')} – {slot.end_time.strftime('%H:%M')}</td>
                      </tr>
                      <tr>
                        <td style="padding:4px 0; color:#868e96;">Hours</td>
                        <td style="padding:4px 0; font-weight:500;">{slot.hours()}h</td>
                      </tr>
                    </table>
                  </td>
                </tr>
              </table>

            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="padding:20px 32px; border-top:1px solid #e9ecef;">
              <p style="margin:0; font-size:12px; color:#adb5bd; line-height:1.5;">
                You're receiving this because you're a registered volunteer for {con_name}.
                {f'Questions? Reach out to <a href="mailto:{contact_email}" style="color:#868e96;">{contact_email}</a>.' if contact_email else ''}
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""


def _smtp_config_or_error():
    server = current_app.config.get("MAIL_SERVER", "")
    sender = current_app.config.get("MAIL_DEFAULT_SENDER", "")
    if not server or not sender:
        return None, None, "Email is not configured for this environment."
    return server, sender, None


def _connect_smtp(server):
    if current_app.config.get("MAIL_USE_SSL"):
        smtp = smtplib.SMTP_SSL(server, current_app.config.get("MAIL_PORT", 465))
    else:
        smtp = smtplib.SMTP(server, current_app.config.get("MAIL_PORT", 587))
    if current_app.config.get("MAIL_USE_TLS") and not current_app.config.get("MAIL_USE_SSL"):
        smtp.starttls()
    username = current_app.config.get("MAIL_USERNAME", "")
    password = current_app.config.get("MAIL_PASSWORD", "")
    if username:
        smtp.login(username, password)
    return smtp


def _send_single_email(to_email, subject, text_body, html_body):
    server, sender, error = _smtp_config_or_error()
    if error:
        return False, error

    if current_app.config.get("MAIL_SUPPRESS_SEND"):
        return True, None

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = to_email
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    try:
        with _connect_smtp(server) as smtp:
            smtp.send_message(message)
    except OSError as exc:
        current_app.logger.warning("Failed to send email to %s: %s", to_email, exc)
        return False, "Failed to send email notification."

    return True, None


def send_shift_assignment_email(volunteer, slot):
    if not volunteer.email:
        return False, "Volunteer has no email address on file."

    subject = f"ConCrew - You were assigned to {slot.label or slot.department.name}"
    return _send_single_email(
        volunteer.email, subject,
        _shift_notification_text(volunteer, slot),
        _shift_notification_html(volunteer, slot),
    )

def send_shift_notification(volunteer, slot):
    return send_shift_assignment_email(volunteer, slot)


def send_shift_reminder_email(volunteer, slot):
    if not volunteer.email:
        return False, "Volunteer has no email address on file."

    subject = f"ConCrew - Reminder: your shift starts tomorrow ({slot.label or slot.department.name})"
    intro = "This is a reminder that you have a volunteer shift starting tomorrow."
    return _send_single_email(
        volunteer.email, subject,
        _shift_notification_text(volunteer, slot, intro=intro),
        _shift_notification_html(volunteer, slot, intro=intro),
    )


def send_pending_shift_reminders():
    """Email every volunteer whose approved shift starts tomorrow and who
    hasn't already been reminded for it. Used by the `flask
    send-shift-reminders` CLI command (for a daily cron/Task Scheduler job)
    and by an on-demand button on the admin dashboard. Returns
    (sent, failed, skipped_no_email) counts."""
    from app.models import ShiftRequest, ShiftRequestStatus, ShiftSlot

    tomorrow = date.today() + timedelta(days=1)
    requests = (
        ShiftRequest.query
        .join(ShiftSlot, ShiftRequest.shift_slot_id == ShiftSlot.id)
        .filter(
            ShiftRequest.status == ShiftRequestStatus.APPROVED,
            ShiftRequest.reminder_sent_at.is_(None),
            ShiftSlot.shift_date == tomorrow,
        )
        .all()
    )

    sent = 0
    failed = 0
    skipped_no_email = 0

    for req in requests:
        volunteer = req.volunteer
        if not volunteer.email:
            skipped_no_email += 1
            continue
        ok, _ = send_shift_reminder_email(volunteer, req.shift_slot)
        if ok:
            req.reminder_sent_at = datetime.utcnow()
            sent += 1
        else:
            failed += 1

    if sent:
        db.session.commit()

    return sent, failed, skipped_no_email


def _mass_email_html(subject, body_text):
    """HTML wrapper for admin-authored mass emails. Body text is admin
    input, not template markup, so it's escaped and turned into paragraphs
    rather than rendered as HTML."""
    import html as _html
    paragraphs = "".join(
        f'<p style="margin:0 0 16px; font-size:14px; color:#495057; line-height:1.5;">{_html.escape(line)}</p>'
        for line in body_text.splitlines() if line.strip()
    )

    return f"""\
<!doctype html>
<html>
<body style="margin:0; padding:0; background-color:#f2f2f5; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f2f2f5; padding:32px 0;">
    <tr>
      <td align="center">
        <table role="presentation" width="480" cellpadding="0" cellspacing="0" style="background-color:#ffffff; border-radius:10px; overflow:hidden; box-shadow:0 2px 8px rgba(0,0,0,0.06);">
          <tr>
            <td style="background-color:#212529; padding:24px 32px;">
              <span style="font-size:20px; font-weight:600; color:#ffffff;">ConCrew</span><br>
              <span style="font-size:13px; color:#adb5bd;">{_html.escape(subject)}</span>
            </td>
          </tr>
          <tr>
            <td style="padding:32px;">
              {paragraphs}
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""


def send_mass_email(recipients, subject, body_text):
    """Send the same subject/body to many users over a single SMTP
    connection. Sends one message per recipient (rather than BCC) so
    recipients never see each other's addresses. Returns (sent, failed)
    where sent is a list of User and failed is a list of (User, reason)."""
    server, sender, error = _smtp_config_or_error()
    if error:
        return [], [(r, error) for r in recipients]

    if current_app.config.get("MAIL_SUPPRESS_SEND"):
        return list(recipients), []

    try:
        smtp = _connect_smtp(server)
    except OSError as exc:
        current_app.logger.warning("Failed to connect to SMTP server for mass email: %s", exc)
        return [], [(r, "Failed to connect to email server.") for r in recipients]

    sent = []
    failed = []
    with smtp:
        for recipient in recipients:
            message = EmailMessage()
            message["Subject"] = subject
            message["From"] = sender
            message["To"] = recipient.email
            message.set_content(body_text)
            message.add_alternative(_mass_email_html(subject, body_text), subtype="html")
            try:
                smtp.send_message(message)
                sent.append(recipient)
            except OSError as exc:
                current_app.logger.warning("Failed to send mass email to %s: %s", recipient.email, exc)
                failed.append((recipient, "Send failed."))

    return sent, failed
