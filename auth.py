"""Authentication blueprint — login, register, logout."""

from contextlib import closing
from datetime import datetime

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort,
)
from flask_login import login_user, logout_user, login_required

from models import (
    get_user_by_email, verify_password, _get_conn,
)
from app import User

auth_bp = Blueprint("auth", __name__, template_folder="templates")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        if not email or not password:
            flash("Email and password are required.", "error")
            return render_template("auth/login.html")

        user = get_user_by_email(email)
        if user and verify_password(user, password):
            if not user.get("is_active", 1):
                flash("Your account has been deactivated.", "error")
                return render_template("auth/login.html")
            # Update last login timestamp
            with closing(_get_conn()) as conn:
                conn.execute(
                    "UPDATE users SET last_login_at = ? WHERE id = ?",
                    (datetime.utcnow().isoformat(), user["id"]),
                )
                conn.commit()

            login_user(User(user), remember=True)
            next_page = request.args.get("next")
            return redirect(next_page or url_for("views.dashboard"))
        else:
            flash("Invalid email or password.", "error")
            return render_template("auth/login.html")

    return render_template("auth/login.html")


# 2026-06-15 — PUBLIC SELF-REGISTRATION DISABLED. This is a
# single-operator system; accounts are created manually by the
# operator (models.create_user, via migrate.py / a script / direct
# DB), never through the web. The route is kept as an explicit
# abort(404) — rather than deleted — so the intent is documented in
# code and pinned by a guardrail test, and so a casual re-add is
# obvious. abort(404) (not 403) leaks no information that a
# registration endpoint ever existed. Both GET and POST 404, so a
# direct POST can't bypass the removed login-page link.
@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    abort(404)


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))
