import os

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY") or os.getenv("FLASK_SECRET_KEY")
    SQLALCHEMY_DATABASE_URI = os.getenv("SQLALCHEMY_DATABASE_URI") or os.getenv("DATABASE_URL")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Flask-Mail Configuration for Sending Emails
    MAIL_SERVER = "smtp.gmail.com"  # Change if using another provider (e.g., Outlook, SMTP relay)
    MAIL_PORT = 587  # TLS port
    MAIL_USE_TLS = True
    MAIL_USE_SSL = False
    MAIL_USERNAME = os.getenv("MAIL_USERNAME", "binginvoice@gmail.com")
    MAIL_PASSWORD = os.getenv("MAIL_PASSWORD")
    MAIL_DEFAULT_SENDER = os.getenv("MAIL_DEFAULT_SENDER", MAIL_USERNAME)  # Sender email

    # Xero Invoice Emails (Mapping Stores to Their Emails)
    STORE_EMAILS = {
        "Doncaster": "bills.os6yj1.soewzjogaynjfpqb@xerofiles.com",
        "Lonsdale": "bills.ogq93p.q13ke638rncqwtd0@xerofiles.com",
        "Clayton": "bills.olc5y0.uyvcqxramjr8hcse@xerofiles.com",
        "Glen Waverley": "bills.otgt4t.rwtpiwemiagfw20t@xerofiles.com",
    }
