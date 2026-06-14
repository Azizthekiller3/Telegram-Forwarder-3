# Telegram-Forwarder-3 Project Context

## Overview
Telegram forwarding bot with dual userbot support.

## Completed Features
- Dual userbot support
- Login menu shows connection status for both bots
- Import existing Telethon string sessions
- Adaptive FloodWait backoff

## Hosting
- Replit

## GitHub Repository
- Azizthekiller3/Telegram-Forwarder-3

## Important Files
- login2.py
- main.py
- handlers/
- telegram-bot/

## Known Issues
- FloodWait may still happen during heavy forwarding
- Session files (*.session) should never be committed

## Future Tasks
- Add /uptime command
- Improve error logging
- Better FloodWait handling

## Instructions For Future Agents
Read this file before making any code changes.