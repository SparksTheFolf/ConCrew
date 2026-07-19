from flask import Blueprint, jsonify, request, current_app

from app.extensions import db
from app.utils import link_telegram_registration

telegram_bp = Blueprint("telegram", __name__)


@telegram_bp.route("/webhook", methods=["POST"])
def webhook():
    payload = request.get_json(silent=True) or {}
    message = payload.get("message") or payload.get("edited_message") or {}
    text = (message.get("text") or "").strip()
    from_user = message.get("from") or {}
    chat = message.get("chat") or {}

    if not text.startswith("/start"):
        return jsonify({"ok": True})

    telegram_handle = (from_user.get("username") or "").strip().lower()
    chat_id = str(chat.get("id") or "")
    if not telegram_handle or not chat_id:
        return jsonify({"ok": True})

    linked = link_telegram_registration(telegram_handle, chat_id)
    db.session.commit()

    if linked:
        current_app.logger.info("Linked Telegram chat %s to @%s", chat_id, telegram_handle)
    else:
        current_app.logger.info("Stored pending Telegram registration for @%s", telegram_handle)

    return jsonify({"ok": True})