from app import app, db
from models import User
from werkzeug.security import generate_password_hash

with app.app_context():
    # ✅ Create admin user if it doesn't exist
    admin = User.query.filter_by(username="admin").first()
    if not admin:
        admin = User(username="admin", role="admin")
        admin.set_password("adminpass")  # ✅ Hash password correctly
        db.session.add(admin)
        print("Admin user created.")

    # ✅ Create store users
    users = [
        {"username": "Clayton", "password": "userpass"},
        {"username": "Glen Waverley", "password": "userpass"},
        {"username": "Doncaster", "password": "userpass"},
    ]

    for user_data in users:
        # ✅ Check if user already exists
        existing_user = User.query.filter_by(username=user_data["username"]).first()
        if existing_user:
            print(f"User '{user_data['username']}' already exists. Skipping.")
            continue  # ✅ Skip to the next user

        # ✅ Create new user
        user = User(username=user_data["username"], role="user")
        user.set_password(user_data["password"])  # ✅ Use set_password() method instead
        db.session.add(user)
        print(f"Added user: {user.username}")

    # ✅ Commit all new users to the database
    db.session.commit()
    print("Users created successfully and saved to the database!")
