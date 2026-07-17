# Управление галереей vibe-изображений: загрузка и удаление с CSRF-защитой.

import os
import re
import uuid
import hmac
import secrets
from flask import Blueprint, render_template, request, redirect, session

images_bp = Blueprint("images", __name__)

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "bot/images/vibe_images")

ALLOWED_EXT = {"jpg", "jpeg", "png", "gif"}


# Показывает список изображений и обрабатывает удаление/загрузку.
@images_bp.route("/images", methods=["GET", "POST"])
def images():

    if "username" not in session:
        return redirect("/login")

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    session.setdefault("csrf_token", secrets.token_urlsafe(32))

    if request.method == "POST":
        sent_token = request.form.get("csrf_token", "")
        session_token = session.get("csrf_token", "")
        if not sent_token or not session_token or not hmac.compare_digest(sent_token, session_token):
            return redirect("/images?csrf=0")

        action = request.form.get("action")

        if action == "upload":
            file = request.files.get("image")
            if file and file.filename:
                ext = file.filename.rsplit(".", 1)[-1].lower()
                if ext in ALLOWED_EXT:
                    new_name = f"img_{uuid.uuid4().hex}.{ext}"
                    target = os.path.join(UPLOAD_DIR, new_name)
                    file.save(target)
                    return redirect("/images?uploaded=1")
            return redirect("/images?uploaded=0")

        if action == "delete":
            filename = os.path.basename(request.form.get("filename", ""))
            full_path = os.path.join(UPLOAD_DIR, filename)
            if os.path.isfile(full_path):
                os.remove(full_path)
                return redirect("/images?deleted=1")
            return redirect("/images?deleted=0")

        return redirect("/images")

    images_list = [
        f for f in os.listdir(UPLOAD_DIR)
        if re.search(r"\.(jpe?g|png|gif)$", f, re.I)
    ]

    images_list.sort()

    return render_template(
        "images.html",
        images=images_list,
        uploaded=request.args.get("uploaded"),
        deleted=request.args.get("deleted"),
        csrf=request.args.get("csrf"),
        csrf_token=session["csrf_token"],
    )
