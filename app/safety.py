"""Hard safety policy for the Render deployment branch."""

# The safe deployment does not automate joining Telegram groups or bulk messaging.
TELEGRAM_USER_ACCOUNT_ENABLED = True
AUTOMATED_GROUP_JOINS_ENABLED = True
BULK_BROADCASTS_ENABLED = False
