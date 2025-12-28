# Discord Bot - Lunar Tierlist

## Overview
This is a Discord bot for managing Minecraft tierlists and player testing queues. The bot provides features for:
- User profile registration with Minecraft IGN, account type, and region
- Queue management for different gamemodes (Netherite, Potion, Sword, Crystal, UHC, SMP, DiaSMP, Axe, Mace)
- Tier testing and result submission
- Role management for testers and players

## Project Structure
```
backend/
  discord_bot.py    # Main bot file with all commands and handlers
  requirements.txt  # Python dependencies
```

## Running the Bot
The bot runs as a console application using Python. It requires a `DISCORD_TOKEN` environment variable to authenticate with Discord.

## Environment Variables
- `DISCORD_TOKEN` - Discord bot token (required)

## Dependencies
- discord.py - Discord API wrapper
- aiohttp - Async HTTP client for Minecraft skin fetching
- requests - HTTP library
- python-dotenv - Environment variable loading

## Recent Changes
- Initial setup for Replit environment (December 2024)
