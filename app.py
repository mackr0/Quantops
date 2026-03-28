"""Flask application factory for QuantOpsAI web dashboard."""

import os
from flask import Flask
from flask_login import LoginManager

from models import init_user_db, get_user_by_id


class User:
    """Flask-Login user wrapper around user dict from models.py."""

    def __init__(self, user_dict):
        self.data = user_dict
        self.id = user_dict["id"]
        self.email = user_dict["email"]
        self.display_name = user_dict.get("display_name", "")
        self.is_admin = bool(user_dict.get("is_admin", 0))

    @property
    def is_authenticated(self):
        return True

    @property
    def is_active(self):
        return bool(self.data.get("is_active", 1))

    def get_id(self):
        return str(self.id)


def create_app():
    app = Flask(__name__)
    app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-me-in-production")

    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message_category = "info"

    @login_manager.user_loader
    def load_user(user_id):
        user = get_user_by_id(int(user_id))
        if user:
            return User(user)
        return None

    # Register blueprints
    from auth import auth_bp
    from views import views_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(views_bp)

    # Init DB
    init_user_db()

    return app


if __name__ == "__main__":
    create_app().run(debug=True, port=5000)
