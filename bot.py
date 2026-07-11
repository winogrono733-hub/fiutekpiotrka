import os
import asyncio
import discord
import io
import datetime
import aiohttp
import sqlite3
import random
from discord.ext import commands
from discord import ui
from dotenv import load_dotenv
from aiohttp import web

# Firebase imports (optional)
try:
    import firebase_admin
    from firebase_admin import credentials, db
    firebase_available = True
except ImportError:
    firebase_available = False
    print("Firebase not available - license connection system will be disabled")

load_dotenv()

# Initialize API keys
google_api_key = os.getenv('GOOGLE_API_KEY')
groq_api_key = os.getenv('GROQ_API_KEY')

# OAuth2 Configuration
oauth2_client_id = os.getenv('OAUTH2_CLIENT_ID', '1503476979893141504')
oauth2_client_secret = os.getenv('OAUTH2_CLIENT_SECRET', 'jqBGmIn8vN36eSmHXrBsCDaZt90JxIj4')
oauth2_redirect_uri = os.getenv('OAUTH2_REDIRECT_URI', 'https://ana-client-production-86f3.up.railway.app/callback')

# Role IDs for verification
verified_role_id = int(os.getenv('VERIFIED_ROLE_ID', '1511087435423944775'))
unverified_role_id = int(os.getenv('UNVERIFIED_ROLE_ID', '1511087311088255136'))
guild_id = int(os.getenv('GUILD_ID', '1510975485331636405'))
giveaway_manager_role_id = int(os.getenv('GIVEAWAY_MANAGER_ROLE_ID', '1511082939700613362'))
giveaway_channel_id = int(os.getenv('GIVEAWAY_CHANNEL_ID', '1521052315757318174'))

# Firebase Configuration
firebase_credentials_path = os.getenv('FIREBASE_CREDENTIALS_PATH', 'firebase-credentials.json')
firebase_database_url = os.getenv('FIREBASE_DATABASE_URL', 'https://a-client-e64d3-default-rtdb.firebaseio.com/')

# Initialize Firebase (only if available)
firebase_db = None
if firebase_available:
    try:
        if not firebase_admin._apps:
            cred = credentials.Certificate(firebase_credentials_path)
            firebase_admin.initialize_app(cred, {
                'databaseURL': firebase_database_url
            })
        firebase_db = db.reference()
        print("Firebase initialized successfully")
    except Exception as e:
        print(f"Firebase initialization error: {e}")
        firebase_db = None

# Initialize SQLite database for suggestions
def init_db():
    # Use persistent volume path for Railway
    db_path = os.getenv('DB_PATH', 'suggestions.db')
    db_dir = os.path.dirname(db_path)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)

    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS suggestions
                 (message_id TEXT PRIMARY KEY,
                  upvotes INTEGER DEFAULT 0,
                  downvotes INTEGER DEFAULT 0,
                  status TEXT DEFAULT 'Oczekuje na decyzję',
                  voters TEXT DEFAULT '')''')
    c.execute('''CREATE TABLE IF NOT EXISTS verified_users
                 (user_id TEXT PRIMARY KEY,
                  verified_at TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS giveaway_cooldowns
                 (user_id TEXT PRIMARY KEY,
                  last_roll TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS legit_check_count
                 (count INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS legit_check_last_message
                 (message_id TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS polacz_panel_message
                 (message_id TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS oauth2_tokens
                 (user_id TEXT PRIMARY KEY,
                  access_token TEXT,
                  refresh_token TEXT,
                  expires_at TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS giveaways
                 (message_id TEXT PRIMARY KEY,
                  prize TEXT,
                  end_time TIMESTAMP,
                  winner_count INTEGER,
                  requirements TEXT,
                  participants TEXT,
                  channel_id TEXT)''')

    # Add channel_id column if it doesn't exist (for existing databases)
    try:
        c.execute('''ALTER TABLE giveaways ADD COLUMN channel_id TEXT''')
    except sqlite3.OperationalError:
        pass  # Column already exists
    conn.commit()
    conn.close()

init_db()

def save_suggestion_state(message_id, upvotes, downvotes, status, voters):
    db_path = os.getenv('DB_PATH', 'suggestions.db')
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    voters_str = ','.join(map(str, voters))
    c.execute('''INSERT OR REPLACE INTO suggestions
                 (message_id, upvotes, downvotes, status, voters)
                 VALUES (?, ?, ?, ?, ?)''',
                 (str(message_id), upvotes, downvotes, status, voters_str))
    conn.commit()
    conn.close()

def load_suggestion_state(message_id):
    db_path = os.getenv('DB_PATH', 'suggestions.db')
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''SELECT upvotes, downvotes, status, voters FROM suggestions WHERE message_id = ?''',
                 (str(message_id),))
    row = c.fetchone()
    conn.close()
    if row:
        voters = set(map(int, row[3].split(','))) if row[3] else set()
        return row[0], row[1], row[2], voters
    return None

def is_user_verified(user_id):
    db_path = os.getenv('DB_PATH', 'suggestions.db')
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''SELECT user_id FROM verified_users WHERE user_id = ?''', (str(user_id),))
    row = c.fetchone()
    conn.close()
    return row is not None

def verify_user(user_id):
    db_path = os.getenv('DB_PATH', 'suggestions.db')
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO verified_users (user_id, verified_at) VALUES (?, ?)''',
             (str(user_id), datetime.datetime.now().isoformat()))
    conn.commit()
    conn.close()

def can_roll_giveaway(user_id):
    db_path = os.getenv('DB_PATH', 'suggestions.db')
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''SELECT last_roll FROM giveaway_cooldowns WHERE user_id = ?''', (str(user_id),))
    row = c.fetchone()
    conn.close()

    if not row:
        return True

    last_roll = datetime.datetime.fromisoformat(row[0])
    time_since_roll = datetime.datetime.now() - last_roll
    return time_since_roll >= datetime.timedelta(hours=24)

def update_giveaway_cooldown(user_id):
    db_path = os.getenv('DB_PATH', 'suggestions.db')
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO giveaway_cooldowns (user_id, last_roll) VALUES (?, ?)''',
             (str(user_id), datetime.datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_legit_check_count():
    db_path = os.getenv('DB_PATH', 'suggestions.db')
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''SELECT count FROM legit_check_count''')
    row = c.fetchone()
    conn.close()
    if row:
        return row[0]
    return 0

def increment_legit_check_count():
    db_path = os.getenv('DB_PATH', 'suggestions.db')
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''SELECT count FROM legit_check_count''')
    row = c.fetchone()
    if row:
        new_count = row[0] + 1
        c.execute('''UPDATE legit_check_count SET count = ?''', (new_count,))
    else:
        new_count = 1
        c.execute('''INSERT INTO legit_check_count (count) VALUES (?)''', (new_count,))
    conn.commit()
    conn.close()
    return new_count

def get_legit_check_last_message():
    db_path = os.getenv('DB_PATH', 'suggestions.db')
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''SELECT message_id FROM legit_check_last_message''')
    row = c.fetchone()
    conn.close()
    if row:
        return row[0]
    return None

def update_legit_check_last_message(message_id):
    db_path = os.getenv('DB_PATH', 'suggestions.db')
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''DELETE FROM legit_check_last_message''')
    c.execute('''INSERT INTO legit_check_last_message (message_id) VALUES (?)''', (str(message_id),))
    conn.commit()
    conn.close()

def save_oauth2_token(user_id, access_token, refresh_token, expires_at):
    db_path = os.getenv('DB_PATH', 'suggestions.db')
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO oauth2_tokens
                 (user_id, access_token, refresh_token, expires_at)
                 VALUES (?, ?, ?, ?)''',
                 (str(user_id), access_token, refresh_token, expires_at.isoformat()))
    conn.commit()
    conn.close()

def get_oauth2_token(user_id):
    db_path = os.getenv('DB_PATH', 'suggestions.db')
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''SELECT access_token, refresh_token, expires_at FROM oauth2_tokens WHERE user_id = ?''', (str(user_id),))
    row = c.fetchone()
    conn.close()
    if row:
        return row[0], row[1], datetime.datetime.fromisoformat(row[2])
    return None

def save_giveaway(message_id, prize, end_time, winner_count, requirements, participants, channel_id):
    db_path = os.getenv('DB_PATH', 'suggestions.db')
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO giveaways
                 (message_id, prize, end_time, winner_count, requirements, participants, channel_id)
                 VALUES (?, ?, ?, ?, ?, ?, ?)''',
                 (str(message_id), prize, end_time.isoformat(), winner_count, requirements, ','.join(map(str, participants)), str(channel_id)))
    conn.commit()
    conn.close()

def get_giveaway(message_id):
    db_path = os.getenv('DB_PATH', 'suggestions.db')
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''SELECT prize, end_time, winner_count, requirements, participants, channel_id FROM giveaways WHERE message_id = ?''', (str(message_id),))
    row = c.fetchone()
    conn.close()
    if row:
        participants = list(map(int, row[4].split(','))) if row[4] else []
        return row[0], datetime.datetime.fromisoformat(row[1]), row[2], row[3], participants, row[5]
    return None

def save_polacz_panel_message(message_id):
    db_path = os.getenv('DB_PATH', 'suggestions.db')
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO polacz_panel_message (message_id) VALUES (?)''', (str(message_id),))
    conn.commit()
    conn.close()

def get_polacz_panel_message():
    db_path = os.getenv('DB_PATH', 'suggestions.db')
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''SELECT message_id FROM polacz_panel_message''')
    row = c.fetchone()
    conn.close()
    if row:
        return row[0]
    return None

def delete_giveaway(message_id):
    db_path = os.getenv('DB_PATH', 'suggestions.db')
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''DELETE FROM giveaways WHERE message_id = ?''', (str(message_id),))
    conn.commit()
    conn.close()

async def check_and_end_giveaways():
    """Check for ended giveaways and select winners"""
    db_path = os.getenv('DB_PATH', 'suggestions.db')
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''SELECT message_id, prize, end_time, winner_count, requirements, participants, channel_id FROM giveaways''')
    rows = c.fetchall()
    conn.close()

    current_time = datetime.datetime.now()

    for row in rows:
        message_id, prize, end_time, winner_count, requirements, participants, channel_id = row
        participants_list = list(map(int, participants.split(','))) if participants else []
        end_datetime = datetime.datetime.fromisoformat(end_time)

        if end_datetime <= current_time:
            # Giveaway has ended, select winners
            if participants_list:
                winners = random.sample(participants_list, min(winner_count, len(participants_list)))
                winner_mentions = [f"<@{winner_id}>" for winner_id in winners]

                # Try to send new message with results
                try:
                    channel = bot.get_channel(int(channel_id))
                    if channel:
                        # Create new message with results
                        result_embed = discord.Embed(
                            title="🎁 ANA CLIENT × GIVEAWAY ZAKOŃCZONY",
                            description=f"**Nagroda:** {prize}",
                            color=0x000000
                        )
                        result_embed.add_field(name="🏆 Zwycięzcy", value=', '.join(winner_mentions), inline=False)
                        result_embed.set_image(url="https://cdn.discordapp.com/attachments/1492544749603786813/1515786374664556574/cOHuSwH.png?ex=6a304591&is=6a2ef411&hm=16007f7fd078b2bc6c4a71dfe026e7f1b8fa648c0635bbf37cf86ed8637ce496&")
                        result_embed.set_footer(text="ANA CLIENT • Giveaway System")

                        await channel.send(embed=result_embed)

                        # Also update original message to show it's ended
                        message = await channel.fetch_message(int(message_id))
                        embed = message.embeds[0]
                        embed.set_footer(text="ANA CLIENT • Giveaway Zakończony")
                        await message.edit(embed=embed, view=None)
                except Exception as e:
                    pass

            # Delete from database
            delete_giveaway(message_id)

async def giveaway_checker():
    """Background task to check for ended giveaways every 10 seconds"""
    await bot.wait_until_ready()
    while not bot.is_closed():
        await check_and_end_giveaways()
        await asyncio.sleep(10)  # Check every 10 seconds

def generate_oauth2_url(state=None):
    """Generate OAuth2 authorization URL"""
    params = {
        'client_id': oauth2_client_id,
        'redirect_uri': oauth2_redirect_uri,
        'response_type': 'code',
        'scope': 'identify',
    }
    if state:
        params['state'] = state

    from urllib.parse import urlencode
    base_url = "https://discord.com/oauth2/authorize"
    return f"{base_url}?{urlencode(params)}"

async def exchange_code_for_token(code):
    """Exchange authorization code for access token"""
    data = {
        'client_id': oauth2_client_id,
        'client_secret': oauth2_client_secret,
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': oauth2_redirect_uri,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post('https://discord.com/api/oauth2/token', data=data) as response:
            if response.status == 200:
                token_data = await response.json()
                access_token = token_data['access_token']
                refresh_token = token_data.get('refresh_token')
                expires_in = token_data['expires_in']
                expires_at = datetime.datetime.now() + datetime.timedelta(seconds=expires_in)
                return access_token, refresh_token, expires_at
    return None, None, None

async def get_user_info(access_token):
    """Get user info using access token"""
    headers = {'Authorization': f'Bearer {access_token}'}
    async with aiohttp.ClientSession() as session:
        async with session.get('https://discord.com/api/users/@me', headers=headers) as response:
            if response.status == 200:
                return await response.json()
    return None

import random

class VerificationModal(discord.ui.Modal, title="Weryfikacja"):
    def __init__(self):
        super().__init__()

        num1 = random.randint(1, 10)
        num2 = random.randint(1, 10)
        self.correct_answer = num1 + num2

        self.add_item(discord.ui.TextInput(
            label=f"Ile to jest: {num1} + {num2}?",
            placeholder="Wpisz odpowiedź...",
            required=True,
            style=discord.TextStyle.short
        ))

    async def on_submit(self, interaction: discord.Interaction):
        user_answer = self.children[0].value
        try:
            if int(user_answer) == self.correct_answer:
                verify_user(interaction.user.id)

                # Assign roles
                try:
                    guild = interaction.guild
                    if guild:
                        # Add verified role
                        verified_role = guild.get_role(verified_role_id)
                        if verified_role:
                            await interaction.user.add_roles(verified_role)

                        # Remove unverified role
                        unverified_role = guild.get_role(unverified_role_id)
                        if unverified_role:
                            await interaction.user.remove_roles(unverified_role)

                    await interaction.response.send_message("✅ Pomyślnie zweryfikowano!", ephemeral=True)
                except Exception as e:
                    await interaction.response.send_message(f"❌ Błąd przy nadawaniu ról: {str(e)}", ephemeral=True)
            else:
                await interaction.response.send_message("❌ Błędna odpowiedź! Spróbuj ponownie.", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("❌ Wpisz liczbę!", ephemeral=True)

class GiveawayModal(discord.ui.Modal, title="Stwórz Giveaway"):
    def __init__(self):
        super().__init__()

        self.add_item(discord.ui.TextInput(
            label="Nagroda",
            placeholder="np. Licencja lifetime, 50 zł, VIP role",
            required=True,
            style=discord.TextStyle.short
        ))

        self.add_item(discord.ui.TextInput(
            label="Kiedy konkurs ma wygasnąć",
            placeholder="np. 1min, 1h, 24h, 7d",
            required=True,
            style=discord.TextStyle.short
        ))

        self.add_item(discord.ui.TextInput(
            label="Ile osób ma wygrać",
            placeholder="np. 1, 3, 5",
            required=True,
            style=discord.TextStyle.short
        ))

        self.add_item(discord.ui.TextInput(
            label="Wymagania",
            placeholder="np. Musisz być na serwerze od 7 dni",
            required=False,
            style=discord.TextStyle.paragraph
        ))

    async def on_submit(self, interaction: discord.Interaction):
        prize = self.children[0].value
        end_time = self.children[1].value
        winner_count = self.children[2].value
        requirements = self.children[3].value

        try:
            winners = int(winner_count)
        except ValueError:
            await interaction.response.send_message("❌ Nieprawidłowa liczba zwycięzców!", ephemeral=True)
            return

        # Calculate end time
        time_map = {'h': 3600, 'd': 86400, 'm': 60, 's': 1}
        duration = 0
        for unit, seconds in time_map.items():
            if end_time.endswith(unit):
                try:
                    duration = int(end_time[:-1]) * seconds
                    break
                except ValueError:
                    pass

        if duration == 0:
            await interaction.response.send_message("❌ Nieprawidłowy format czasu! Użyj np. 1h, 24h, 7d", ephemeral=True)
            return

        end_timestamp = datetime.datetime.now() + datetime.timedelta(seconds=duration)

        embed = discord.Embed(
            title="🎁 ANA CLIENT × GIVEAWAY",
            description=f"**Nagroda:** {prize}",
            color=0x000000
        )

        embed.add_field(name="", value="━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", inline=False)
        embed.add_field(name="⏰ Koniec", value=f"<t:{int(end_timestamp.timestamp())}:R>", inline=True)
        embed.add_field(name="👥 Zwycięzców", value=f"{winners}", inline=True)
        embed.add_field(name="", value="━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", inline=False)

        if requirements:
            embed.add_field(name="📋 Wymagania", value=requirements, inline=False)

        embed.set_image(url="https://cdn.discordapp.com/attachments/1492544749603786813/1515786374664556574/cOHuSwH.png?ex=6a304591&is=6a2ef411&hm=16007f7fd078b2bc6c4a71dfe026e7f1b8fa648c0635bbf37cf86ed8637ce496&")
        embed.set_footer(text="ANA CLIENT • Giveaway System")

        # Create custom view for this giveaway
        class CustomGiveawayView(discord.ui.View):
            def __init__(self, end_time, prize, message_id=None, participants=None, winner_count=None, requirements=None):
                super().__init__(timeout=None)
                self.end_time = end_time
                self.prize = prize
                self.message_id = message_id
                self.participants = participants if participants else []
                self.winner_count = winner_count
                self.requirements = requirements

            @discord.ui.button(label="🎁 Weź udział", style=discord.ButtonStyle.green)
            async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                user_id = interaction.user.id
                if user_id not in self.participants:
                    self.participants.append(user_id)
                    if self.message_id:
                        save_giveaway(self.message_id, self.prize, self.end_time, self.winner_count, self.requirements, self.participants, interaction.channel_id)
                    await interaction.response.send_message("Zarejestrowano udział w giveaway!", ephemeral=True)
                else:
                    await interaction.response.send_message("Już bierzesz udział w tym giveaway!", ephemeral=True)

            @discord.ui.button(label="🏆 Zakończ i wylosuj", style=discord.ButtonStyle.red)
            async def end_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                # Check if user has giveaway manager role
                has_role = any(role.id == giveaway_manager_role_id for role in interaction.user.roles)
                if not has_role:
                    await interaction.response.send_message("❌ Nie masz uprawnień do zakończenia giveaway!", ephemeral=True)
                    return

                if self.participants:
                    winners = random.sample(self.participants, min(self.winner_count, len(self.participants)))
                    winner_mentions = [f"<@{winner_id}>" for winner_id in winners]
                    await interaction.response.send_message(f"🎉 Giveaway zakończony! Zwycięzcy: {', '.join(winner_mentions)}")
                else:
                    await interaction.response.send_message("❌ Brak uczestników do wylosowania!")

        view = CustomGiveawayView(end_timestamp, prize, None, [], winners, requirements)

        # Send to giveaway channel instead of current channel
        giveaway_channel = bot.get_channel(giveaway_channel_id)
        if giveaway_channel:
            sent_message = await giveaway_channel.send(embed=embed, view=view)
            view.message_id = sent_message.id
            save_giveaway(sent_message.id, prize, end_timestamp, winners, requirements, [], giveaway_channel_id)
            await interaction.response.send_message(f"✅ Giveaway stworzony na kanale {giveaway_channel.mention}!", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Nie znaleziono kanału giveaway!", ephemeral=True)

class LicenseModal(ui.Modal, title="Połącz Licencję"):
    license_key = ui.TextInput(
        label="Klucz Licencyjny",
        placeholder="Wprowadź swój klucz licencyjny",
        required=True,
        style=discord.TextStyle.short
    )

    async def on_submit(self, interaction: discord.Interaction):
        license_key = self.license_key.value
        user_id = str(interaction.user.id)
        username = interaction.user.name
        discriminator = interaction.user.discriminator

        # Check if license key starts with ANACLIENT-
        if not license_key.startswith("ANACLIENT-"):
            await interaction.response.send_message("❌ Nieprawidłowy klucz licencyjny!", ephemeral=True)
            return

        # Check if user already exists in Firebase
        if firebase_db:
            try:
                existing_user = firebase_db.child('connected_accounts').child(user_id).get()
                if existing_user:
                    await interaction.response.send_message("❌ Twoje konto jest już połączone z licencją!", ephemeral=True)
                    return
            except Exception as e:
                await interaction.response.send_message(f"❌ Błąd podczas sprawdzania połączenia: {str(e)}", ephemeral=True)
                return

        # Get user roles
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("❌ Błąd: Nie znaleziono serwera!", ephemeral=True)
            return

        # Check for specific roles
        role_names = []
        for role in interaction.user.roles:
            if role.name in ["Owner", "Media", "Customer"]:
                role_names.append(role.name)

        if not role_names:
            await interaction.response.send_message("❌ Nie masz wymaganej roli (Owner, Media, Customer)!", ephemeral=True)
            return

        # Save to Firebase
        if firebase_db:
            try:
                user_data = {
                    'discord_id': user_id,
                    'username': username,
                    'discriminator': discriminator,
                    'license_key': license_key,
                    'roles': role_names,
                    'connected_at': datetime.datetime.now().isoformat()
                }
                firebase_db.child('connected_accounts').child(user_id).set(user_data)
                await interaction.response.send_message(f"✅ Pomyślnie połączono konto z kluczem: `{license_key}`\nRole: {', '.join(role_names)}", ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(f"❌ Błąd podczas zapisywania do Firebase: {str(e)}", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Firebase nie jest dostępne!", ephemeral=True)

class VerificationView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="🔐 Zweryfikuj się", style=discord.ButtonStyle.green, custom_id="verify_button")
    async def verify_button(self, interaction: discord.Interaction, button: ui.Button):
        # Check if user actually has the verified role, not just in database
        guild = interaction.guild
        if guild:
            verified_role = guild.get_role(verified_role_id)
            if verified_role and verified_role in interaction.user.roles:
                await interaction.response.send_message("✅ Jesteś już zweryfikowany!", ephemeral=True)
                return

        modal = VerificationModal()
        await interaction.response.send_modal(modal)

class GiveawayView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="🎰 Losuj Nagrodę", style=discord.ButtonStyle.blurple, custom_id="giveaway_button", emoji="🎁")
    async def giveaway_button(self, interaction: discord.Interaction, button: ui.Button):
        user_id = interaction.user.id

        # Check if user has Owner role (unlimited rolls)
        has_owner_role = any("Owner" in role.name for role in interaction.user.roles)

        # Check cooldown (skip for Owners)
        if not has_owner_role and not can_roll_giveaway(user_id):
            conn = sqlite3.connect('suggestions.db')
            c = conn.cursor()
            c.execute('''SELECT last_roll FROM giveaway_cooldowns WHERE user_id = ?''', (str(user_id),))
            row = c.fetchone()
            conn.close()

            if row:
                last_roll = datetime.datetime.fromisoformat(row[0])
                time_remaining = datetime.timedelta(hours=24) - (datetime.datetime.now() - last_roll)
                hours, remainder = divmod(time_remaining.seconds, 3600)
                minutes, _ = divmod(remainder, 60)
                await interaction.response.send_message(f"⏰ Musisz poczekać jeszcze {hours}h {minutes}min przed kolejnym losowaniem!", ephemeral=True)
            return

        # Roll the giveaway
        import random
        roll = random.random() * 100  # 0-100

        # Very small chances for prizes
        if roll < 0.01:  # 0.01% chance for lifetime
            prize = "🎉 ANA CLIENT LIFETIME"
            prize_emoji = "👑"
        elif roll < 0.05:  # 0.04% chance for 30 days
            prize = "🎉 ANA CLIENT 30 DNI"
            prize_emoji = "💎"
        elif roll < 0.15:  # 0.10% chance for 7 days
            prize = "🎉 ANA CLIENT 7 DNI"
            prize_emoji = "🌟"
        elif roll < 0.35:  # 0.20% chance for 3 days
            prize = "🎉 ANA CLIENT 3 DNI"
            prize_emoji = "✨"
        else:  # 99.65% chance for nothing
            prize = "😢 Nic nie wygrał"
            prize_emoji = "💨"

        # Update cooldown (skip for Owners)
        if not has_owner_role:
            update_giveaway_cooldown(user_id)

        # Log giveaway result
        giveaway_log_channel_id = 1511395102373970113  # Owner log channel
        log_channel = bot.get_channel(giveaway_log_channel_id)
        if log_channel:
            log_embed = discord.Embed(
                title="🎰 Giveaway Activity Log",
                description=f"**User ID:** {user_id}",
                color=discord.Color.from_rgb(147, 51, 234)
            )
            log_embed.add_field(name="👤 User", value=f"{interaction.user.mention} (`{interaction.user.name}`)", inline=False)
            log_embed.add_field(name="🎁 Result", value=prize, inline=False)
            log_embed.add_field(name="📊 Roll Value", value=f"{roll:.4f}", inline=True)
            log_embed.add_field(name="⏰ Timestamp", value=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), inline=True)
            if has_owner_role:
                log_embed.add_field(name="👑 Privileges", value="Unlimited rolls (Owner)", inline=False)
            else:
                log_embed.add_field(name="🔒 Status", value="Standard user (24h cooldown)", inline=False)
            log_embed.set_footer(text="ANA CLIENT • Owner Logs")
            log_embed.timestamp = datetime.datetime.now()
            await log_channel.send(embed=log_embed)

        # Create result embed
        if "ANA CLIENT" in prize:
            result_embed = discord.Embed(
                title=f"{prize_emoji} GRATULACJE!",
                description=f"{interaction.user.mention} wygrał: **{prize}**",
                color=discord.Color.from_rgb(255, 215, 0)
            )
            result_embed.add_field(name="", value="━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", inline=False)
            result_embed.add_field(name="🎉 Wygrana", value=prize, inline=False)
            result_embed.add_field(name="", value="━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", inline=False)
            result_embed.set_thumbnail(url="https://cdn.discordapp.com/emojis/🎉")
            result_embed.set_footer(text="ANA CLIENT • Daily Giveaway")
            result_embed.timestamp = datetime.datetime.now()
            await interaction.response.send_message(embed=result_embed)  # Public for winners
        else:
            result_embed = discord.Embed(
                title=f"{prize_emoji} Niestety...",
                description=f"{interaction.user.mention} przegrałeś",
                color=discord.Color.from_rgb(239, 68, 68)
            )
            result_embed.add_field(name="", value="━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", inline=False)
            result_embed.add_field(name="💨", value="Spróbuj ponownie za 24 godziny!", inline=False)
            result_embed.add_field(name="", value="━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", inline=False)
            result_embed.set_thumbnail(url="https://cdn.discordapp.com/emojis/😢")
            result_embed.set_footer(text="ANA CLIENT • Daily Giveaway")
            result_embed.timestamp = datetime.datetime.now()
            await interaction.response.send_message(embed=result_embed, ephemeral=True)  # Private for losers

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

class SuggestionView(ui.View):
    def __init__(self, message_id=None):
        super().__init__(timeout=None)
        self.message_id = message_id
        self.upvotes = 0
        self.downvotes = 0
        self.status = "Oczekuje na decyzję"
        self.voters = set()

        # Load state from database if message_id is provided
        if message_id:
            state = load_suggestion_state(message_id)
            if state:
                self.upvotes, self.downvotes, self.status, self.voters = state
    
    @ui.button(label="Akceptuj", style=discord.ButtonStyle.green, emoji="✅", custom_id="suggestion_accept")
    async def accept(self, interaction: discord.Interaction, button: ui.Button):
        # Check if user has Owner role (more flexible check)
        user_roles = [role.name for role in interaction.user.roles]
        print(f"User roles: {user_roles}")
        has_owner_role = any("Owner" in role.name for role in interaction.user.roles)
        if not has_owner_role:
            await interaction.response.send_message(f"spierdalaj! Twoje role: {', '.join(user_roles)}", ephemeral=True)
            return

        self.status = "Zaakceptowano"
        await self.update_embed(interaction)

    @ui.button(label="Odrzuć", style=discord.ButtonStyle.red, emoji="❌", custom_id="suggestion_reject")
    async def reject(self, interaction: discord.Interaction, button: ui.Button):
        # Check if user has Owner role (more flexible check)
        user_roles = [role.name for role in interaction.user.roles]
        print(f"User roles: {user_roles}")
        has_owner_role = any("Owner" in role.name for role in interaction.user.roles)
        if not has_owner_role:
            await interaction.response.send_message(f"spierdalaj! Twoje role: {', '.join(user_roles)}", ephemeral=True)
            return

        self.status = "Odrzucono"
        await self.update_embed(interaction)

    @ui.button(label="UP", style=discord.ButtonStyle.blurple, emoji="👍", custom_id="suggestion_upvote")
    async def upvote(self, interaction: discord.Interaction, button: ui.Button):
        user_id = interaction.user.id
        if user_id in self.voters:
            await interaction.response.send_message("Już zagłosowałeś na tę sugestię!", ephemeral=True)
            return

        self.voters.add(user_id)
        self.upvotes += 1
        await self.update_embed(interaction)

    @ui.button(label="DOWN", style=discord.ButtonStyle.blurple, emoji="👎", custom_id="suggestion_downvote")
    async def downvote(self, interaction: discord.Interaction, button: ui.Button):
        user_id = interaction.user.id
        if user_id in self.voters:
            await interaction.response.send_message("Już zagłosowałeś na tę sugestię!", ephemeral=True)
            return
        
        self.voters.add(user_id)
        self.downvotes += 1
        await self.update_embed(interaction)
    
    async def update_embed(self, interaction: discord.Interaction):
        embed = interaction.message.embeds[0]

        # Save state to database
        if self.message_id:
            save_suggestion_state(self.message_id, self.upvotes, self.downvotes, self.status, self.voters)

        new_embed = discord.Embed(
            title=embed.title,
            description=embed.description,
            color=0x000000
        )
        new_embed.set_author(name=embed.author.name, icon_url=embed.author.icon_url)

        # Add fields with clean separators
        new_embed.add_field(name="", value="━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", inline=False)
        new_embed.add_field(name="Status", value=self.status, inline=False)
        new_embed.add_field(name="Głosy", value=f"{self.upvotes} | {self.downvotes}", inline=False)
        new_embed.add_field(name="", value="━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", inline=False)

        # Keep other fields from original embed
        if len(embed.fields) > 4:
            new_embed.add_field(name="Autor", value=embed.fields[4].value, inline=True)
        if len(embed.fields) > 5:
            new_embed.add_field(name="Data", value=embed.fields[5].value, inline=True)

        new_embed.set_footer(text="ANA CLIENT • System Sugestii")
        new_embed.timestamp = interaction.message.created_at

        await interaction.response.edit_message(embed=new_embed, view=self)

async def create_transcript(channel):
    transcript = ""
    async for message in channel.history(limit=None, oldest_first=True):
        timestamp = message.created_at.strftime("%Y-%m-%d %H:%M:%S")
        content = message.content if message.content else "[Załącznik/Embed]"
        transcript += f"[{timestamp}] {message.author.name}: {content}\n"
    return transcript

class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🤖 AI Odpowiedź", style=discord.ButtonStyle.green, custom_id="ai_response")
    async def ai_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        channel = interaction.channel

        # Get ticket context
        transcript_text = await create_transcript(channel)

        # Detect ticket category from channel name
        channel_name_lower = channel.name.lower()
        if "zakup" in channel_name_lower:
            category = "zakup"
        elif "pytanie" in channel_name_lower:
            category = "pytanie"
        elif "problem" in channel_name_lower:
            category = "problem"
        elif "pomoc" in channel_name_lower:
            category = "pomoc"
        elif "wspolpraca" in channel_name_lower:
            category = "wspolpraca"
        else:
            category = "ogólny"

        # Prepare AI prompt based on category
        if category == "zakup":
            prompt = f"""
Jesteś asystentem sprzedaży dla ANA CLIENT - premium klient do Minecraft.

TWOJA ROLA: Pomagaj klientom w zakupie licencji ANA CLIENT.

INFORMACJE O PRODUKCIE:
- Cennik: Tydzień (15 zł), Miesiąc (30 zł), Lifetime (50 zł)
- Metody płatności: PayPal, BLIK, Krypto, Paysafecard
- Niewykrywalny, regularne aktualizacje, wsparcie 24/7

INFORMACJE O SERWERZE DISCORD:
- Kanał #cennik - pełny cennik i informacje o planach
- Kanał #vouches - legit checki i opinie klientów
- Kanał #metody-płatności - szczegółowe informacje o metodach płatności
- Strona internetowa - możliwość rejestracji konta

PROCES ZAKUPU:
1. Aby kupić licencję, użytkownik MUSI pingować osobę z rangą Owner
2. Owner przeprowadzi użytkownika przez proces zakupu
3. Po zapłacie użytkownik otrzyma klucz licencyjny
4. Klucz aktywuje się w kliencie ANA CLIENT

WAŻNE ZASADY:
1. NIE dziękuj za zakup - zakup nie został jeszcze zakończony
2. ZAWSZE informuj użytkownika aby pingował Ownera aby dokończyć zakup
3. NIE mów że zakup został zakończony - tylko Owner może zakończyć zakup
4. Kiedy użytkownik jest gotowy do zakupu, powiedz mu aby pingował Ownera i czekał na odpowiedź
5. Bądź pomocny w wyborze planu i odpowiedziach na pytania
6. Odpowiadaj krótko i konkretnie

PRZYKŁADOWE ODPOWIEDZI:
- "Aby kupić licencję, pingnij osobę z rangą Owner i czekaj na odpowiedź."
- "Jesteś gotowy do zakupu? Pingnij Ownera aby dokończyć transakcję."
- "Owner przeprowadzi Cię przez proces płatności."

Kontekst ticketu:
{transcript_text[-300:]}

Odpowiedz jako profesjonalny asystent sprzedaży."""
        elif category == "pytanie":
            prompt = f"""
Jesteś asystentem informacyjnym dla ANA CLIENT - premium klient do Minecraft.

TWOJA ROLA: Odpowiadaj na pytania o ANA CLIENT.

INFORMACJE O PRODUKCIE:
- Cennik: Tydzień (15 zł), Miesiąc (30 zł), Lifetime (50 zł)
- Moduły: Aimbot, ESP, Fly, Speedhack, Killaura i wiele więcej
- Niewykrywalny przez anty-cheaty
- Wsparcie techniczne 24/7

INFORMACJE O SERWERZE DISCORD:
- Kanał #cennik - pełny cennik i informacje o planach
- Kanał #vouches - legit checki i opinie klientów
- Kanał #metody-płatności - szczegółowe informacje o metodach płatności
- Strona internetowa - możliwość rejestracji konta

PROCES ZAKUPU:
- Aby kupić licencję, musisz pingować osobę z rangą Owner
- Owner przeprowadzi Cię przez proces zakupu

ZASADY:
1. Odpowiadaj dokładnie i wyczerpująco
2. Jeśli nie wiesz odpowiedzi, powiedz to szczerze
3. Bądź pomocny i uprzejmy
4. Odpowiadaj w zwięzły sposób

Kontekst ticketu:
{transcript_text[-300:]}

Odpowiedz na pytanie użytkownika."""
        elif category == "problem":
            prompt = f"""
Jesteś technicznym wsparciem dla ANA CLIENT - premium klient do Minecraft.

TWOJA ROLA: Rozwiązyuj problemy techniczne użytkowników.

TYPowe PROBLEMY I ROZWIĄZANIA:
- Problem z instalacją: Sprawdź kompatybilność systemu, uprawnienia administratora
- Problem z kluczem: Sprawdź czy klucz jest poprawny, czy nie wygasł
- Problem z działaniem: Zaktualizuj klienta, sprawdź konfigurację
- Problem z wykrywalnością: Czekaj na aktualizację, używaj ostrożnie

INFORMACJE O SERWERZE DISCORD:
- Kanał #cennik - pełny cennik i informacje o planach
- Kanał #vouches - legit checki i opinie klientów
- Kanał #metody-płatności - szczegółowe informacje o metodach płatności
- Strona internetowa - możliwość rejestracji konta

PROCES ZAKUPU:
- Aby kupić licencję, musisz pingować osobę z rangą Owner
- Owner przeprowadzi Cię przez proces zakupu

ZASADY:
1. Krok po kroku prowadź przez rozwiązanie
2. Pytaj o szczegóły techniczne
3. Jeśli nie możesz pomóc, eskaluj do wyższego poziomu wsparcia
4. Bądź cierpliwy i empatyczny

Kontekst ticketu:
{transcript_text[-300:]}

Pomóż rozwiązać problem użytkownika."""
        elif category == "pomoc":
            prompt = f"""
Jesteś ogólnym asystentem pomocy dla ANA CLIENT.

TWOJA ROLA: Pomagaj użytkownikom w różnych sprawach związanych z ANA CLIENT.

ZAKRES POMOCY:
- Pytania o funkcje i możliwości
- Pomoc z konfiguracją
- Wyjaśnienie jak korzystać z klienta
- Wsparcie ogólne

INFORMACJE O SERWERZE DISCORD:
- Kanał #cennik - pełny cennik i informacje o planach
- Kanał #vouches - legit checki i opinie klientów
- Kanał #metody-płatności - szczegółowe informacje o metodach płatności
- Strona internetowa - możliwość rejestracji konta

PROCES ZAKUPU:
- Aby kupić licencję, musisz pingować osobę z rangą Owner
- Owner przeprowadzi Cię przez proces zakupu

ZASADY:
1. Bądź pomocny i przyjazny
2. Jeśli nie możesz pomóc, skieruj do odpowiedniego działu
3. Odpowiadaj w zrozumiały sposób
4. Bądź cierpliwy

Kontekst ticketu:
{transcript_text[-300:]}

Pomóż użytkownikowi."""
        elif category == "wspolpraca":
            prompt = f"""
Jesteś menedżerem współpracy dla ANA CLIENT.

TWOJA ROLA: Rozważaj propozycje współpracy i partnerstwa.

TYPY WSPÓŁPRACY:
- Promocja ANA CLIENT (YouTube, TikTok, Twitch)
- Partnerstwa z innymi serwerami/projektami
- Program afiliacyjny
- Reklama

INFORMACJE O SERWERZE DISCORD:
- Kanał #cennik - pełny cennik i informacje o planach
- Kanał #vouches - legit checki i opinie klientów
- Kanał #metody-płatności - szczegółowe informacje o metodach płatności
- Strona internetowa - możliwość rejestracji konta

PROCES ZAKUPU:
- Aby kupić licencję, musisz pingować osobę z rangą Owner
- Owner przeprowadzi Cię przez proces zakupu

ZASADY:
1. Bądź profesjonalny i biznesowy
2. Pytaj o szczegóły propozycji
3. Oceniaj potencjał współpracy
4. Sugeruj kolejne kroki

Kontekst ticketu:
{transcript_text[-300:]}

Rozważ propozycję współpracy."""
        else:
            prompt = f"""
Jesteś asystentem obsługi klienta dla ANA CLIENT - premium klient do Minecraft.

INFORMACJE O ANA CLIENT:
- Cennik: Tydzień (15 zł), Miesiąc (30 zł), Lifetime (50 zł)
- Płatności: PayPal, BLIK, Krypto, Paysafecard
- Niewykrywalny, aktualizacje, wsparcie 24/7

INFORMACJE O SERWERZE DISCORD:
- Kanał #cennik - pełny cennik i informacje o planach
- Kanał #vouches - legit checki i opinie klientów
- Kanał #metody-płatności - szczegółowe informacje o metodach płatności
- Strona internetowa - możliwość rejestracji konta

PROCES ZAKUPU:
- Aby kupić licencję, musisz pingować osobę z rangą Owner
- Owner przeprowadzi Cię przez proces zakupu

ZASADY:
1. ODPOWIADAJ KRÓTKO I NA TEMAT - max 2-3 zdania
2. Bądź pomocny i uprzejmy
3. Jeśli nie rozumiesz, zapytaj o szczegóły

Kontekst ticketu:
{transcript_text[-300:]}

Odpowiedz na wiadomość użytkownika."""

        try:
            # Use Groq API for AI responses
            if groq_api_key:
                url = "https://api.groq.com/openai/v1/chat/completions"
                headers = {
                    "Authorization": f"Bearer {groq_api_key}",
                    "Content-Type": "application/json"
                }

                # Prepare prompt with ANA CLIENT knowledge
                prompt = f"""Jesteś asystentem obsługi klienta dla ANA CLIENT - premium klient do Minecraft.

INFORMACJE O ANA CLIENT:
- Cennik: Tydzień (15 zł), Miesiąc (30 zł), Lifetime (50 zł)
- Płatności: PayPal, BLIK, Krypto, Paysafecard
- Niewykrywalny, aktualizacje, wsparcie 24/7

PEŁNA LISTA MODUŁÓW ANA CLIENT:

COMBAT (walka):
Cobweb Placer, Hitbox Breaker, AutoDripStone, NoFriendHurt, Auto Gapple, Pearl Chaser, AutoTotem, AutoArmor, Trigger Bot, Aim Assist, Auto Lever, HitEffect, SlotLock, BowAim, Aura

MOVEMENT (ruch):
No Jump Delay, GUI Move, Move Fix, Speed, Sprint

PLAYER (gracz):
Anti Trap, Msg Bot, NoPush, FreeCam, Discord, No Server Pack, Toggle Sounds, Lever Replace, Auto Logout, Elytra Swap, Fake Player, Sword Info, Fast Place, Better Painting, Mouse Tweaks, Name Protect, No GUI Sign, ElytraBoost, Anti Frame, Fast Break, Spammer

VISUALS (wizualne):
Swing Animation, Color Gradient, BlockOutline, Jump Circle, View Model, Name Tags, No Render, Entity ESP, Health Indicator, Logout Spots, Storage ESP, Trajectories, Motion Blur, Target ESP, Ambience, China Hat, Block ESP, Fullbright, KillEffect, Item ESP, Particles, Outlines, Trap ESP, Pointers, Tracers, Chams, X-Ray, Trials

UTILS (narzędzia):
AutoRynek, Gear Render

ZASADY:
1. ODPOWIADAJ KRÓTKO I NA TEMAT - max 2-3 zdania
2. Jeśli użytkownik pyta O MODUŁACH/FUNKCJACH ("jakie moduły", "co daje", "co robi", "funkcje", "features") - WYMIEŃ KONKRETNE MODUŁY z kategorii, nie tylko nazwy kategorii
3. Jeśli użytkownik pyta O CENĘ ("ile kosztuje", "cena", "zł") - TYLKO podaj cenę, nie wymieniaj modułów
4. Jeśli NIE ROZUMIESZ pytania, napisz: "Nie rozumiem pytania. Czy możesz sprecyzować o co pytasz?"
5. Nie wymyślaj informacji - odpowiadaj tylko na to o co pytano

Ostatnia wiadomość użytkownika:
{transcript_text[-200:]}

Odpowiedz krótko i konkretnie na pytanie."""

                data = {
                    "model": "llama-3.1-8b-instant",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.7,
                    "max_tokens": 500
                }

                print(f"Sending request to Groq API...")
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json=data, headers=headers) as response:
                        print(f"Groq API Response Status: {response.status}")
                        if response.status == 200:
                            result = await response.json()
                            ai_response = result["choices"][0]["message"]["content"].strip()
                            print(f"AI Response generated successfully")

                            embed = discord.Embed(
                                title="🤖 AI Odpowiedź",
                                description=ai_response,
                                color=0x00ff88
                            )
                            embed.set_footer(text="ANA CLIENT • Groq AI")
                            await channel.send(embed=embed)
                            return
                        else:
                            error_text = await response.text()
                            print(f"Groq API Error: {response.status} - {error_text}")
                            # Fallback to intelligent responses
                            await self.send_intelligent_response(channel, transcript_text)
            else:
                print("Groq API key not found")
                # Fallback to intelligent responses
                await self.send_intelligent_response(channel, transcript_text, category)
        except Exception as e:
            print(f"AI Error: {e}")
            import traceback
            traceback.print_exc()
            # Fallback to intelligent responses on error
            await self.send_intelligent_response(channel, transcript_text, category)

    def generate_intelligent_response(self, transcript_text, channel_name, category):
        """Generate intelligent response based on keywords and context"""
        text_lower = transcript_text.lower()
        channel_lower = channel_name.lower()

        # Category-specific responses
        if category == "zakup":
            if any(word in text_lower for word in ['cena', 'ile', 'koszt', 'zł', 'pln', 'price']):
                return """**💰 Cennik ANA CLIENT:**

• **Tydzień:** 15 zł
• **Miesiąc:** 30 zł  
• **Lifetime:** 50 zł

**Metody płatności:** PayPal, BLIK, Krypto, Paysafecard

📌 Więcej informacji na kanale #cennik

Który plan Cię interesuje?"""
            elif any(word in text_lower for word in ['płatność', 'zapłacić', 'paypal', 'blik', 'krypto', 'paysafecard']):
                return """**💳 Metody płatności:**

• **PayPal** - bezpieczne i szybkie
• **BLIK** - natychmiastowa płatność
• **Kryptowaluty** - anonimowe i bezpieczne
• **Paysafecard** - bez karty bankowej

📌 Szczegóły na kanale #metody-płatności

Wybierz metodę która Ci odpowiada!"""
            else:
                return """**🎉 Zakup ANA CLIENT:**

Dziękujemy za zainteresowanie!

**Cennik:**
• Tydzień: 15 zł
• Miesiąc: 30 zł
• Lifetime: 50 zł

**Jak kupić?**
1. Pingnij osobę z rangą Owner
2. Owner przeprowadzi Cię przez zakup
3. Zapłać
4. Otrzymasz klucz licencyjny

📌 Zarejestruj się na stronie
📌 Sprawdź vouches na kanale #vouches

Jaki plan wybierasz?"""
        elif category == "pytanie":
            if any(word in text_lower for word in ['funkcje', 'features', 'co potrafi', 'co ma', 'możliwości']):
                return """**⚡ Funkcje ANA CLIENT:**

• **Aimbot** - celowanie automatyczne
• **ESP** - widzenie przez ściany
• **Fly** - latanie
• **Speedhack** - zwiększona prędkość
• **Killaura** - automatyczny atak
• **I wiele więcej!**

📌 Sprawdź cennik na kanale #cennik
📌 Zobacz opinie na kanale #vouches

Wszystkie funkcje są bezpieczne i niewykrywalne."""
            elif any(word in text_lower for word in ['bezpieczny', 'ban', 'wykrywalny', 'detect', 'vAC']):
                return """**🛡️ Bezpieczeństwo:**

ANA CLIENT jest:
• **Niewykrywalny** przez anty-cheaty
• **Regularnie aktualizowany** 
• **Testowany** przez tysiące użytkowników
• **Bezpieczny** dla Twojego konta

📌 Sprawdź legit checki na kanale #vouches

Używaj go z głową!"""
            else:
                return """**❓ Masz pytanie?**

Nasz zespół chętnie odpowie!

Typowe tematy:
• Ceny i płatności (kanał #cennik)
• Instalacja
• Funkcje klienta
• Bezpieczeństwo
• Wsparcie techniczne

📌 Zarejestruj się na stronie
📌 Aby kupić licencję - pingnij Ownera

Zadaj swoje pytanie, a odpowiemy!"""
        elif category == "problem":
            if any(word in text_lower for word in ['instalacja', 'zainstalować', 'pobrać', 'download', 'jak', 'how']):
                return """**📥 Instalacja ANA CLIENT:**

1. Pingnij Ownera aby kupić licencję
2. Pobierz instalator
3. Uruchom i postępuj zgodnie z instrukcjami
4. Wklej klucz licencyjny
5. Gotowe!

📌 Szczegóły płatności na kanale #metody-płatności

Potrzebujesz pomocy z instalacją?"""
            elif any(word in text_lower for word in ['licencja', 'klucz', 'key', 'license', 'wygasa']):
                return """**🔑 Licencja ANA CLIENT:**

• Licencja jest przypisana do Twojego konta
• Możesz używać na jednym komputerze
• Lifetime = na zawsze
• Możliwość przedłużenia

📌 Problem z kluczem? Pingnij Ownera

Masz problem z licencją?"""
            else:
                return """**⚠️ Zgłoszenie problemu:**

Dziękujemy za zgłoszenie!

Opisz szczegóły problemu:
• Co się dzieje?
• Kiedy to się zaczęło?
• Co już próbowałeś?

📌 Sprawdź cennik na kanale #cennik
📌 Zarejestruj się na stronie

Pomożemy Ci to rozwiązać!"""
        elif category == "pomoc":
            return """**🆘 Potrzebujesz pomocy?**

Jesteśmy tu dla Ciebie!

W czym możemy pomóc:
• Instalacja i konfiguracja
• Problemy techniczne
• Pytania o funkcje
• Wsparcie

📌 Sprawdź cennik na kanale #cennik
📌 Zobacz metody płatności na #metody-płatności
📌 Zarejestruj się na stronie
📌 Aby kupić licencję - pingnij Ownera

Napisz w czym potrzebujesz pomocy!"""
        elif category == "wspolpraca":
            return """**🤝 Współpraca:**

Dziękujemy za zainteresowanie!

Możliwości:
• Promocja ANA CLIENT (YouTube, TikTok, Twitch)
• Partnerstwa z innymi serwerami/projektami
• Program afiliacyjny
• Reklama

📌 Sprawdź vouches na kanale #vouches
📌 Zarejestruj się na stronie
📌 Aby kupić licencję - pingnij Ownera

Opisz swoją propozycję współpracy!"""
        else:
            # Default responses for general category
            if any(word in text_lower for word in ['cena', 'ile', 'koszt', 'zł', 'pln', 'price']):
                return """**💰 Cennik ANA CLIENT:**

• **Tydzień:** 15 zł
• **Miesiąc:** 30 zł  
• **Lifetime:** 50 zł

**Metody płatności:** PayPal, BLIK, Krypto, Paysafecard

📌 Więcej informacji na kanale #cennik

Który plan Cię interesuje?"""
            elif any(word in text_lower for word in ['płatność', 'zapłacić', 'paypal', 'blik', 'krypto', 'paysafecard']):
                return """**💳 Metody płatności:**

• **PayPal** - bezpieczne i szybkie
• **BLIK** - natychmiastowa płatność
• **Kryptowaluty** - anonimowe i bezpieczne
• **Paysafecard** - bez karty bankowej

📌 Szczegóły na kanale #metody-płatności

Wybierz metodę która Ci odpowiada!"""
            elif any(word in text_lower for word in ['instalacja', 'zainstalować', 'pobrać', 'download', 'jak', 'how']):
                return """**📥 Instalacja ANA CLIENT:**

1. Pingnij Ownera aby kupić licencję
2. Pobierz instalator
3. Uruchom i postępuj zgodnie z instrukcjami
4. Wklej klucz licencyjny
5. Gotowe!

📌 Szczegóły płatności na kanale #metody-płatności

Potrzebujesz pomocy z instalacją?"""
            elif any(word in text_lower for word in ['funkcje', 'features', 'co potrafi', 'co ma', 'możliwości']):
                return """**⚡ Funkcje ANA CLIENT:**

• **Aimbot** - celowanie automatyczne
• **ESP** - widzenie przez ściany
• **Fly** - latanie
• **Speedhack** - zwiększona prędkość
• **Killaura** - automatyczny atak
• **I wiele więcej!**

📌 Sprawdź cennik na kanale #cennik
📌 Zobacz opinie na kanale #vouches

Wszystkie funkcje są bezpieczne i niewykrywalne."""
            elif any(word in text_lower for word in ['bezpieczny', 'ban', 'wykrywalny', 'detect', 'vAC']):
                return """**🛡️ Bezpieczeństwo:**

ANA CLIENT jest:
• **Niewykrywalny** przez anty-cheaty
• **Regularnie aktualizowany** 
• **Testowany** przez tysiące użytkowników
• **Bezpieczny** dla Twojego konta

📌 Sprawdź legit checki na kanale #vouches

Używaj go z głową!"""
            elif any(word in text_lower for word in ['pomoc', 'support', 'problem', 'nie działa', 'błąd', 'error']):
                return """**🆘 Wsparcie techniczne:**

Nasz zespół pomoże Ci z:
• Problemami z instalacją
• Błędami działania
• Problemami z płatnością
• Pytaniami o funkcje

📌 Zarejestruj się na stronie
📌 Aby kupić licencję - pingnij Ownera

Opisz szczegóły swojego problemu, a my pomożemy!"""
            elif any(word in text_lower for word in ['licencja', 'klucz', 'key', 'license', 'wygasa']):
                return """**🔑 Licencja ANA CLIENT:**

• Licencja jest przypisana do Twojego konta
• Możesz używać na jednym komputerze
• Lifetime = na zawsze
• Możliwość przedłużenia

📌 Problem z kluczem? Pingnij Ownera

Masz problem z licencją?"""
            else:
                return """**👋 Witaj!**

Dziękujemy za kontakt z ANA CLIENT!

W czym możemy Ci pomóc?
• Zakup licencji (pingnij Ownera)
• Pytania o klienta
• Wsparcie techniczne
• Współpraca

📌 Sprawdź cennik na kanale #cennik
📌 Zobacz vouches na kanale #vouches
📌 Zarejestruj się na stronie

Napisz swoje pytanie lub potrzebę!"""

    async def send_intelligent_response(self, channel, transcript_text, category):
        # Use the intelligent response generation with category
        response = self.generate_intelligent_response(transcript_text, channel.name, category)

        embed = discord.Embed(
            title="🤖 Inteligentna Odpowiedź",
            description=response,
            color=0x00ff88
        )
        embed.set_footer(text="ANA CLIENT • Smart Response")
        await channel.send(embed=embed)

    @discord.ui.button(label="Zamknij Ticket", style=discord.ButtonStyle.red, custom_id="close_ticket")
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        channel = interaction.channel
        guild = interaction.guild
        transcript_text = await create_transcript(channel)
        log_channel = discord.utils.get(guild.text_channels, name="historia-ticketow")
        if not log_channel:
            overwrites = {guild.default_role: discord.PermissionOverwrite(read_messages=False)}
            log_channel = await guild.create_text_channel(name="historia-ticketow", overwrites=overwrites)
        file_data = io.BytesIO(transcript_text.encode("utf-8"))
        file = discord.File(file_data, filename=f"transcript-{channel.name}.txt")
        embed = discord.Embed(
            title="📂 Historia Ticketa",
            description=f"Ticket **{channel.name}** został zamknięty przez **{interaction.user.name}**.",
            color=discord.Color.dark_grey(),
            timestamp=datetime.datetime.now()
        )
        await log_channel.send(embed=embed, file=file)
        await channel.send("🔒 Historia została zapisana. Zamykanie za 5 sekund...")
        async def delete_channel():
            await discord.utils.sleep_until(datetime.datetime.now() + datetime.timedelta(seconds=5))
            await channel.delete()
        asyncio.create_task(delete_channel())

class TicketModal(discord.ui.Modal, title="Tworzenie Ticketa"):
    def __init__(self, category):
        super().__init__()
        self.category = category

        if category == "zakup":
            self.add_item(discord.ui.TextInput(label="Na ile chcesz kupić klienta?", style=discord.TextStyle.short, placeholder="np. 1 miesiąc, 3 miesiące..."))
            self.add_item(discord.ui.TextInput(label="Metoda płatności", style=discord.TextStyle.short, placeholder="np. PayPal, BLIK, krypto..."))
        elif category == "pytanie":
            self.add_item(discord.ui.TextInput(label="Zadaj pytanie", style=discord.TextStyle.paragraph, placeholder="Twoje pytanie..."))
        elif category == "problem":
            self.add_item(discord.ui.TextInput(label="Opisz swój problem", style=discord.TextStyle.paragraph, placeholder="Opisz problem z klientem..."))
        elif category == "pomoc":
            self.add_item(discord.ui.TextInput(label="W czym potrzebujesz pomocy?", style=discord.TextStyle.paragraph, placeholder="Opisz w czym potrzebujesz pomocy..."))
        elif category == "wspolpraca":
            self.add_item(discord.ui.TextInput(label="Link do kanału YT/TT", style=discord.TextStyle.short, placeholder="https://youtube.com/..."))

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        user = interaction.user
        category_map = {
            "zakup": ("Zakup Clienta", "zakup", discord.Color.green()),
            "pytanie": ("Pytanie", "pytanie", discord.Color.blue()),
            "problem": ("Zgłoś Problem", "problem", discord.Color.red()),
            "pomoc": ("Pomoc", "pomoc", discord.Color.orange()),
            "wspolpraca": ("Współpraca", "wspolpraca", discord.Color.gold()),
        }
        cat_name, prefix, color = category_map[self.category]
        channel_name = f"{prefix}-{user.name}".lower().replace(" ", "-")

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }

        # Add Owner and Administrator roles
        for role in guild.roles:
            if "Owner" in role.name or "administrator" in role.name.lower():
                overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        # Map ticket categories to Discord category IDs
        category_map = {
            "zakup": 1511084479916150878,
            "pytanie": 1511084580411805871,
            "wspolpraca": 1511084828231991488,
            "pomoc": 1511084759873491075,
            "problem": 1511084705309524078
        }

        # Get the category for this ticket type
        category_id = category_map.get(self.category)
        category = guild.get_channel(category_id) if category_id else None

        channel = await guild.create_text_channel(name=channel_name, overwrites=overwrites, category=category)

        # Get user input
        if self.category == "zakup":
            duration = self.children[0].value
            payment_method = self.children[1].value
            embed = discord.Embed(
                title="💎 ZAKUP LICENCJI",
                description=f"**{user.mention}** chce zakupic **ANA CLIENT**\n\n✨ Nasz zespół wkrótce się z Tobą skontaktuje.",
                color=0x00ff88
            )
            embed.add_field(name="⏱️ Czas trwania", value=f"```{duration}```", inline=True)
            embed.add_field(name="💳 Metoda płatności", value=f"```{payment_method}```", inline=True)
            embed.add_field(name="📅 Data zamówienia", value=discord.utils.format_dt(datetime.datetime.now(), style='f'), inline=False)
            embed.add_field(name="👤 ID użytkownika", value=f"`{user.id}`", inline=False)
            embed.set_author(name=user.display_name, icon_url=user.avatar.url if user.avatar else None)
            embed.set_thumbnail(url="https://cdn.discordapp.com/emojis/💎")
            embed.set_footer(text="ANA CLIENT • Premium Ticket System")
        elif self.category == "pytanie":
            user_input = self.children[0].value
            embed = discord.Embed(
                title="❓ PYTANIE",
                description=f"**{user.mention}** ma pytanie dotyczące **ANA CLIENT**\n\n💬 Nasz zespół odpowie na Twoje pytanie wkrótce.",
                color=0x00aaff
            )
            embed.add_field(name="💬 Treść pytania", value=f"```{user_input}```", inline=False)
            embed.add_field(name="📅 Data zgłoszenia", value=discord.utils.format_dt(datetime.datetime.now(), style='f'), inline=False)
            embed.add_field(name="👤 ID użytkownika", value=f"`{user.id}`", inline=False)
            embed.set_author(name=user.display_name, icon_url=user.avatar.url if user.avatar else None)
            embed.set_thumbnail(url="https://cdn.discordapp.com/emojis/💬")
            embed.set_footer(text="ANA CLIENT • Premium Ticket System")
        elif self.category == "problem":
            user_input = self.children[0].value
            embed = discord.Embed(
                title="⚠️ ZGŁOSZENIE PROBLEMU",
                description=f"**{user.mention}** zgłasza problem z **ANA CLIENT**\n\n🔧 Nasz zespół zanalizuje problem i wkrótce się z Tobą skontaktuje.",
                color=0xff4444
            )
            embed.add_field(name="📝 Opis problemu", value=f"```{user_input}```", inline=False)
            embed.add_field(name="📅 Data zgłoszenia", value=discord.utils.format_dt(datetime.datetime.now(), style='f'), inline=False)
            embed.add_field(name="👤 ID użytkownika", value=f"`{user.id}`", inline=False)
            embed.set_author(name=user.display_name, icon_url=user.avatar.url if user.avatar else None)
            embed.set_thumbnail(url="https://cdn.discordapp.com/emojis/⚠️")
            embed.set_footer(text="ANA CLIENT • Premium Ticket System")
        elif self.category == "pomoc":
            user_input = self.children[0].value
            embed = discord.Embed(
                title="🆘 POMOC",
                description=f"**{user.mention}** potrzebuje pomocy z **ANA CLIENT**\n\n🤝 Nasz zespół pomoże Ci wkrótce.",
                color=0xffaa00
            )
            embed.add_field(name="🤝 W czym potrzebujesz pomocy", value=f"```{user_input}```", inline=False)
            embed.add_field(name="📅 Data zgłoszenia", value=discord.utils.format_dt(datetime.datetime.now(), style='f'), inline=False)
            embed.add_field(name="👤 ID użytkownika", value=f"`{user.id}`", inline=False)
            embed.set_author(name=user.display_name, icon_url=user.avatar.url if user.avatar else None)
            embed.set_thumbnail(url="https://cdn.discordapp.com/emojis/🆘")
            embed.set_footer(text="ANA CLIENT • Premium Ticket System")
        elif self.category == "wspolpraca":
            user_input = self.children[0].value
            embed = discord.Embed(
                title="🤝 WSPÓŁPRACA",
                description=f"**{user.mention}** chce podjąć współpracę z **ANA CLIENT**\n\n📊 Nasz zespół przeanalizuje Twoją propozycję i wkrótce się z Tobą skontaktuje.",
                color=0xffd700
            )
            embed.add_field(name="🔗 Link do kanału", value=f"```{user_input}```", inline=False)
            embed.add_field(name="📅 Data zgłoszenia", value=discord.utils.format_dt(datetime.datetime.now(), style='f'), inline=False)
            embed.add_field(name="👤 ID użytkownika", value=f"`{user.id}`", inline=False)
            embed.set_author(name=user.display_name, icon_url=user.avatar.url if user.avatar else None)
            embed.set_thumbnail(url="https://cdn.discordapp.com/emojis/🤝")
            embed.set_footer(text="ANA CLIENT • Premium Ticket System")

        await channel.send(embed=embed, view=CloseTicketView())
        await interaction.response.send_message(f"✅ Ticket utworzony: {channel.mention}", ephemeral=True)

class TicketSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Zakup Clienta", description="Chcesz zakupić klienta", emoji="<:emoji:1515834256562061382>", value="zakup"),
            discord.SelectOption(label="Pytanie", description="Masz pytanie", emoji="<:emoji:1515813909880115351>", value="pytanie"),
            discord.SelectOption(label="Zgłoś Problem", description="Zgłoś problem z klientem", emoji="<:emoji:1515813963034529812>", value="problem"),
            discord.SelectOption(label="Pomoc", description="Potrzebujesz pomocy", emoji="<:emoji:1515814006210691253>", value="pomoc"),
            discord.SelectOption(label="Współpraca", description="Chcesz podjąć z nami współpracę?", emoji="<:emoji:1515814048959299614>", value="wspolpraca"),
        ]
        super().__init__(placeholder="Wybierz kategorię...", min_values=1, max_values=1, options=options, custom_id="ticket_select")

    async def callback(self, interaction: discord.Interaction):
        category = self.values[0]
        modal = TicketModal(category)
        await interaction.response.send_modal(modal)

class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketSelect())

@bot.event
async def on_ready():
    print(f'{bot.user} is ready!')
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name='for suggestions'))

    # Re-register persistent views
    bot.add_view(CloseTicketView())
    bot.add_view(TicketView())
    bot.add_view(VerificationView())
    bot.add_view(GiveawayView())

    # Restore suggestion views from database
    suggestion_channel_id = 1510981985772896366
    suggestion_channel = bot.get_channel(suggestion_channel_id)
    if suggestion_channel:
        async for message in suggestion_channel.history(limit=None):
            if message.embeds and "Nowa sugestia" in message.embeds[0].title:
                state = load_suggestion_state(message.id)
                if state:
                    view = SuggestionView(message.id)
                    view.upvotes, view.downvotes, view.status, view.voters = state
                    bot.add_view(view)
                    print(f"Restored suggestion view for message {message.id}")

    # Restore giveaway views from database
    db_path = os.getenv('DB_PATH', 'suggestions.db')
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''SELECT message_id, prize, end_time, winner_count, requirements, participants, channel_id FROM giveaways''')
    rows = c.fetchall()
    conn.close()

    for row in rows:
        message_id, prize, end_time, winner_count, requirements, participants, channel_id = row
        participants_list = list(map(int, participants.split(','))) if participants else []
        end_datetime = datetime.datetime.fromisoformat(end_time)

        # Only restore if giveaway hasn't ended
        if end_datetime > datetime.datetime.now():
            class RestoredGiveawayView(discord.ui.View):
                def __init__(self, end_time, prize, message_id, participants, winner_count, requirements, channel_id):
                    super().__init__(timeout=None)
                    self.end_time = end_time
                    self.prize = prize
                    self.message_id = message_id
                    self.participants = participants
                    self.winner_count = winner_count
                    self.requirements = requirements
                    self.channel_id = channel_id

                @discord.ui.button(label="🎁 Weź udział", style=discord.ButtonStyle.green)
                async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                    user_id = interaction.user.id
                    if user_id not in self.participants:
                        self.participants.append(user_id)
                        save_giveaway(self.message_id, self.prize, self.end_time, self.winner_count, self.requirements, self.participants, interaction.channel_id)
                        await interaction.response.send_message("Zarejestrowano udział w giveaway!", ephemeral=True)
                    else:
                        await interaction.response.send_message("Już bierzesz udział w tym giveaway!", ephemeral=True)

                @discord.ui.button(label="🏆 Zakończ i wylosuj", style=discord.ButtonStyle.red)
                async def end_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                    # Check if user has giveaway manager role
                    has_role = any(role.id == giveaway_manager_role_id for role in interaction.user.roles)
                    if not has_role:
                        await interaction.response.send_message("❌ Nie masz uprawnień do zakończenia giveaway!", ephemeral=True)
                        return

                    if self.participants:
                        winners = random.sample(self.participants, min(self.winner_count, len(self.participants)))
                        winner_mentions = [f"<@{winner_id}>" for winner_id in winners]
                        await interaction.response.send_message(f"🎉 Giveaway zakończony! Zwycięzcy: {', '.join(winner_mentions)}")
                    else:
                        await interaction.response.send_message("❌ Brak uczestników do wylosowania!")

            view = RestoredGiveawayView(end_datetime, prize, message_id, participants_list, winner_count, requirements, channel_id)
            bot.add_view(view)
            print(f"Restored giveaway view for message {message_id}")

    # Restore polacz panel view
    polacz_message_id = get_polacz_panel_message()
    if polacz_message_id:
        class ConnectLicenseView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=None)

            @discord.ui.button(label="🔗 Połącz Licencję", style=discord.ButtonStyle.green, custom_id="connect_license_button")
            async def connect_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                modal = LicenseModal()
                await interaction.response.send_modal(modal)

        bot.add_view(ConnectLicenseView())
        print(f"Restored polacz panel view for message {polacz_message_id}")

    print("Persistent views registered")

@bot.event
async def on_member_join(member):
    # Assign Niezweryfikowany role to new members
    unverified_role = discord.utils.get(member.guild.roles, name="Niezweryfikowany")
    if unverified_role:
        await member.add_roles(unverified_role)

    # Update welcome channel name with member count
    welcome_channel_id = 1510975485331636405
    welcome_channel = bot.get_channel(welcome_channel_id)
    if welcome_channel:
        member_count = member.guild.member_count
        new_name = f"👋・welcome -{member_count}"
        try:
            await welcome_channel.edit(name=new_name)
        except discord.Forbidden:
            print("No permission to edit channel name")

    channel_id = 1510975485331636405
    channel = bot.get_channel(channel_id)

    if channel:
        member_count = member.guild.member_count
        embed = discord.Embed(
            title="👋 Witaj na serwerze!",
            description=f"{member.mention}\n\nJesteś {member_count}. osobą na tym serwerze!",
            color=discord.Color.green()
        )
        embed.add_field(name="📚 Polecamy", value="Zapoznać się z kanałem <#1511093591366176799>", inline=False)
        embed.add_field(name="🔐 Weryfikacja", value="Aby uzyskać dostęp do serwera, przejdź do kanału weryfikacji i kliknij przycisk 'Zweryfikuj się'.", inline=False)
        embed.set_thumbnail(url=member.avatar.url if member.avatar else None)
        embed.set_footer(text="ANA CLIENT • Welcome")
        await channel.send(embed=embed)

@bot.command()
async def verificationpanel(ctx):
    # Check if user has the required role
    has_required_role = any(role.id == 1511082939700613362 for role in ctx.author.roles)

    if not has_required_role:
        await ctx.send("spierdalaj!", ephemeral=True)
        return

    embed = discord.Embed(
        title="🔐 Weryfikacja Użytkownika",
        description="Kliknij przycisk poniżej, aby się zweryfikować i uzyskać dostęp do serwera.",
        color=discord.Color.gold()
    )
    embed.add_field(name="📋 Proces weryfikacji:", value="1. Kliknij przycisk 'Zweryfikuj się'\n2. Rozwiąż proste zadanie matematyczne\n3. Otrzymaj rolę 'Zweryfikowany'", inline=False)
    embed.add_field(name="⚠️ Uwaga:", value="Weryfikacja jest wymagana aby korzystać z serwera.", inline=False)
    embed.set_thumbnail(url="https://cdn.discordapp.com/emojis/🔐")
    embed.set_footer(text="ANA CLIENT • Verification System")

    await ctx.send(embed=embed, view=VerificationView())

@bot.command()
async def giveawaypanel(ctx):
    # Check if user has the required role
    has_required_role = any(role.id == 1511082939700613362 for role in ctx.author.roles)

    if not has_required_role:
        await ctx.send("spierdalaj!", ephemeral=True)
        return

    embed = discord.Embed(
        title="🎰 Daily Giveaway",
        description="Kliknij przycisk poniżej, aby wziąć udział w codziennym losowaniu nagród!",
        color=discord.Color.from_rgb(147, 51, 234)
    )

    embed.add_field(name="", value="━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", inline=False)
    embed.add_field(name="🎁 Nagrody", value="👑 **ANA CLIENT LIFETIME**\n💎 **ANA CLIENT 30 DNI**\n🌟 **ANA CLIENT 7 DNI**\n✨ **ANA CLIENT 3 DNI**", inline=False)
    embed.add_field(name="", value="━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", inline=False)
    embed.add_field(name="⏰ Cooldown", value="Możesz losować co 24 godziny", inline=True)
    embed.add_field(name="📋 Jak to działa", value="Kliknij przycisk i wygraj!", inline=True)
    embed.add_field(name="", value="━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", inline=False)
    embed.set_thumbnail(url="https://cdn.discordapp.com/emojis/🎰")
    embed.set_footer(text="ANA CLIENT • Daily Giveaway System")
    embed.timestamp = datetime.datetime.now()

    await ctx.send(embed=embed, view=GiveawayView())

@bot.command()
async def setwin(ctx, user: discord.Member, *, prize: str):
    # Check if user has the required role
    has_required_role = any(role.id == 1511082939700613362 for role in ctx.author.roles)

    if not has_required_role:
        await ctx.send("spierdalaj!", ephemeral=True)
        return

    # Determine prize emoji based on prize content
    if "LIFETIME" in prize.upper():
        prize_emoji = "👑"
    elif "30 DNI" in prize.upper():
        prize_emoji = "💎"
    elif "7 DNI" in prize.upper():
        prize_emoji = "🌟"
    elif "3 DNI" in prize.upper():
        prize_emoji = "✨"
    else:
        prize_emoji = "🎉"

    # Create result embed (identical to real win)
    result_embed = discord.Embed(
        title=f"{prize_emoji} GRATULACJE!",
        description=f"{user.mention} wygrał: **{prize}**",
        color=discord.Color.from_rgb(255, 215, 0)
    )
    result_embed.add_field(name="", value="━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", inline=False)
    result_embed.add_field(name="🎉 Wygrana", value=prize, inline=False)
    result_embed.add_field(name="", value="━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", inline=False)
    result_embed.set_thumbnail(url="https://cdn.discordapp.com/emojis/🎉")
    result_embed.set_footer(text="ANA CLIENT • Daily Giveaway")
    result_embed.timestamp = datetime.datetime.now()

    # Send the win message
    await ctx.send(embed=result_embed)

    # Log the manual win
    giveaway_log_channel_id = 1511395102373970113  # Owner log channel
    log_channel = bot.get_channel(giveaway_log_channel_id)
    if log_channel:
        log_embed = discord.Embed(
            title="🎰 Manual Win Set",
            description=f"**User ID:** {user.id}",
            color=discord.Color.from_rgb(255, 215, 0)
        )
        log_embed.add_field(name="👤 User", value=f"{user.mention} (`{user.name}`)", inline=False)
        log_embed.add_field(name="🎁 Prize", value=prize, inline=False)
        log_embed.add_field(name="👑 Set by", value=f"{ctx.author.mention} (Owner)", inline=False)
        log_embed.add_field(name="⏰ Timestamp", value=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), inline=True)
        log_embed.set_footer(text="ANA CLIENT • Owner Logs")
        log_embed.timestamp = datetime.datetime.now()
        await log_channel.send(embed=log_embed)

    await ctx.send(f"✅ Ustawiono wygraną dla {user.mention}!", ephemeral=True)

@bot.command()
async def ticketpanel(ctx):
    # Check if user has the required role
    has_required_role = any(role.id == 1511082939700613362 for role in ctx.author.roles)

    if not has_required_role:
        await ctx.send("spierdalaj!", ephemeral=True)
        return

    embed = discord.Embed(
        title="🎫 ANA CLIENT × TICKET",
        description="Aby utworzyć ticket, wybierz jedną z opcji poniżej",
        color=0x000000
    )
    embed.set_image(url="https://cdn.discordapp.com/attachments/1492544749603786813/1515786374664556574/cOHuSwH.png?ex=6a304591&is=6a2ef411&hm=16007f7fd078b2bc6c4a71dfe026e7f1b8fa648c0635bbf37cf86ed8637ce496&")
    embed.set_footer(text="ANA CLIENT • Ticket System")
    await ctx.send(embed=embed, view=TicketView())

@bot.command()
async def cennik(ctx):
    # Check if user has the required role
    has_required_role = any(role.id == 1511082939700613362 for role in ctx.author.roles)

    if not has_required_role:
        await ctx.send("spierdalaj!", ephemeral=True)
        return

    embed = discord.Embed(
        title="ANA CLIENT × CENNIK",
        description="",
        color=0x000000
    )
    embed.add_field(name="<:emoji:1515822153939943585> 1 MIESIĄC", value="**20 zł**", inline=False)
    embed.add_field(name="<:emoji:1515822131554942987> 3 MIESIĄCE", value="**30 zł**", inline=False)
    embed.add_field(name="<:emoji:1515822103469752500> LIFETIME", value="**50 zł**", inline=False)
    embed.set_footer(text="ANA CLIENT • Cennik")
    await ctx.send(embed=embed)

@bot.command()
async def creategiveaway(ctx):
    # Check if user has the required role
    has_required_role = any(role.id == 1511082939700613362 for role in ctx.author.roles)

    if not has_required_role:
        await ctx.send("spierdalaj!", ephemeral=True)
        return

    class CreateGiveawayView(discord.ui.View):
        @discord.ui.button(label="🎁 Stwórz Giveaway", style=discord.ButtonStyle.green)
        async def create_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            modal = GiveawayModal()
            await interaction.response.send_modal(modal)

    embed = discord.Embed(
        title="🎁 Stwórz Giveaway",
        description="Kliknij przycisk poniżej aby stworzyć nowy giveaway",
        color=0x000000
    )
    embed.set_footer(text="ANA CLIENT • Giveaway Creator")

    await ctx.send(embed=embed, view=CreateGiveawayView())

@bot.command()
async def info(ctx):
    """Wyświetla listę wszystkich komend bota"""
    # Check if user has the required role
    has_required_role = any(role.id == 1511082939700613362 for role in ctx.author.roles)

    if not has_required_role:
        await ctx.send("spierdalaj!", ephemeral=True)
        return

    embed = discord.Embed(
        title="📋 Lista Komend ANA CLIENT",
        description="Wszystkie dostępne komendy bota",
        color=0x000000
    )

    embed.add_field(name="🔐 verificationpanel", value="Tworzy panel weryfikacji użytkowników", inline=False)
    embed.add_field(name="🎁 giveawaypanel", value="Tworzy panel giveaway", inline=False)
    embed.add_field(name="🎯 setwin [użytkownik] [nagroda]", value="Ustawia wygraną dla użytkownika", inline=False)
    embed.add_field(name="🎫 ticketpanel", value="Tworzy panel ticketów", inline=False)
    embed.add_field(name="💰 cennik", value="Wyświetla cennik", inline=False)
    embed.add_field(name="🎁 creategiveaway", value="Tworzy nowy giveaway", inline=False)
    embed.add_field(name="🔗 polacz", value="Tworzy panel łączenia licencji", inline=False)
    embed.add_field(name="🔄 restoregiveaway [message_id]", value="Przywraca funkcjonalność giveaway dla starej wiadomości", inline=False)
    embed.add_field(name="💳 metody", value="Wyświetla metody płatności", inline=False)
    embed.add_field(name="✅ legit [sprzedawca] [klient] [argumenty]", value="Wykonuje legit check transakcji", inline=False)
    embed.add_field(name="⏰ pv [użytkownik] [czas] [licencja]", value="wysyła licencje do użytkownika", inline=False)
    embed.add_field(name="📢 spam [użytkownik] [liczba] [wiadomość]", value="Spamuje użytkownika wiadomością przez DM (1-1000)", inline=False)

    embed.set_footer(text="ANA CLIENT • System Komend")
    await ctx.send(embed=embed)

@bot.command()
async def polacz(ctx):
    """Połącz konto Discord z kluczem licencyjnym"""
    # Check if user has the required role
    has_required_role = any(role.id == 1511082939700613362 for role in ctx.author.roles)

    if not has_required_role:
        await ctx.send("spierdalaj!", ephemeral=True)
        return

    class ConnectLicenseView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)

        @discord.ui.button(label="🔗 Połącz Licencję", style=discord.ButtonStyle.green, custom_id="connect_license_button")
        async def connect_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            modal = LicenseModal()
            await interaction.response.send_modal(modal)

    embed = discord.Embed(
        title="🔗 Połącz Licencję",
        description="Kliknij przycisk poniżej aby połączyć swoje konto Discord z kluczem licencyjnym",
        color=0x000000
    )
    embed.set_footer(text="ANA CLIENT • System Licencyjny")

    view = ConnectLicenseView()
    sent_message = await ctx.send(embed=embed, view=view)

    # Save message ID for persistence
    save_polacz_panel_message(sent_message.id)

@bot.command()
async def restoregiveaway(ctx, message_id: int):
    # Check if user has the required role
    has_required_role = any(role.id == 1511082939700613362 for role in ctx.author.roles)

    if not has_required_role:
        await ctx.send("spierdalaj!", ephemeral=True)
        return

    # Get giveaway from database
    giveaway_data = get_giveaway(message_id)
    if not giveaway_data:
        await ctx.send("❌ Nie znaleziono giveaway w bazie danych!", ephemeral=True)
        return

    prize, end_time, winner_count, requirements, participants, channel_id = giveaway_data

    # Check if giveaway has ended
    if end_time < datetime.datetime.now():
        await ctx.send("❌ Ten giveaway już się zakończył!", ephemeral=True)
        return

    # Create new view for the message
    class ManualRestoreView(discord.ui.View):
        def __init__(self, end_time, prize, message_id, participants, winner_count, requirements, channel_id):
            super().__init__(timeout=None)
            self.end_time = end_time
            self.prize = prize
            self.message_id = message_id
            self.participants = participants
            self.winner_count = winner_count
            self.requirements = requirements
            self.channel_id = channel_id

        @discord.ui.button(label="🎁 Weź udział", style=discord.ButtonStyle.green)
        async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            user_id = interaction.user.id
            if user_id not in self.participants:
                self.participants.append(user_id)
                save_giveaway(self.message_id, self.prize, self.end_time, self.winner_count, self.requirements, self.participants, interaction.channel_id)
                await interaction.response.send_message("Zarejestrowano udział w giveaway!", ephemeral=True)
            else:
                await interaction.response.send_message("Już bierzesz udział w tym giveaway!", ephemeral=True)

        @discord.ui.button(label="🏆 Zakończ i wylosuj", style=discord.ButtonStyle.red)
        async def end_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            # Check if user has giveaway manager role
            has_role = any(role.id == giveaway_manager_role_id for role in interaction.user.roles)
            if not has_role:
                await interaction.response.send_message("❌ Nie masz uprawnień do zakończenia giveaway!", ephemeral=True)
                return

            if self.participants:
                winners = random.sample(self.participants, min(self.winner_count, len(self.participants)))
                winner_mentions = [f"<@{winner_id}>" for winner_id in winners]
                await interaction.response.send_message(f"🎉 Giveaway zakończony! Zwycięzcy: {', '.join(winner_mentions)}")
            else:
                await interaction.response.send_message("❌ Brak uczestników do wylosowania!")

    view = ManualRestoreView(end_time, prize, message_id, participants, winner_count, requirements, channel_id)
    bot.add_view(view)

    # Try to update the message with the new view
    try:
        message = await ctx.channel.fetch_message(message_id)
        await message.edit(view=view)
        await ctx.send(f"✅ Przywrócono giveaway dla wiadomości {message_id}!", ephemeral=True)
    except discord.NotFound:
        await ctx.send("❌ Nie znaleziono wiadomości na tym kanale!", ephemeral=True)
    except Exception as e:
        await ctx.send(f"❌ Błąd: {str(e)}", ephemeral=True)

@bot.command()
async def metody(ctx):
    # Check if user has the required role
    has_required_role = any(role.id == 1511082939700613362 for role in ctx.author.roles)

    if not has_required_role:
        await ctx.send("spierdalaj!", ephemeral=True)
        return

    embed = discord.Embed(
        title="ANA CLIENT × METODY PŁATNOŚCI",
        description="",
        color=0x000000
    )
    embed.add_field(name="<:emoji:1515831957643071518> PayPal", value="", inline=False)
    embed.add_field(name="<:emoji:1515831784296812735> Paysafecard", value="", inline=False)
    embed.add_field(name="<:emoji:1515832001645641749> BLIK", value="", inline=False)
    embed.add_field(name="<:emoji:1515831981127241849> Krypto", value="", inline=False)
    embed.set_footer(text="ANA CLIENT • Metody Płatności")
    await ctx.send(embed=embed)

@bot.command()
async def legit(ctx, sprzedawca: discord.Member = None, klient: discord.Member = None, *, args: str = None):
    print(f"!legit command called by {ctx.author.name}")
    print(f"Sprzedawca: {sprzedawca}")
    print(f"Klient: {klient}")
    print(f"Args: {args}")

    # Check if user has the required role
    has_required_role = any(role.id == 1511082939700613362 for role in ctx.author.roles)

    if not has_required_role:
        await ctx.send("spierdalaj!", ephemeral=True)
        return

    # Parse arguments
    if not args:
        await ctx.send("Użycie: !legit @sprzedawca @klient <produkt> <kwota> [data]", ephemeral=True)
        return

    parts = args.split()
    if len(parts) < 2:
        await ctx.send("Użycie: !legit @sprzedawca @klient <produkt> <kwota> [data]", ephemeral=True)
        return

    # Try to extract product and amount from remaining args
    # Format: produkt kwota [data]
    produkt = parts[0]
    kwota = parts[1] if len(parts) > 1 else None
    data = parts[2] if len(parts) > 2 else None

    if not sprzedawca or not klient or not produkt or not kwota:
        await ctx.send("Użycie: !legit @sprzedawca @klient <produkt> <kwota> [data]", ephemeral=True)
        return

    # Format date if not provided
    if not data:
        data = discord.utils.format_dt(datetime.datetime.now(), style='f')

    # Create styled embed with small caps
    embed = discord.Embed(
        title="✅ ᴘᴏᴛᴡɪᴇʀᴅᴢᴏɴᴏ ʟᴇɢɪᴛɴᴏꜱᴄ",
        description=f"Nasz klient {klient.mention} potwierdza naszą legitność.",
        color=discord.Color.green()
    )

    embed.add_field(name="szczegóły transakcji", value="", inline=False)
    embed.add_field(name="» Produkt", value=produkt, inline=False)
    embed.add_field(name="» Kwota", value=kwota, inline=False)
    embed.add_field(name="» Sprzedawca", value=sprzedawca.mention, inline=False)
    embed.add_field(name="» Klient", value=klient.mention, inline=False)
    embed.add_field(name="» Data", value=data, inline=False)

    embed.set_footer(text="ANA CLIENT • Legit Check")
    await ctx.send(embed=embed)

@bot.command()
async def spam(ctx, user: discord.Member, count: int, *, message: str):
    # Check if user has the required role
    has_required_role = any(role.id == 1511082939700613362 for role in ctx.author.roles)

    if not has_required_role:
        await ctx.send("spierdalaj!", ephemeral=True)
        return

    if count < 1 or count > 1000:
        await ctx.send("❌ Liczba wiadomości musi być między 1 a 1000!", ephemeral=True)
        return

    try:
        # Send spam messages via DM
        for i in range(count):
            await user.send(message)
            await asyncio.sleep(0.5)  # Small delay to avoid rate limiting

        await ctx.send(f"✅ Wysłano {count} wiadomości do {user.mention}!", ephemeral=True)
    except discord.Forbidden:
        await ctx.send(f"❌ Nie można wysłać wiadomości do {user.mention} (ma wyłączone DM)!", ephemeral=True)
    except Exception as e:
        await ctx.send(f"❌ Błąd: {str(e)}", ephemeral=True)

@bot.command()
async def pv(ctx, user: discord.Member, czas: str, licencja: str):
    # Check if user has the required role
    has_required_role = any(role.id == 1511082939700613362 for role in ctx.author.roles)

    if not has_required_role:
        await ctx.send("spierdalaj!", ephemeral=True)
        return

    try:
        # Create DM embed
        data_wyslania = discord.utils.format_dt(datetime.datetime.now(), style='D')
        embed = discord.Embed(
            title="💎 Dziękujemy za zakup ANA CLIENT",
            description=f"{user.mention}\n\nPoniżej znajdziesz szczegóły Twojej licencji.",
            color=discord.Color.green()
        )
        embed.add_field(name="⏰ Licencja wygasa za", value=czas, inline=False)
        embed.add_field(name="🔑 Twój klucz licencyjny", value=f"```{licencja}```", inline=False)
        embed.add_field(name="📅 Data wysłania", value=data_wyslania, inline=False)
        embed.add_field(name="📚 Instrukcja", value="1. Skopiuj klucz licencyjny\n2. Wklej go w modzie ANA CLIENT\n3. Ciesz się pełnym dostępem!", inline=False)
        embed.set_thumbnail(url="https://cdn.discordapp.com/emojis/💎")
        embed.set_footer(text="ANA CLIENT • Purchase Confirmation")

        # Send DM to user
        await user.send(embed=embed)

        # Confirm to the command sender
        await ctx.send(f"✅ Wysłano potwierdzenie zakupu do {user.mention}", ephemeral=True)
    except discord.Forbidden:
        await ctx.send(f"❌ Nie można wysłać DM do {user.mention} (użytkownik ma wyłączone DM)", ephemeral=True)
    except Exception as e:
        await ctx.send(f"❌ Błąd: {str(e)}", ephemeral=True)

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # Process commands first
    await bot.process_commands(message)

    # Check if it's a command (already processed)
    if message.content.startswith('!'):
        return

    channel_id_str = os.getenv('SUGGESTION_CHANNEL_ID')
    # Extract channel ID from URL if needed
    if '/' in channel_id_str:
        channel_id_str = channel_id_str.split('/')[-1]

    suggestion_channel_id = int(channel_id_str)

    print(f"Received message in channel {message.channel.id}, target: {suggestion_channel_id}")

    # Anti-link system for all channels (except tickets and Owners)
    # Check if user has Owner role (exempt from anti-link)
    has_owner_role = any("Owner" in role.name for role in message.author.roles)
    
    # Check if channel is a ticket channel (zakup, pytanie, problem, pomoc, wspolpraca)
    is_ticket_channel = any(prefix in message.channel.name.lower() for prefix in ["zakup", "pytanie", "problem", "pomoc", "wspolpraca"])
    
    if not has_owner_role and not is_ticket_channel:
        import re
        # Block ALL links including Discord links
        link_pattern = r'https?://\S+|www\.\S+'
        if re.search(link_pattern, message.content, re.IGNORECASE):
            await message.delete()
            try:
                await message.author.timeout(datetime.timedelta(minutes=10), reason="Wysłanie linku")
                await message.channel.send(f"⚠️ {message.author.mention} został wyciszony na 10 minut za wysłanie linku.")
                print(f"Successfully timed out user {message.author.name} for 10 minutes for sending link")
            except discord.Forbidden:
                await message.channel.send(f"⚠️ Bot nie ma uprawnień do wyciszania użytkowników!")
                print(f"Error: Bot lacks permission to timeout user {message.author.name}")
            except Exception as e:
                await message.channel.send(f"⚠️ Błąd podczas wyciszania: {str(e)}")
                print(f"Error timing out user {message.author.name}: {e}")

    if message.channel.id == suggestion_channel_id:
        print(f"Creating suggestion embed for message: {message.content[:50]}")
        await create_suggestion_embed(message)

    # Bug report embed for channel 1523296189867622461
    if message.channel.id == 1523296189867622461:
        print(f"Creating bug report embed for message: {message.content[:50]}")
        embed = discord.Embed(
            title="<:emoji:1515813963034529812> Błąd",
            description=f"> {message.content}",
            color=0xff0000
        )

        embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
        embed.add_field(name="", value="━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", inline=False)
        embed.add_field(name="Autor", value=message.author.mention, inline=True)
        embed.add_field(name="Data", value=discord.utils.format_dt(message.created_at, style='f'), inline=True)
        embed.set_footer(text="ANA CLIENT • System Zgłoszeń Błędów")
        embed.timestamp = message.created_at

        await message.delete()
        await message.channel.send(embed=embed)

    # Legit check counter for channel 1510975968456474786
    legit_check_channel_id = 1510975968456474786
    if message.channel.id == legit_check_channel_id:
        count = increment_legit_check_count()
        
        # Delete previous bot message
        last_message_id = get_legit_check_last_message()
        if last_message_id:
            try:
                last_message = await message.channel.fetch_message(last_message_id)
                await last_message.delete()
            except discord.NotFound:
                pass
            except Exception as e:
                print(f"Error deleting last message: {e}")
        
        # Polish pluralization for "legit check"
        if count == 1:
            legit_check_text = "legit checka"
        elif 2 <= count <= 4:
            legit_check_text = "legit checki"
        else:
            legit_check_text = "legit checków"
        
        embed = discord.Embed(
            title="✅ Legit Check",
            description=f"Posiadamy już **{count}** {legit_check_text}!",
            color=discord.Color.green()
        )
        embed.set_footer(text="ANA CLIENT • Legit Check Counter")
        embed.timestamp = datetime.datetime.now()
        sent_message = await message.channel.send(embed=embed)
        
        # Store the new message ID
        update_legit_check_last_message(sent_message.id)

async def create_suggestion_embed(message):
    view = SuggestionView()

    embed = discord.Embed(
        title="<:emoji:1515825926393299064> Nowa Sugestia",
        description=f"> {message.content}",
        color=0x000000
    )

    embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
    embed.add_field(name="", value="━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", inline=False)
    embed.add_field(name="Status", value="Oczekuje na decyzję", inline=False)
    embed.add_field(name="Głosy", value=f"0 | 0", inline=False)
    embed.add_field(name="", value="━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", inline=False)
    embed.add_field(name="Autor", value=message.author.mention, inline=True)
    embed.add_field(name="Data", value=discord.utils.format_dt(message.created_at, style='f'), inline=True)
    embed.set_footer(text="ANA CLIENT • System Sugestii")
    embed.timestamp = message.created_at

    sent_message = await message.channel.send(embed=embed, view=view)

    # Create thread for the suggestion
    thread = await sent_message.create_thread(
        name=f"📝 {message.author.display_name}'s suggestion",
        auto_archive_duration=1440  # 24 hours
    )

    # Update view with message_id and save initial state
    view.message_id = sent_message.id
    save_suggestion_state(sent_message.id, 0, 0, "Oczekuje na decyzję", set())

    await message.delete()

async def oauth2_callback(request):
    """OAuth2 callback handler"""
    print("OAuth2 callback triggered")
    code = request.query.get('code')
    state = request.query.get('state')
    print(f"Code: {code}, State: {state}")

    if not code:
        return web.Response(text="Authorization code missing", status=400)

    # Exchange code for token
    access_token, refresh_token, expires_at = await exchange_code_for_token(code)

    if not access_token:
        return web.Response(text="Failed to exchange code for token", status=500)

    # Get user info
    user_info = await get_user_info(access_token)

    if not user_info:
        return web.Response(text="Failed to get user info", status=500)

    # Save token
    user_id = user_info['id']
    save_oauth2_token(user_id, access_token, refresh_token, expires_at)

    # Verify user
    verify_user(user_id)

    # Assign roles using REST API for better reliability
    try:
        headers = {
            'Authorization': f'Bot {os.getenv("DISCORD_TOKEN")}',
            'Content-Type': 'application/json'
        }

        # Add verified role
        url = f'https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}/roles/{verified_role_id}'
        async with aiohttp.ClientSession() as session:
            async with session.put(url, headers=headers) as response:
                print(f"Add role response: {response.status}")

        # Remove unverified role
        url = f'https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}/roles/{unverified_role_id}'
        async with aiohttp.ClientSession() as session:
            async with session.delete(url, headers=headers) as response:
                print(f"Remove role response: {response.status}")
    except Exception as e:
        print(f"Role assignment error: {e}")

    # Redirect back to Discord or show success page
    html_response = """
    <html>
    <head>
        <title>Verification Successful</title>
        <style>
            body {
                font-family: Arial, sans-serif;
                background-color: #1a1a1a;
                color: #ffffff;
                display: flex;
                justify-content: center;
                align-items: center;
                height: 100vh;
                margin: 0;
            }
            .container {
                text-align: center;
                padding: 40px;
                background-color: #2d2d2d;
                border-radius: 10px;
                box-shadow: 0 4px 6px rgba(0, 0, 0, 0.3);
            }
            h1 {
                color: #00ff88;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>✅ Weryfikacja Udana!</h1>
            <p>Zostałeś pomyślnie zweryfikowany.</p>
            <p>Możesz zamknąć tę stronę i wrócić do Discord.</p>
        </div>
    </body>
    </html>
    """
    return web.Response(text=html_response, content_type='text/html')

async def run_http_server():
    app = web.Application()

    async def log_request(request):
        print(f"Received request: {request.method} {request.path}")
        return await request

    app.router.add_get('/', lambda r: web.Response(text='Bot is running!'))
    app.router.add_get('/health', lambda r: web.Response(text='OK'))
    app.router.add_get('/callback', oauth2_callback)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 7860)
    await site.start()
    print("HTTP server started on port 7860")
    print(f"Callback URL should be: https://ana-client-production-86f3.up.railway.app/callback")

async def run_bot():
    token = os.getenv('DISCORD_TOKEN')
    if not token:
        print("Error: DISCORD_TOKEN not found")
        return
    await bot.start(token)

async def main():
    await asyncio.gather(run_http_server(), run_bot(), giveaway_checker())

if __name__ == "__main__":
    asyncio.run(main())
