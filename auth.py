"""Authentication blueprint — login, register, logout."""

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user, login_required

from models import (
    get_user_by_email, verify_password, create_user,
    create_default_segment_configs,
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
            login_user(User(user), remember=True)
            next_page = request.args.get("next")
            return redirect(next_page or url_for("views.dashboard"))
        else:
            flash("Invalid email or password.", "error")
            return render_template("auth/login.html")

    return render_template("auth/login.html")


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        display_name = request.form.get("display_name", "").strip()

        if not email or not password:
            flash("Email and password are required.", "error")
            return render_template("auth/register.html")

        if password != confirm:
            flash("Passwords do not match.", "error")
            return render_template("auth/register.html")

        if len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return render_template("auth/register.html")

        existing = get_user_by_email(email)
        if existing:
            flash("An account with that email already exists.", "error")
            return render_template("auth/register.html")

        try:
            user_id = create_user(email, password, display_name=display_name)
            create_default_segment_configs(user_id)
            user = get_user_by_email(email)
            login_user(User(user), remember=True)
            flash("Account created successfully. Configure your API keys in Settings.", "success")
            return redirect(url_for("views.settings"))
        except Exception as exc:
            flash(f"Registration failed: {exc}", "error")
            return render_template("auth/register.html")

    return render_template("auth/register.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))
