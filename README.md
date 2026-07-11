---
title: Discord Suggestion Bot
emoji: 🤖
colorFrom: blue
colorTo: purple
sdk: docker
pinned: false
---

# Discord Suggestion Bot

A Discord bot that converts messages in a specific channel into formatted suggestion embeds with voting reactions.

## Features

- Converts user messages into styled "NEW PROPOSAL" embeds
- Adds voting reactions (👍 👎 😊)
- Displays vote counts in the embed footer
- Shows author name, avatar, and timestamp
- Automatically updates vote counts when users react

## Setup

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Create a Discord Application:**
   - Go to https://discord.com/developers/applications
   - Click "New Application" and create a bot
   - Enable "MESSAGE CONTENT INTENT" in the Bot tab
   - Copy the bot token

3. **Invite the bot to your server:**
   - Go to the OAuth2 > URL Generator tab
   - Select scopes: `bot`
   - Select permissions: `Send Messages`, `Embed Links`, `Add Reactions`, `Read Message History`, `Manage Messages`
   - Copy the generated URL and open it in your browser to invite the bot

4. **Configure the bot:**
   - Copy `.env.example` to `.env`
   - Fill in your bot token and suggestion channel ID:
     ```
     DISCORD_TOKEN=your_bot_token_here
     SUGGESTION_CHANNEL_ID=your_channel_id_here
     ```

5. **Get your channel ID:**
   - Enable Developer Mode in Discord (User Settings > Advanced)
   - Right-click on the suggestion channel and select "Copy ID"

6. **Run the bot:**
   ```bash
   python bot.py
   ```

## Usage

1. Set up a dedicated channel for suggestions
2. Add the channel ID to your `.env` file
3. When users type in that channel, their message will be converted to a suggestion embed
4. Users can vote using the reaction buttons
5. Vote counts update automatically in the embed footer

## Example

The bot creates embeds that look like:
```
📝 NEW PROPOSAL
[your suggestion text here]

[Author Name] • [Timestamp]
👍 5 | 👎 2 | 😊 3 | Vote using reactions below!
```
