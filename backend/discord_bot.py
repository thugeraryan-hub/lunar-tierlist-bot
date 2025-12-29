import os
import discord
import aiohttp
from discord.ext import commands
from discord import ui, app_commands
from datetime import datetime, timezone
from typing import Optional
import time

# Load token from environment variable
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")

# Bot setup
intents = discord.Intents.default()
intents.members = True  # Required for role assignment
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# In-memory profile storage (user_id -> profile data)
user_profiles: dict[int, dict] = {}

# Gamemode options
GAMEMODES = [
    "Netherite",
    "Potion",
    "Sword",
    "Crystal",
    "UHC",
    "SMP",
    "DiaSMP",
    "Axe",
    "Mace",
]

REGIONS = ["NA", "EU", "AS"]

# In-memory queue storage: {(gamemode, region): [user_id, ...]}
queues: dict[tuple[str, str], list[int]] = {}

# Active testers: {(gamemode, region): [user_id, ...]}
active_testers: dict[tuple[str, str], list[int]] = {}

# Pulled users (locked): {(gamemode, region): user_id or None}
pulled_users: dict[tuple[str, str], Optional[int]] = {}

# Queue panel message IDs: {(gamemode, region): message_id}
queue_panel_messages: dict[tuple[str, str], int] = {}

# Staff roles that can see ticket channels
STAFF_ROLES = ["Senior Tester", "Head Tester", "Admin", "Administrator", "Moderator", "Manager"]

# Staff roles that can use /result command
RESULT_ALLOWED_ROLES = ["Senior Tester", "Head Tester", "Admin", "Administrator", "Manager"]

# Tier options
TIERS = [
    "Unranked",
    "LT5", "HT5",
    "LT4", "HT4",
    "LT3", "HT3",
    "LT2", "HT2",
    "LT1", "HT1",
]

# Results channel name (configurable)
RESULTS_CHANNEL_NAME = "tier-results"

# In-memory result log: [(timestamp, tester_id, player_id, ign, gamemode, region, old_tier, new_tier), ...]
result_log: list[tuple] = []

# Cooldown tracking: {tester_id: last_result_timestamp}
result_cooldowns: dict[int, float] = {}
RESULT_COOLDOWN_SECONDS = 30  # 30 second cooldown between results

# Default Steve skin URL
STEVE_SKIN_URL = "https://mc-heads.net/avatar/MHF_Steve/128"


async def fetch_minecraft_skin(ign: str) -> str:
    """
    Fetch Minecraft player skin avatar URL.
    Returns Steve skin if player not found or error occurs.
    Uses mc-heads.net for reliable avatar fetching.
    """
    try:
        # mc-heads.net automatically handles invalid usernames with Steve
        # But we verify with Mojang API first for premium check
        async with aiohttp.ClientSession() as session:
            # Check if username exists on Mojang
            mojang_url = f"https://api.mojang.com/users/profiles/minecraft/{ign}"
            async with session.get(mojang_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    # Valid premium account - use their skin
                    return f"https://mc-heads.net/avatar/{ign}/128"
                else:
                    # Cracked or invalid - use Steve
                    return STEVE_SKIN_URL
    except Exception:
        # Any error - fallback to Steve
        return STEVE_SKIN_URL


def can_submit_result(member: discord.Member, gamemode: str) -> tuple[bool, str]:
    """
    Check if member can submit a result for the given gamemode.
    Returns (allowed, error_message).
    """
    gamemode_display = get_gamemode_display(gamemode)
    tester_role_name = f"{gamemode_display} Tester"

    # Check if has specific tester role
    has_tester = any(role.name == tester_role_name for role in member.roles)

    # Check if has staff role (can submit any gamemode)
    has_staff = any(role.name in RESULT_ALLOWED_ROLES for role in member.roles)

    if has_tester or has_staff:
        return True, ""
    else:
        return False, f"You need the `{tester_role_name}` role or a staff role to submit results."


def check_cooldown(tester_id: int) -> tuple[bool, int]:
    """
    Check if tester is on cooldown.
    Returns (on_cooldown, seconds_remaining).
    """
    last_time = result_cooldowns.get(tester_id, 0)
    elapsed = time.time() - last_time
    if elapsed < RESULT_COOLDOWN_SECONDS:
        return True, int(RESULT_COOLDOWN_SECONDS - elapsed)
    return False, 0


async def update_tier_roles(
    guild: discord.Guild,
    member: discord.Member,
    gamemode: str,
    old_tier: str,
    new_tier: str,
) -> tuple[bool, str]:
    """
    Update tier roles for a member.
    Removes old tier role (if exists) and assigns new tier role.
    Returns (success, message).
    """
    gamemode_display = get_gamemode_display(gamemode)

    # Role naming format: "HT5 Sword", "LT3 Crystal", etc.
    old_role_name = f"{old_tier} {gamemode_display}" if old_tier != "Unranked" else None
    new_role_name = f"{new_tier} {gamemode_display}" if new_tier != "Unranked" else None

    try:
        # Remove old tier role if it exists
        if old_role_name:
            old_role = discord.utils.get(guild.roles, name=old_role_name)
            if old_role and old_role in member.roles:
                await member.remove_roles(old_role, reason="Tier update - old tier removed")

        # Add new tier role
        if new_role_name:
            new_role = discord.utils.get(guild.roles, name=new_role_name)
            if new_role is None:
                # Create the role if it doesn't exist
                new_role = await guild.create_role(name=new_role_name, reason="Tier role auto-created")
            await member.add_roles(new_role, reason=f"Tier update - promoted to {new_tier}")

        return True, "Roles updated successfully"
    except discord.Forbidden:
        return False, "Bot lacks permission to manage roles"
    except Exception as e:
        return False, f"Error updating roles: {str(e)}"


def get_queue_key(gamemode: str, region: str) -> tuple[str, str]:
    """Normalize queue key."""
    return (gamemode.lower(), region.upper())


def get_queue(gamemode: str, region: str) -> list[int]:
    """Get or create a queue for gamemode + region."""
    key = get_queue_key(gamemode, region)
    if key not in queues:
        queues[key] = []
    return queues[key]


def get_active_testers(gamemode: str, region: str) -> list[int]:
    """Get active testers for gamemode + region."""
    key = get_queue_key(gamemode, region)
    if key not in active_testers:
        active_testers[key] = []
    return active_testers[key]


def get_pulled_user(gamemode: str, region: str) -> Optional[int]:
    """Get currently pulled user for gamemode + region."""
    key = get_queue_key(gamemode, region)
    return pulled_users.get(key)


def set_pulled_user(gamemode: str, region: str, user_id: Optional[int]):
    """Set pulled user for gamemode + region."""
    key = get_queue_key(gamemode, region)
    pulled_users[key] = user_id


async def grant_channel_access(
    guild: discord.Guild, member: discord.Member, channel_name: str
) -> bool:
    """Grant VIEW access to a waitlist channel for a member."""
    channel = discord.utils.get(guild.text_channels, name=channel_name)
    if channel is None:
        return False
    try:
        await channel.set_permissions(
            member,
            view_channel=True,
            reason=f"Granted waitlist access to #{channel_name}",
        )
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


def has_tester_role(member: discord.Member, gamemode: str) -> bool:
    """Check if member has the tester role for a gamemode."""
    tester_role_name = f"{gamemode} Tester"
    return any(role.name == tester_role_name for role in member.roles)


def has_waitlist_role(member: discord.Member, gamemode: str) -> bool:
    """Check if member has the waitlist role for a gamemode."""
    # Find the display name for the gamemode
    gamemode_display = next((gm for gm in GAMEMODES if gm.lower() == gamemode.lower()), gamemode)
    role_name = f"Waitlist {gamemode_display}"
    return any(role.name == role_name for role in member.roles)


def get_gamemode_display(gamemode: str) -> str:
    """Get proper display name for gamemode."""
    return next((gm for gm in GAMEMODES if gm.lower() == gamemode.lower()), gamemode)


def create_closed_queue_embed(gamemode: str, region: str) -> discord.Embed:
    """Create a CLOSED queue panel embed."""
    gamemode_display = get_gamemode_display(gamemode)

    embed = discord.Embed(
        title=f"🔒 {gamemode_display} Queue Closed",
        description="This testing session has ended.\nYou will be notified here when a new queue opens.",
        color=discord.Color.dark_gray(),
    )
    embed.add_field(
        name="📋 Reason",
        value="Manually closed by queue administrator",
        inline=False,
    )
    embed.add_field(
        name="⏰ Session Ended",
        value=f"<t:{int(datetime.now(timezone.utc).timestamp())}:F>",
        inline=False,
    )
    embed.set_footer(text=f"Thank you for testing! | 🌍 Region: {region}")

    return embed


def create_open_queue_embed(guild: discord.Guild, gamemode: str, region: str) -> discord.Embed:
    """Create an OPEN queue panel embed."""
    gamemode_display = get_gamemode_display(gamemode)
    queue = get_queue(gamemode, region)
    testers = get_active_testers(gamemode, region)
    pulled = get_pulled_user(gamemode, region)

    embed = discord.Embed(
        title=f"✅ {gamemode_display} Tester Available!",
        description="The queue is now open and updates in real-time.",
        color=discord.Color.green(),
    )

    # Queue List
    if queue:
        queue_lines = []
        for i, uid in enumerate(queue, 1):
            member = guild.get_member(uid)
            mention = member.mention if member else f"<@{uid}>"
            pulled_marker = " 🔒" if pulled == uid else ""
            queue_lines.append(f"`{i}.` {mention}{pulled_marker}")
        embed.add_field(
            name=f"📋 Queue ({len(queue)})",
            value="\n".join(queue_lines[:15]) + ("\n..." if len(queue) > 15 else ""),
            inline=False,
        )
    else:
        embed.add_field(
            name="📋 Queue",
            value="*Queue is empty*",
            inline=False,
        )

    # Active Testers
    if testers:
        tester_lines = []
        for i, tid in enumerate(testers, 1):
            member = guild.get_member(tid)
            tester_lines.append(f"{i}. {member.mention if member else f'<@{tid}>'}")
        embed.add_field(
            name="🎮 Active Testers",
            value="\n".join(tester_lines),
            inline=False,
        )

    embed.set_footer(text=f"🌍 Region: {region} | ⏱ Last Updated: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")

    return embed


async def create_queue_embed(guild: discord.Guild, gamemode: str, region: str) -> discord.Embed:
    """Create the queue panel embed based on state."""
    testers = get_active_testers(gamemode, region)
    if testers:
        return create_open_queue_embed(guild, gamemode, region)
    else:
        return create_closed_queue_embed(gamemode, region)


class QueueView(ui.View):
    """Persistent view for queue panel with Join/Leave buttons."""

    def __init__(self, gamemode: str, region: str, disabled: bool = False):
        super().__init__(timeout=None)
        self.gamemode = gamemode
        self.region = region
        # Update button states
        for child in self.children:
            if isinstance(child, ui.Button):
                child.disabled = disabled

    @ui.button(
        label="Join Queue",
        style=discord.ButtonStyle.success,
        custom_id="queue_join",
        emoji="✅",
    )
    async def join_button(self, interaction: discord.Interaction, button: ui.Button):
        # Check if testers are online
        testers = get_active_testers(self.gamemode, self.region)
        if not testers:
            await interaction.response.send_message(
                "❌ Queue is closed. No tester is currently online.",
                ephemeral=True,
            )
            return

        # Check if user has waitlist role
        if not has_waitlist_role(interaction.user, self.gamemode):
            await interaction.response.send_message(
                "❌ You need the Waitlist role for this gamemode to join.",
                ephemeral=True,
            )
            return

        queue = get_queue(self.gamemode, self.region)

        # Prevent duplicate joins
        if interaction.user.id in queue:
            await interaction.response.send_message(
                "❌ You are already in the queue.",
                ephemeral=True,
            )
            return

        # Add to queue
        queue.append(interaction.user.id)
        position = len(queue)

        # Update panel
        embed = await create_queue_embed(interaction.guild, self.gamemode, self.region)
        await interaction.message.edit(embed=embed)

        await interaction.response.send_message(
            f"✅ You joined the queue at position **#{position}**.",
            ephemeral=True,
        )

    @ui.button(
        label="Leave Queue",
        style=discord.ButtonStyle.danger,
        custom_id="queue_leave",
        emoji="❌",
    )
    async def leave_button(self, interaction: discord.Interaction, button: ui.Button):
        queue = get_queue(self.gamemode, self.region)

        if interaction.user.id not in queue:
            await interaction.response.send_message(
                "❌ You are not in the queue.",
                ephemeral=True,
            )
            return

        # Remove from queue
        queue.remove(interaction.user.id)

        # Clear pulled status if this user was pulled
        if get_pulled_user(self.gamemode, self.region) == interaction.user.id:
            set_pulled_user(self.gamemode, self.region, None)

        # Update panel
        embed = await create_queue_embed(interaction.guild, self.gamemode, self.region)
        await interaction.message.edit(embed=embed)

        await interaction.response.send_message(
            "✅ You left the queue.",
            ephemeral=True,
        )


class ResultModal(ui.Modal, title="Submit Tier Result"):
    """Modal for submitting tier test results."""

    def __init__(self, gamemode: str, tester: discord.Member):
        super().__init__()
        self.gamemode = gamemode
        self.tester = tester

    player_id = ui.TextInput(
        label="Discord User ID",
        placeholder="Enter the player's Discord User ID (right-click > Copy ID)",
        required=True,
        max_length=20,
    )

    ign = ui.TextInput(
        label="Minecraft IGN",
        placeholder="Enter the player's Minecraft username",
        required=True,
        max_length=16,
    )

    region = ui.TextInput(
        label="Region",
        placeholder="NA / EU / AS-AU",
        required=True,
        max_length=5,
    )

    previous_tier = ui.TextInput(
        label="Previous Tier",
        placeholder="Unranked, LT5, HT5, LT4, HT4, LT3, HT3, LT2, HT2, LT1, HT1",
        required=True,
        max_length=10,
    )

    new_tier = ui.TextInput(
        label="New Tier",
        placeholder="LT5, HT5, LT4, HT4, LT3, HT3, LT2, HT2, LT1, HT1",
        required=True,
        max_length=10,
    )

    async def on_submit(self, interaction: discord.Interaction):
        # Validate region
        reg = self.region.value.strip().upper()
        if reg == "AS-AU":
            reg = "AS"
        if reg not in ["NA", "EU", "AS"]:
            await interaction.response.send_message(
                "❌ Region must be 'NA', 'EU', or 'AS-AU'.",
                ephemeral=True,
            )
            return

        # Validate tiers
        old_tier = self.previous_tier.value.strip().upper()
        if old_tier.lower() == "unranked":
            old_tier = "Unranked"
        new_tier_val = self.new_tier.value.strip().upper()

        valid_tiers = ["UNRANKED", "LT5", "HT5", "LT4", "HT4", "LT3", "HT3", "LT2", "HT2", "LT1", "HT1"]
        if old_tier.upper() not in valid_tiers:
            await interaction.response.send_message(
                f"❌ Invalid previous tier: `{old_tier}`. Valid: {', '.join(TIERS)}",
                ephemeral=True,
            )
            return

        if new_tier_val not in valid_tiers:
            await interaction.response.send_message(
                f"❌ Invalid new tier: `{new_tier_val}`. Valid: {', '.join(TIERS)}",
                ephemeral=True,
            )
            return

        # Parse player ID
        try:
            player_id = int(self.player_id.value.strip().replace("<@", "").replace(">", "").replace("!", ""))
        except ValueError:
            await interaction.response.send_message(
                "❌ Invalid Discord User ID. Right-click the user > Copy ID.",
                ephemeral=True,
            )
            return

        # Get player member
        player = interaction.guild.get_member(player_id)
        if player is None:
            await interaction.response.send_message(
                "❌ Could not find that user in this server.",
                ephemeral=True,
            )
            return

        ign = self.ign.value.strip()
        gamemode_display = get_gamemode_display(self.gamemode)

        # Defer response while we fetch skin and update roles
        await interaction.response.defer(ephemeral=True)

        # Fetch Minecraft skin
        skin_url = await fetch_minecraft_skin(ign)

        # Update tier roles
        role_success, role_msg = await update_tier_roles(
            interaction.guild, player, self.gamemode, old_tier, new_tier_val
        )

        # Log the result
        result_log.append((
            time.time(),
            self.tester.id,
            player_id,
            ign,
            self.gamemode,
            reg,
            old_tier,
            new_tier_val,
        ))

        # Update cooldown
        result_cooldowns[self.tester.id] = time.time()

        # Create result embed
        result_embed = discord.Embed(
            title=f"{ign}'s Tier Update 🏆",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        result_embed.set_thumbnail(url=skin_url)
        result_embed.add_field(name="Tester", value=self.tester.mention, inline=True)
        result_embed.add_field(name="Minecraft Username", value=ign, inline=True)
        result_embed.add_field(name="Game Mode", value=gamemode_display, inline=True)
        result_embed.add_field(name="Previous Rank", value=old_tier, inline=True)
        result_embed.add_field(name="Rank Earned", value=new_tier_val, inline=True)
        result_embed.add_field(name="Region", value=reg, inline=True)
        result_embed.set_footer(text="Powered by Lunar Tierlist Bot")

        # Find results channel
        results_channel = discord.utils.get(interaction.guild.text_channels, name=RESULTS_CHANNEL_NAME)

        if results_channel is None:
            await interaction.followup.send(
                f"⚠️ Result logged but `#{RESULTS_CHANNEL_NAME}` channel not found. "
                f"Please create the channel to post public results.\n"
                f"Role update: {role_msg}",
                ephemeral=True,
            )
            return

        # Post to results channel
        try:
            await results_channel.send(embed=result_embed)
            await interaction.followup.send(
                f"✅ Result posted to {results_channel.mention}!\n"
                f"Role update: {role_msg}",
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                f"❌ Cannot post to {results_channel.mention}. Check bot permissions.",
                ephemeral=True,
            )


class ProfileModal(ui.Modal, title="Register / Update Profile"):
    """Modal for user profile registration."""

    ign = ui.TextInput(
        label="Minecraft IGN",
        placeholder="Enter your in-game name",
        required=True,
        max_length=16,
    )

    account_type = ui.TextInput(
        label="Account Type",
        placeholder="Premium / Cracked",
        required=True,
        max_length=10,
    )

    region = ui.TextInput(
        label="Region",
        placeholder="NA / EU / AS-AU",
        required=True,
        max_length=5,
    )

    async def on_submit(self, interaction: discord.Interaction):
        acc_type = self.account_type.value.strip().lower()
        if acc_type not in ["premium", "cracked"]:
            await interaction.response.send_message(
                "❌ Account Type must be 'Premium' or 'Cracked'.",
                ephemeral=True,
            )
            return

        reg = self.region.value.strip().upper()
        if reg not in ["NA", "EU", "AS-AU"]:
            await interaction.response.send_message(
                "❌ Region must be 'NA', 'EU', or 'AS-AU'.",
                ephemeral=True,
            )
            return

        user_profiles[interaction.user.id] = {
            "user_id": interaction.user.id,
            "ign": self.ign.value.strip(),
            "account_type": acc_type.capitalize(),
            "region": reg,
        }

        await interaction.response.send_message(
            "✅ Your profile has been saved successfully.",
            ephemeral=True,
        )


class WaitlistView(ui.View):
    """Persistent UI view for main panel with button and dropdown."""

    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(
        label="Register / Update Profile",
        style=discord.ButtonStyle.primary,
        custom_id="register_profile_button",
        row=0,
    )
    async def register_button(self, interaction: discord.Interaction, button: ui.Button):
        modal = ProfileModal()
        await interaction.response.send_modal(modal)

    @ui.select(
        placeholder="Select a gamemode to get the waitlist role",
        custom_id="gamemode_select",
        options=[discord.SelectOption(label=gm, value=gm.lower()) for gm in GAMEMODES],
        row=1,
    )
    async def gamemode_select(self, interaction: discord.Interaction, select: ui.Select):
        if interaction.user.id not in user_profiles:
            await interaction.response.send_message(
                "❌ Please register your profile first.",
                ephemeral=True,
            )
            return

        selected_gamemode = select.values[0]
        gamemode_display = get_gamemode_display(selected_gamemode)
        role_name = f"Waitlist {gamemode_display}"

        guild = interaction.guild
        role = discord.utils.get(guild.roles, name=role_name)

        if role is None:
            try:
                role = await guild.create_role(name=role_name, reason="Waitlist role auto-created")
            except discord.Forbidden:
                await interaction.response.send_message(
                    "❌ Bot lacks permission to create roles.",
                    ephemeral=True,
                )
                return

        try:
            if role not in interaction.user.roles:
                await interaction.user.add_roles(role, reason="Joined waitlist")
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Bot lacks permission to assign roles.",
                ephemeral=True,
            )
            return

        channel_name = f"waitlist-{selected_gamemode}"
        await grant_channel_access(interaction.guild, interaction.user, channel_name)

        await interaction.response.send_message(
            f"✅ You now have access to the {gamemode_display} waitlist.",
            ephemeral=True,
        )


def create_waitlist_embed() -> discord.Embed:
    """Create the main waitlist embed panel with exact specified content."""
    embed = discord.Embed(
        title="📜 Evaluation Testing Waitlist & Roles",
        color=discord.Color.blurple(),
    )

    description = """**Step 1: Register Your Profile**
Click the Register / Update Profile button to set your in-game details.

**Step 2: Get a Waitlist Role**
After registering, select any gamemode below to get the corresponding waitlist role.

• Region: NA, EU, AS/AU
• Username: The name of the account you will be testing on.

⚠️ Failure to provide authentic information will result in a denied test."""

    embed.description = description
    return embed


# ============================================================
# TESTER COMMANDS
# ============================================================

async def start_queue(interaction: discord.Interaction, gamemode: str, region: str):
    """Start a queue for a gamemode and region."""
    gamemode_display = get_gamemode_display(gamemode)

    # Check tester role
    if not has_tester_role(interaction.user, gamemode_display):
        await interaction.response.send_message(
            f"❌ You need the `{gamemode_display} Tester` role to manage this queue.",
            ephemeral=True,
        )
        return

    testers = get_active_testers(gamemode, region)

    if interaction.user.id in testers:
        await interaction.response.send_message(
            "❌ You are already active for this queue.",
            ephemeral=True,
        )
        return

    testers.append(interaction.user.id)

    # Find waitlist channel
    channel_name = f"waitlist-{gamemode.lower()}"
    channel = discord.utils.get(interaction.guild.text_channels, name=channel_name)

    if channel is None:
        await interaction.response.send_message(
            f"❌ Channel #{channel_name} not found.",
            ephemeral=True,
        )
        return

    # Update the existing panel (or create if missing)
    key = get_queue_key(gamemode, region)
    embed = await create_queue_embed(interaction.guild, gamemode, region)
    view = QueueView(gamemode.lower(), region, disabled=False)

    panel_msg_id = queue_panel_messages.get(key)
    ping_content = f"@here A **{gamemode_display}** queue is open for the **{region}** region!"

    if panel_msg_id:
        try:
            msg = await channel.fetch_message(panel_msg_id)
            await msg.edit(content=ping_content, embed=embed, view=view)
        except discord.NotFound:
            # Panel was deleted, create new one
            msg = await channel.send(content=ping_content, embed=embed, view=view)
            queue_panel_messages[key] = msg.id
    else:
        # No panel exists, create one
        msg = await channel.send(content=ping_content, embed=embed, view=view)
        queue_panel_messages[key] = msg.id

    await interaction.response.send_message(
        f"✅ You are now active for **{gamemode_display} ({region})**.",
        ephemeral=True,
    )


async def end_queue(interaction: discord.Interaction, gamemode: str, region: str, clear_queue: bool = False):
    """End queue activity for a tester."""
    gamemode_display = get_gamemode_display(gamemode)

    if not has_tester_role(interaction.user, gamemode_display):
        await interaction.response.send_message(
            f"❌ You need the `{gamemode_display} Tester` role to manage this queue.",
            ephemeral=True,
        )
        return

    testers = get_active_testers(gamemode, region)

    if interaction.user.id not in testers:
        await interaction.response.send_message(
            "❌ You are not active for this queue.",
            ephemeral=True,
        )
        return

    testers.remove(interaction.user.id)

    # If no testers left, close the queue
    if not testers:
        # Clear the queue when closing
        queue = get_queue(gamemode, region)
        queue.clear()
        set_pulled_user(gamemode, region, None)

    # Update queue panel
    key = get_queue_key(gamemode, region)
    channel_name = f"waitlist-{gamemode.lower()}"
    channel = discord.utils.get(interaction.guild.text_channels, name=channel_name)

    if channel:
        panel_msg_id = queue_panel_messages.get(key)
        if panel_msg_id:
            try:
                msg = await channel.fetch_message(panel_msg_id)
                embed = await create_queue_embed(interaction.guild, gamemode, region)
                # Disable buttons if no testers left
                is_closed = len(get_active_testers(gamemode, region)) == 0
                view = QueueView(gamemode.lower(), region, disabled=is_closed)
                await msg.edit(content=None, embed=embed, view=view)
            except discord.NotFound:
                pass

    await interaction.response.send_message(
        f"✅ You are now offline for **{gamemode_display} ({region})**.",
        ephemeral=True,
    )


# Slash commands for starting queues (renamed to /na-start, /eu-start, /as-start)
@bot.tree.command(name="na-start", description="Start testing for NA region")
@app_commands.describe(gamemode="The gamemode to start testing")
@app_commands.choices(gamemode=[app_commands.Choice(name=gm, value=gm.lower()) for gm in GAMEMODES])
async def na_start(interaction: discord.Interaction, gamemode: app_commands.Choice[str]):
    await start_queue(interaction, gamemode.value, "NA")


@bot.tree.command(name="eu-start", description="Start testing for EU region")
@app_commands.describe(gamemode="The gamemode to start testing")
@app_commands.choices(gamemode=[app_commands.Choice(name=gm, value=gm.lower()) for gm in GAMEMODES])
async def eu_start(interaction: discord.Interaction, gamemode: app_commands.Choice[str]):
    await start_queue(interaction, gamemode.value, "EU")


@bot.tree.command(name="as-start", description="Start testing for AS/AU region")
@app_commands.describe(gamemode="The gamemode to start testing")
@app_commands.choices(gamemode=[app_commands.Choice(name=gm, value=gm.lower()) for gm in GAMEMODES])
async def as_start(interaction: discord.Interaction, gamemode: app_commands.Choice[str]):
    await start_queue(interaction, gamemode.value, "AS")


# Slash commands for ending queues (renamed to /na-end, /eu-end, /as-end)
@bot.tree.command(name="na-end", description="End testing for NA region")
@app_commands.describe(gamemode="The gamemode to stop testing")
@app_commands.choices(gamemode=[app_commands.Choice(name=gm, value=gm.lower()) for gm in GAMEMODES])
async def na_end(interaction: discord.Interaction, gamemode: app_commands.Choice[str]):
    await end_queue(interaction, gamemode.value, "NA")


@bot.tree.command(name="eu-end", description="End testing for EU region")
@app_commands.describe(gamemode="The gamemode to stop testing")
@app_commands.choices(gamemode=[app_commands.Choice(name=gm, value=gm.lower()) for gm in GAMEMODES])
async def eu_end(interaction: discord.Interaction, gamemode: app_commands.Choice[str]):
    await end_queue(interaction, gamemode.value, "EU")


@bot.tree.command(name="as-end", description="End testing for AS/AU region")
@app_commands.describe(gamemode="The gamemode to stop testing")
@app_commands.choices(gamemode=[app_commands.Choice(name=gm, value=gm.lower()) for gm in GAMEMODES])
async def as_end(interaction: discord.Interaction, gamemode: app_commands.Choice[str]):
    await end_queue(interaction, gamemode.value, "AS")


# ============================================================
# PULL / NEXT COMMANDS
# ============================================================

@bot.tree.command(name="pull", description="Pull the next user from the queue")
@app_commands.describe(gamemode="The gamemode queue", region="The region (NA/EU/AS)")
@app_commands.choices(
    gamemode=[app_commands.Choice(name=gm, value=gm.lower()) for gm in GAMEMODES],
    region=[app_commands.Choice(name=r, value=r) for r in REGIONS],
)
async def pull_user(
    interaction: discord.Interaction,
    gamemode: app_commands.Choice[str],
    region: app_commands.Choice[str],
):
    """Pull the user at position #1 and lock them."""
    gm = gamemode.value
    reg = region.value
    gamemode_display = get_gamemode_display(gm)

    # Check tester role
    if not has_tester_role(interaction.user, gamemode_display):
        await interaction.response.send_message(
            f"❌ You need the `{gamemode_display} Tester` role to pull users.",
            ephemeral=True,
        )
        return

    # Check if tester is active
    testers = get_active_testers(gm, reg)
    if interaction.user.id not in testers:
        await interaction.response.send_message(
            "❌ You must be active (`/start-*`) before pulling users.",
            ephemeral=True,
        )
        return

    queue = get_queue(gm, reg)

    if not queue:
        await interaction.response.send_message(
            "❌ Queue is empty.",
            ephemeral=True,
        )
        return

    # Check if someone is already pulled
    current_pulled = get_pulled_user(gm, reg)
    if current_pulled is not None:
        member = interaction.guild.get_member(current_pulled)
        name = member.display_name if member else f"User {current_pulled}"
        await interaction.response.send_message(
            f"❌ **{name}** is already pulled. Use `/next` to proceed.",
            ephemeral=True,
        )
        return

    # Pull user at position #1
    pulled_id = queue[0]
    set_pulled_user(gm, reg, pulled_id)

    member = interaction.guild.get_member(pulled_id)
    name = member.display_name if member else f"User {pulled_id}"

    # Update queue panel
    key = get_queue_key(gm, reg)
    channel_name = f"waitlist-{gm}"
    channel = discord.utils.get(interaction.guild.text_channels, name=channel_name)

    if channel:
        panel_msg_id = queue_panel_messages.get(key)
        if panel_msg_id:
            try:
                msg = await channel.fetch_message(panel_msg_id)
                embed = await create_queue_embed(interaction.guild, gm, reg)
                view = QueueView(gm, reg)
                await msg.edit(embed=embed, view=view)
            except discord.NotFound:
                pass

    await interaction.response.send_message(
        f"🔒 Pulled **{name}**. Use `/next` to create the testing channel.",
        ephemeral=True,
    )


@bot.tree.command(name="next", description="Move pulled user to private testing channel")
@app_commands.describe(gamemode="The gamemode queue", region="The region (NA/EU/AS)")
@app_commands.choices(
    gamemode=[app_commands.Choice(name=gm, value=gm.lower()) for gm in GAMEMODES],
    region=[app_commands.Choice(name=r, value=r) for r in REGIONS],
)
async def next_user(
    interaction: discord.Interaction,
    gamemode: app_commands.Choice[str],
    region: app_commands.Choice[str],
):
    """Create private ticket channel for pulled user."""
    gm = gamemode.value
    reg = region.value
    gamemode_display = get_gamemode_display(gm)

    # Check tester role
    if not has_tester_role(interaction.user, gamemode_display):
        await interaction.response.send_message(
            f"❌ You need the `{gamemode_display} Tester` role.",
            ephemeral=True,
        )
        return

    # Check if someone is pulled
    pulled_id = get_pulled_user(gm, reg)
    if pulled_id is None:
        await interaction.response.send_message(
            "❌ No user is pulled. Use `/pull` first.",
            ephemeral=True,
        )
        return

    queue = get_queue(gm, reg)
    member = interaction.guild.get_member(pulled_id)

    if member is None:
        # User left server, remove and clear
        if pulled_id in queue:
            queue.remove(pulled_id)
        set_pulled_user(gm, reg, None)
        await interaction.response.send_message(
            "❌ Pulled user left the server. Cleared from queue.",
            ephemeral=True,
        )
        return

    # Find or create "Testing Tickets" category
    category = discord.utils.get(interaction.guild.categories, name="Testing Tickets")
    if category is None:
        try:
            category = await interaction.guild.create_category(
                "Testing Tickets",
                reason="Testing ticket category auto-created",
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Bot lacks permission to create categories.",
                ephemeral=True,
            )
            return

    # Create private ticket channel
    ticket_name = f"test-{member.name}-{gm}"[:50]

    # Build permission overwrites
    overwrites = {
        interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        interaction.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }

    # Add staff roles
    for role_name in STAFF_ROLES:
        role = discord.utils.get(interaction.guild.roles, name=role_name)
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    try:
        ticket_channel = await interaction.guild.create_text_channel(
            ticket_name,
            category=category,
            overwrites=overwrites,
            reason=f"Testing ticket for {member.display_name}",
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ Bot lacks permission to create channels.",
            ephemeral=True,
        )
        return

    # Remove user from queue
    if pulled_id in queue:
        queue.remove(pulled_id)
    set_pulled_user(gm, reg, None)

    # Update queue panel
    key = get_queue_key(gm, reg)
    channel_name = f"waitlist-{gm}"
    channel = discord.utils.get(interaction.guild.text_channels, name=channel_name)

    if channel:
        panel_msg_id = queue_panel_messages.get(key)
        if panel_msg_id:
            try:
                msg = await channel.fetch_message(panel_msg_id)
                embed = await create_queue_embed(interaction.guild, gm, reg)
                view = QueueView(gm, reg)
                await msg.edit(embed=embed, view=view)
            except discord.NotFound:
                pass

    # Send welcome message in ticket
    profile = user_profiles.get(pulled_id, {})
    ign = profile.get("ign", "Unknown")
    acc_type = profile.get("account_type", "Unknown")
    user_region = profile.get("region", "Unknown")

    ticket_embed = discord.Embed(
        title=f"🎮 {gamemode_display} Test — {reg}",
        color=discord.Color.blue(),
    )
    ticket_embed.add_field(name="Player", value=member.mention, inline=True)
    ticket_embed.add_field(name="Tester", value=interaction.user.mention, inline=True)
    ticket_embed.add_field(name="IGN", value=ign, inline=True)
    ticket_embed.add_field(name="Account Type", value=acc_type, inline=True)
    ticket_embed.add_field(name="Region", value=user_region, inline=True)

    await ticket_channel.send(embed=ticket_embed)

    await interaction.response.send_message(
        f"✅ Created ticket: {ticket_channel.mention}",
        ephemeral=True,
    )


# ============================================================
# BOT EVENTS & PANEL COMMANDS
# ============================================================

async def initialize_queue_panels(guild: discord.Guild):
    """Initialize closed queue panels in all waitlist channels on startup."""
    for gm in GAMEMODES:
        channel_name = f"waitlist-{gm.lower()}"
        channel = discord.utils.get(guild.text_channels, name=channel_name)

        if channel is None:
            continue

        for reg in REGIONS:
            key = get_queue_key(gm, reg)

            # Skip if we already have a panel for this combo
            if key in queue_panel_messages:
                continue

            # Create closed panel
            embed = create_closed_queue_embed(gm, reg)
            view = QueueView(gm.lower(), reg, disabled=True)

            try:
                msg = await channel.send(embed=embed, view=view)
                queue_panel_messages[key] = msg.id
                print(f"Created panel for {gm} ({reg}) in #{channel_name}")
            except discord.Forbidden:
                print(f"Cannot send to #{channel_name}")


@bot.event
async def on_ready():
    print(f"Bot is ready: {bot.user}")
    # Register persistent views
    bot.add_view(WaitlistView())
    # Register queue views for all gamemode/region combos
    for gm in GAMEMODES:
        for reg in REGIONS:
            bot.add_view(QueueView(gm.lower(), reg, disabled=True))
            bot.add_view(QueueView(gm.lower(), reg, disabled=False))

    # Sync slash commands
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

    # Initialize panels in all guilds
    for guild in bot.guilds:
        await initialize_queue_panels(guild)


@bot.command(name="panel")
async def send_panel(ctx: commands.Context):
    """Send the main waitlist panel (prefix command)."""
    embed = create_waitlist_embed()
    view = WaitlistView()
    await ctx.send(embed=embed, view=view)


@bot.tree.command(name="panel", description="Send the waitlist panel")
async def slash_panel(interaction: discord.Interaction):
    """Send the main waitlist panel (slash command)."""
    embed = create_waitlist_embed()
    view = WaitlistView()
    await interaction.response.send_message(embed=embed, view=view)


# ============================================================
# ADMIN COMMANDS
# ============================================================

def is_admin(member: discord.Member) -> bool:
    """Check if member has admin permissions."""
    admin_roles = ["Admin", "Administrator", "Manager", "Head Tester"]
    return any(role.name in admin_roles for role in member.roles) or member.guild_permissions.administrator


@bot.tree.command(name="status", description="Check queue status")
@app_commands.describe(gamemode="The gamemode", region="The region (NA/EU/AS)")
@app_commands.choices(
    gamemode=[app_commands.Choice(name=gm, value=gm.lower()) for gm in GAMEMODES],
    region=[app_commands.Choice(name=r, value=r) for r in REGIONS],
)
async def queue_status(
    interaction: discord.Interaction,
    gamemode: app_commands.Choice[str],
    region: app_commands.Choice[str],
):
    """Show tester online/offline status and queue size."""
    gm = gamemode.value
    reg = region.value
    gamemode_display = get_gamemode_display(gm)

    testers = get_active_testers(gm, reg)
    queue = get_queue(gm, reg)

    status = "🟢 OPEN" if testers else "🔴 CLOSED"

    embed = discord.Embed(
        title=f"{gamemode_display} ({reg}) Status",
        color=discord.Color.green() if testers else discord.Color.red(),
    )
    embed.add_field(name="Status", value=status, inline=True)
    embed.add_field(name="Queue Size", value=str(len(queue)), inline=True)
    embed.add_field(name="Active Testers", value=str(len(testers)), inline=True)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="force-open", description="[Admin] Force open a queue")
@app_commands.describe(gamemode="The gamemode", region="The region", tester="Tester to add")
@app_commands.choices(
    gamemode=[app_commands.Choice(name=gm, value=gm.lower()) for gm in GAMEMODES],
    region=[app_commands.Choice(name=r, value=r) for r in REGIONS],
)
async def force_open(
    interaction: discord.Interaction,
    gamemode: app_commands.Choice[str],
    region: app_commands.Choice[str],
    tester: discord.Member,
):
    """Admin force open a queue with a tester."""
    if not is_admin(interaction.user):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return

    gm = gamemode.value
    reg = region.value
    gamemode_display = get_gamemode_display(gm)

    testers = get_active_testers(gm, reg)
    if tester.id not in testers:
        testers.append(tester.id)

    channel_name = f"waitlist-{gm}"
    channel = discord.utils.get(interaction.guild.text_channels, name=channel_name)

    if channel:
        key = get_queue_key(gm, reg)
        embed = await create_queue_embed(interaction.guild, gm, reg)
        view = QueueView(gm, reg, disabled=False)
        ping_content = f"@here A **{gamemode_display}** queue is open for the **{reg}** region!"

        panel_msg_id = queue_panel_messages.get(key)
        if panel_msg_id:
            try:
                msg = await channel.fetch_message(panel_msg_id)
                await msg.edit(content=ping_content, embed=embed, view=view)
            except discord.NotFound:
                msg = await channel.send(content=ping_content, embed=embed, view=view)
                queue_panel_messages[key] = msg.id
        else:
            msg = await channel.send(content=ping_content, embed=embed, view=view)
            queue_panel_messages[key] = msg.id

    await interaction.response.send_message(
        f"✅ Force opened **{gamemode_display} ({reg})** with {tester.mention}.",
        ephemeral=True,
    )


@bot.tree.command(name="force-close", description="[Admin] Force close a queue")
@app_commands.describe(gamemode="The gamemode", region="The region")
@app_commands.choices(
    gamemode=[app_commands.Choice(name=gm, value=gm.lower()) for gm in GAMEMODES],
    region=[app_commands.Choice(name=r, value=r) for r in REGIONS],
)
async def force_close(
    interaction: discord.Interaction,
    gamemode: app_commands.Choice[str],
    region: app_commands.Choice[str],
):
    """Admin force close a queue."""
    if not is_admin(interaction.user):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return

    gm = gamemode.value
    reg = region.value
    gamemode_display = get_gamemode_display(gm)

    # Clear testers and queue
    testers = get_active_testers(gm, reg)
    testers.clear()
    queue = get_queue(gm, reg)
    queue.clear()
    set_pulled_user(gm, reg, None)

    channel_name = f"waitlist-{gm}"
    channel = discord.utils.get(interaction.guild.text_channels, name=channel_name)

    if channel:
        key = get_queue_key(gm, reg)
        embed = create_closed_queue_embed(gm, reg)
        view = QueueView(gm, reg, disabled=True)

        panel_msg_id = queue_panel_messages.get(key)
        if panel_msg_id:
            try:
                msg = await channel.fetch_message(panel_msg_id)
                await msg.edit(content=None, embed=embed, view=view)
            except discord.NotFound:
                pass

    await interaction.response.send_message(
        f"✅ Force closed **{gamemode_display} ({reg})**.",
        ephemeral=True,
    )


@bot.tree.command(name="clear-queue", description="[Admin] Clear a queue without closing")
@app_commands.describe(gamemode="The gamemode", region="The region")
@app_commands.choices(
    gamemode=[app_commands.Choice(name=gm, value=gm.lower()) for gm in GAMEMODES],
    region=[app_commands.Choice(name=r, value=r) for r in REGIONS],
)
async def clear_queue_cmd(
    interaction: discord.Interaction,
    gamemode: app_commands.Choice[str],
    region: app_commands.Choice[str],
):
    """Admin clear the queue."""
    if not is_admin(interaction.user):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return

    gm = gamemode.value
    reg = region.value
    gamemode_display = get_gamemode_display(gm)

    queue = get_queue(gm, reg)
    cleared = len(queue)
    queue.clear()
    set_pulled_user(gm, reg, None)

    # Update panel
    channel_name = f"waitlist-{gm}"
    channel = discord.utils.get(interaction.guild.text_channels, name=channel_name)

    if channel:
        key = get_queue_key(gm, reg)
        panel_msg_id = queue_panel_messages.get(key)
        if panel_msg_id:
            try:
                msg = await channel.fetch_message(panel_msg_id)
                embed = await create_queue_embed(interaction.guild, gm, reg)
                testers = get_active_testers(gm, reg)
                view = QueueView(gm, reg, disabled=len(testers) == 0)
                await msg.edit(embed=embed, view=view)
            except discord.NotFound:
                pass

    await interaction.response.send_message(
        f"✅ Cleared {cleared} user(s) from **{gamemode_display} ({reg})** queue.",
        ephemeral=True,
    )


# ============================================================
# RESULT COMMAND
# ============================================================

@bot.tree.command(name="result", description="Submit a tier test result")
@app_commands.describe(gamemode="The gamemode the test was for")
@app_commands.choices(gamemode=[app_commands.Choice(name=gm, value=gm.lower()) for gm in GAMEMODES])
async def submit_result(interaction: discord.Interaction, gamemode: app_commands.Choice[str]):
    """Open result submission modal for a completed test."""
    gm = gamemode.value
    gamemode_display = get_gamemode_display(gm)

    # Check permission
    can_submit, error_msg = can_submit_result(interaction.user, gm)
    if not can_submit:
        await interaction.response.send_message(
            f"❌ {error_msg}",
            ephemeral=True,
        )
        return

    # Check cooldown
    on_cooldown, remaining = check_cooldown(interaction.user.id)
    if on_cooldown:
        await interaction.response.send_message(
            f"❌ Please wait {remaining} seconds before submitting another result.",
            ephemeral=True,
        )
        return

    # Open modal
    modal = ResultModal(gm, interaction.user)
    await interaction.response.send_modal(modal)


@bot.tree.command(name="set-results-channel", description="Set the channel for posting tier results")
@app_commands.describe(channel="The channel to post results in")
async def set_results_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    """Set the results channel (Admin only)."""
    # Check for admin permissions
    has_admin = any(role.name in ["Admin", "Administrator", "Manager"] for role in interaction.user.roles)
    if not has_admin and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "❌ You need Admin permissions to use this command.",
            ephemeral=True,
        )
        return

    global RESULTS_CHANNEL_NAME
    RESULTS_CHANNEL_NAME = channel.name

    await interaction.response.send_message(
        f"✅ Results will now be posted to {channel.mention}",
        ephemeral=True,
    )


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("Error: DISCORD_TOKEN environment variable not set.")
        exit(1)
    bot.run(DISCORD_TOKEN)
