from telegram.ext import ConversationHandler

# Main menu states
(
    MAIN_MENU,
    ADD_RULE_SOURCE,
    ADD_RULE_DEST,
    ADD_RULE_CONFIRM,
    DELETE_RULE_SELECT,
    IGNORE_ADD_CHAT,
    IGNORE_REMOVE_SELECT,
    FORWARD_HISTORY_SOURCE,
    FORWARD_HISTORY_DEST,
    FORWARD_HISTORY_LIMIT,
) = range(10)

# Copy / dryrun / sync wizard states (used by handlers/copybot.py)
COPY_AWAIT_SRC     = 10
COPY_AWAIT_DST     = 11
COPY_OPTIONS       = 12
COPY_AWAIT_REPLACE = 13

# In-bot userbot login wizard states (used by handlers/login.py)
LOGIN_PHONE = 14
LOGIN_OTP   = 15
LOGIN_2FA   = 16

# Caption-preview wizard state (used by handlers/preview.py)
PREVIEW_AWAIT_MSG = 17

# Second userbot login wizard states (used by handlers/login2.py)
LOGIN2_PHONE = 18
LOGIN2_OTP   = 19
LOGIN2_2FA   = 20

# Dual-bot copy wizard states (used by handlers/dualcopy.py)
DUAL_AWAIT_SRC    = 21
DUAL_AWAIT_DST    = 22
DUAL_OPTIONS      = 23
DUAL_AWAIT_REPLACE = 24

# String-session import states (used by handlers/login.py and login2.py)
LOGIN_STRING  = 25   # Bot 1: waiting for pasted Telethon string session
LOGIN2_STRING = 26   # Bot 2: waiting for pasted Telethon string session
