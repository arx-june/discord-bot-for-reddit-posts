
"""
Discord Task Allocation Bot - MODIFIED VERSION WITH DM FEATURE
============================================================

SETUP INSTRUCTIONS:
1. Discord Bot Setup:
   - Go to https://discord.com/developers/applications
   - Create new application and bot
   - Copy bot token to .env file
   - CRITICAL: Invite bot with BOTH scopes: 'bot' AND 'applications.commands'
   - Permissions needed: Send Messages, Manage Messages, Add Reactions, Manage Roles, Use Slash Commands

2. Google Sheets Setup:
   - Enable Google Sheets API and Google Drive API
   - Create service account, download credentials.json
   - Share sheet with service account email
   - Headers: Task No. | Post link | Comment to post | Assigned user | Proof link

3. Environment Setup:
   Create .env file:
   DISCORD_TOKEN=your_bot_token_here
   TASK_ROLE_NAME=TaskHolder

4. Install: pip install discord.py gspread google-auth python-dotenv

5. Usage:
   /configure_settings (Admin only) - Set up all bot settings
   /create_task tasks:100 sheet_url:your_sheet_url (Admin only) - Start task allocation
"""

import discord, asyncpraw
from discord.ext import commands, tasks
from discord import app_commands
import gspread
from google.oauth2.service_account import Credentials
import os
import re
import asyncio, functools
import logging
from datetime import datetime
from typing import Optional, Dict
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configuration
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
TASK_ROLE_NAME = os.getenv('TASK_ROLE_NAME', 'TaskHolder')
GOOGLE_SHEETS_SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]
# Reddit API credentials
REDDIT_CLIENT_ID = os.getenv('REDDIT_CLIENT_ID')
REDDIT_CLIENT_SECRET = os.getenv('REDDIT_CLIENT_SECRET')
REDDIT_USER_AGENT = os.getenv('REDDIT_USER_AGENT', 'TaskBot/1.0')

if not DISCORD_TOKEN:
    raise ValueError("DISCORD_TOKEN environment variable is required")

class TaskBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True
        intents.guilds = True
        intents.guild_messages = True
        intents.members = True  # Required for Member objects
        
        super().__init__(command_prefix='!', intents=intents)
        
        # Bot state
        self.total_tasks: int = 0
        self.sheet_url: str = ""
        # Move this to after interval_minutes is defined
        self.interval_minutes: int = 4
        self.configured: bool = False
        self.loop_should_run: bool = False
        self.task_allocation_loop = tasks.loop(minutes=1)(self.task_allocation_loop_impl) # type: ignore
        self.announce_channel: Optional[discord.TextChannel] = None
        self.logs_channel: Optional[discord.TextChannel] = None
        self.current_task: int = 1
        self.gc = None
        self.commands_synced: bool = False
        self.reaction_timestamps: Dict[int, Dict[int, datetime]] = {}
        self.reaction_time: int = 5  # Default reaction time in seconds
        self.role_removal_hours: int = 12  # Default role removal time in hours
        self.ping_role_name: str = "✅・VERIFIED"  # Role to ping for task announcements
        # Add this new attribute after the existing attributes
        self.winners_per_task: int = 1  # Default to 1 winner per task   
        self.task_type: str = "Comment"  # Default task type   

        
    async def get_member_safely(self, guild: discord.Guild, user_id: int) -> Optional[discord.Member]:
        """Safely get member from guild with fallback to fetch"""
        if not guild:
            return None
            
        # Try cache first
        member = guild.get_member(user_id)
        if member:
            return member
            
        # Fallback to API fetch
        try:
            member = await guild.fetch_member(user_id)
            return member
        except discord.NotFound:
            logger.error(f"User {user_id} not found in guild {guild.name}")
            return None
        except discord.HTTPException as e:
            logger.error(f"Failed to fetch member {user_id}: {e}")
            return None
    
    
    async def setup_hook(self):
        """Initialize Google Sheets connection"""
        await self.setup_google_sheets()
        
    @tasks.loop(minutes=4)
    async def task_allocation_loop(self):
        """Main task allocation loop"""
        if not self.configured or not self.announce_channel:
            return
            
        if self.current_task > self.total_tasks:
            embed = discord.Embed(
                title="🎉 All Tasks Complete!",
                description="All tasks have been assigned!",
                color=discord.Color.green()
            )
            await self.announce_channel.send(embed=embed)
            await self.send_log("All tasks have been completed!")
            self.task_allocation_loop.stop() # type: ignore
            return
            
        try:
            # Post task announcement
            embed = discord.Embed(
                title="🔔 Task Available!",
                description=f"React with ✅ within {self.reaction_time} seconds to claim task #{self.current_task}!",
                color=discord.Color.blue()
            )

            # Send the embed message
            message = await self.announce_channel.send(embed=embed)
            self.reaction_timestamps[message.id] = {}
            await message.add_reaction('✅')

            # FIXED: Send role ping immediately after embed, before any other logic
            role_to_ping = discord.utils.get(self.announce_channel.guild.roles, name=self.ping_role_name)
            if role_to_ping:
                try:
                    ping_message = await self.announce_channel.send(content=role_to_ping.mention, delete_after=1)
                    logger.info(f"Role ping sent for {self.ping_role_name}")
                except Exception as ping_error:
                    logger.error(f"Failed to send role ping: {ping_error}")
                    await self.send_log(f"⚠️ Failed to send role ping: {ping_error}")
            else:
                logger.warning(f"Role '{self.ping_role_name}' not found!")
                all_roles = [role.name for role in self.announce_channel.guild.roles]
                logger.info(f"Available roles: {all_roles}")
                await self.send_log(f"⚠️ Role '{self.ping_role_name}' not found - no ping sent")

            # Log task creation
            await self.send_log(f"Task #{self.current_task} created and posted")
            
            # Wait for configured reaction time
            await asyncio.sleep(self.reaction_time)
            
            # Get reactions with error handling
            try:
                message = await self.announce_channel.fetch_message(message.id)
            except discord.NotFound:
                logger.error(f"Message {message.id} not found")
                if message.id in self.reaction_timestamps:
                    del self.reaction_timestamps[message.id]
                return
            except discord.HTTPException as e:
                logger.error(f"Failed to fetch message: {e}")
                if message.id in self.reaction_timestamps:
                    del self.reaction_timestamps[message.id]
                return
                
            checkmark_reaction = None
            
            for reaction in message.reactions:
                if reaction.emoji == '✅':
                    checkmark_reaction = reaction
                    break
                    
            if not checkmark_reaction:
                return
                
            # Get non-bot reactors
            reactors = []
            async for user in checkmark_reaction.users():
                if not user.bot:
                    reactors.append(user)
                    
            if reactors:
                # Find first reactor by timestamp
                winner = reactors[0]
                earliest_time = None
                
                for reactor in reactors:
                    if (message.id in self.reaction_timestamps and 
                        reactor.id in self.reaction_timestamps[message.id]):
                        timestamp = self.reaction_timestamps[message.id][reactor.id]
                        if earliest_time is None or timestamp < earliest_time:
                            earliest_time = timestamp
                            winner = reactor

                    if earliest_time is None:
                        winner = reactors[0]
                            
            
                # Convert User to Member for role assignment
                winner_member = await self.get_member_safely(message.guild, winner.id) if message.guild else None
                if not winner_member:
                    logger.error(f"User {winner.name} not found in guild")
                    await self.announce_channel.send(f"❌ Could not find {winner.mention} in server")
                    return
                            
                # Announce winner
                embed = discord.Embed(
                    title="➡️ Task Assigned!",
                    description=f"Task #{self.current_task} goes to {winner.mention}!",
                    color=discord.Color.green()
                )
                await self.announce_channel.send(embed=embed)
                
                # Assign role
                try:
                    if winner_member and message.guild and winner_member in message.guild.members:
                        role = await self.get_or_create_role(message.guild, TASK_ROLE_NAME)
                        
                        # Check if bot has permission to manage roles
                        bot_member = message.guild.me
                        if not bot_member.guild_permissions.manage_roles:
                            logger.error("Bot lacks manage_roles permission")
                            await self.announce_channel.send("❌ Bot needs 'Manage Roles' permission!")
                            return
                            
                        # Check if bot's role is higher than target role
                        if bot_member.top_role.position <= role.position:
                            logger.error(f"Bot role too low to assign {role.name}")
                            await self.announce_channel.send(f"❌ Bot role must be higher than {role.name}!")
                            return
                        
                        await winner_member.add_roles(role, reason=f"TaskBot: Task #{self.current_task}")
                        # Schedule role removal as background task
                        removal_task = asyncio.create_task(self.schedule_role_removal(winner_member, role))
                        # Store task reference to prevent garbage collection
                        if not hasattr(self, '_role_removal_tasks'):
                            self._role_removal_tasks = set()
                        self._role_removal_tasks.add(removal_task)
                        removal_task.add_done_callback(self._role_removal_tasks.discard)

                        logger.info(f"Successfully assigned {role.name} to {winner_member.name}")
                        
                        # Log role assignment
                        await self.send_log(f"TaskHolder role given to {winner_member.mention} for Task #{self.current_task}")
                        
                        # DM the winner with sheet link and instructions
                        await self.dm_winner(winner_member, self.current_task)
                        
                        # Confirm role assignment
                        if role in winner_member.roles:
                            logger.info(f"Role assignment confirmed for {winner_member.name}")
                        else:
                            logger.error(f"Role assignment failed for {winner_member.name}")
                            await self.announce_channel.send(f"❌ Failed to assign role to {winner_member.mention}")
                            
                    else:
                        logger.error(f"Could not get member object for user {winner.name}")
                        await self.announce_channel.send(f"❌ Could not assign role to {winner.mention}")
                        
                except discord.Forbidden:
                    logger.error("Bot forbidden from managing roles")
                    await self.announce_channel.send("❌ Bot lacks permission to manage roles!")
                except discord.HTTPException as e:
                    logger.error(f"HTTP error assigning role: {e}")
                    await self.announce_channel.send(f"❌ Error assigning role: {e}")
                except Exception as e:
                    logger.error(f"Unexpected error assigning role: {e}")
                    await self.announce_channel.send(f"❌ Unexpected error: {e}")
                    
                # Update sheet
                await self.write_to_sheet(self.current_task, winner.name)
                
                # Next task
                self.current_task += 1
                
            else:
                # No reactors
                embed = discord.Embed(
                    title="⚠️ No Claims",
                    description=f"No one claimed task #{self.current_task}. Reposting in {self.interval_minutes} minutes…",
                    color=discord.Color.orange()
                )
                await self.announce_channel.send(embed=embed)
                await self.send_log(f"No one claimed Task #{self.current_task}. Reposting soon.")
                
            # Cleanup with safety check
            try:
                if message and message.id in self.reaction_timestamps:
                    del self.reaction_timestamps[message.id]
            except Exception as e:
                logger.error(f"Cleanup error: {e}")
                
        except Exception as e:
            logger.error(f"Task allocation error: {e}")
    
    async def setup_google_sheets(self):
        """Initialize Google Sheets API connection"""
        try:
            if os.path.exists('credentials.json'):
                credentials = Credentials.from_service_account_file(
                    'credentials.json', scopes=GOOGLE_SHEETS_SCOPES
                )
                self.gc = gspread.authorize(credentials)
                logger.info("Google Sheets API initialized")
            else:
                logger.warning("credentials.json not found")
        except Exception as e:
            logger.error(f"Google Sheets setup failed: {e}")
            
    async def on_ready(self):
        """Called when bot is ready"""
        logger.info(f'{self.user} connected to Discord!')
        logger.info(f'Bot is in {len(self.guilds)} servers')
        
        # Sync commands only once
        if not self.commands_synced:
            await self.sync_commands()
            self.commands_synced = True
            
    async def sync_commands(self):
        """Sync slash commands"""
        try:
            # Global sync (takes up to 1 hour)
            synced = await self.tree.sync()
            logger.info(f"Synced {len(synced)} commands globally")
            
            # Guild-specific sync (immediate)
            for guild in self.guilds:
                try:
                    guild_synced = await self.tree.sync(guild=guild)
                    logger.info(f"Synced {len(guild_synced)} commands to {guild.name}")
                except Exception as e:
                    logger.error(f"Failed to sync to {guild.name}: {e}")
                    
        except Exception as e:
            logger.error(f"Command sync failed: {e}")
            
    async def on_reaction_add(self, reaction, user):
        """Track reaction timestamps"""
        if user.bot or reaction.emoji != '✅':
            return
            
        message_id = reaction.message.id
        if message_id not in self.reaction_timestamps:
            self.reaction_timestamps[message_id] = {}
            
        if user.id not in self.reaction_timestamps[message_id]:
            self.reaction_timestamps[message_id][user.id] = datetime.now()
            
    def extract_sheet_id(self, url: str) -> Optional[str]:
        """Extract Google Sheets ID from URL"""
        match = re.search(r'/d/([A-Za-z0-9\-_]+)', url)
        return match.group(1) if match else None
        
    async def validate_sheet_access(self, sheet_url: str) -> tuple[bool, str]:
        """Validate Google Sheets access"""
        if not self.gc:
            return False, "Google Sheets API not initialized"
            
        try:
            sheet_id = self.extract_sheet_id(sheet_url)
            if not sheet_id:
                return False, "Invalid Google Sheets URL"
                
            sheet = self.gc.open_by_key(sheet_id)
            worksheet = sheet.get_worksheet(0)
            worksheet.row_values(1)  # Test access
            return True, "Sheet access verified"
            
        except Exception as e:
            return False, f"Sheet access failed: {e}"
            
    async def get_or_create_role(self, guild: discord.Guild, role_name: str) -> discord.Role:
        """Get or create role"""
        role = discord.utils.get(guild.roles, name=role_name)
        if not role:
            try:
                role = await guild.create_role(name=role_name, reason="TaskBot role")
                logger.info(f"Created role '{role_name}' in {guild.name}")
            except discord.Forbidden:
                logger.error(f"Bot lacks permission to create role '{role_name}'")
                raise
            except Exception as e:
                logger.error(f"Failed to create role '{role_name}': {e}")
                raise
        return role
    
    async def write_to_sheet(self, task_number: int, winner_name: str):
        """Write winner to Google Sheets with retry logic"""
        if not self.gc or not self.sheet_url:
            logger.warning("Google Sheets not configured")
            return
            
        for attempt in range(3):  # 3 retry attempts
            try:
                sheet_id = self.extract_sheet_id(self.sheet_url)
                if not sheet_id:
                    return
                    
                sheet = self.gc.open_by_key(sheet_id)
                try:
                    worksheet = sheet.worksheet("Tasks")
                except gspread.WorksheetNotFound:
                    worksheet = sheet.get_worksheet(0)
                    
                # Validate row exists
                if task_number + 1 > len(worksheet.get_all_values()):
                    logger.error(f"Task {task_number} row doesn't exist in sheet")
                    return
                    
                # Write to column D (Assigned user)
                worksheet.update_cell(task_number + 1, 4, winner_name)
                logger.info(f"Updated sheet: Task {task_number} → {winner_name}")
                return
                
            except Exception as e:
                logger.error(f"Sheet write attempt {attempt + 1} failed: {e}")
                if attempt < 2:  # Don't sleep on last attempt
                    await asyncio.sleep(2 ** attempt)  # Exponential backoff
                else:
                    logger.error(f"Failed to write to sheet after 3 attempts")

    async def dm_winner(self, winner_member: discord.Member, task_number: int):
        """DM the winner with sheet link and instructions"""
        try:
            # Create embed for DM
            embed = discord.Embed(
                title="🎉 Congratulations! Task Assigned",
                description=f"You have been assigned **Task #{task_number}**!",
                color=discord.Color.green()
            )
            
            # Add sheet link as hyperlink
            embed.add_field(
                name="📋 Google Sheets Link",
                value=f"[Click here to access the task sheet]({self.sheet_url})",
                inline=False
            )
            
            embed.add_field(
                name="📝 Instructions",
                value="Please fill in the **Proof link** column (Column E) in the sheet with your Reddit post link once you complete the task.",
                inline=False
            )
            
            embed.add_field(
                name="⏰ Task Details",
                value=f"Task Number: **{task_number}**\nRole: **{TASK_ROLE_NAME}** (will be removed in {self.role_removal_hours} hours)",
                inline=False
            )
            
            embed.set_footer(text="Good luck with your task!")
            
            # Try to send DM
            try:
                await winner_member.send(embed=embed)
                logger.info(f"Successfully sent DM to {winner_member.name} for Task #{task_number}")
                
                # Log successful DM
                await self.send_log(f"✅ DM sent to {winner_member.mention} for Task #{task_number}")
                
                return True
                
            except (discord.Forbidden, discord.HTTPException) as e:
                logger.warning(f"Cannot DM {winner_member.name}: {e}")
                
                # Send in channel instead with inaccessible DM notice
                channel_embed = discord.Embed(
                    title="🚫 Inaccessible DM's",
                    description=f"{winner_member.mention} - Your DMs are inaccessible, so here's your task information:",
                    color=discord.Color.orange()
                )
                
                channel_embed.add_field(
                    name="🎉 Task Assigned",
                    value=f"You have been assigned **Task #{task_number}**!",
                    inline=False
                )
                
                channel_embed.add_field(
                    name="📋 Google Sheets Link",
                    value=f"[Click here to access the task sheet]({self.sheet_url})",
                    inline=False
                )
                
                channel_embed.add_field(
                    name="📝 Instructions",
                    value="Please fill in the **Proof link** column (Column E) in the sheet with your Reddit post link once you complete the task.",
                    inline=False
                )
                
                channel_embed.add_field(
                    name="⏰ Task Details",
                    value=f"Task Number: **{task_number}**\nRole: **{TASK_ROLE_NAME}** (will be removed in {self.role_removal_hours} hours)",
                    inline=False
                )
                
                channel_embed.set_footer(text="Please enable DMs for future tasks!")
                
                # Send in announce channel
                if self.announce_channel:
                    await self.announce_channel.send(embed=channel_embed)
                    
                # Log inaccessible DM
                await self.send_log(f"🚫 DM inaccessible for {winner_member.mention} - sent in channel instead")
                
                return False
                
        except Exception as e:
            logger.error(f"Error in dm_winner: {e}")
            return False
            
    async def schedule_role_removal(self, member: discord.Member, role: discord.Role):
        """Remove role after configured hours"""
        await asyncio.sleep(self.role_removal_hours * 60 * 60)  # Convert hours to seconds
        try:
            if role in member.roles:
                await member.remove_roles(role, reason=f"TaskBot: {self.role_removal_hours}-hour removal")
                logger.info(f"Removed {role.name} from {member.name} after {self.role_removal_hours} hours")
                await self.send_log(f"TaskHolder role removed from {member.mention} after {self.role_removal_hours} hours")
        except Exception as e:
            logger.error(f"Failed to remove role: {e}")
            
    async def restart_task_loop(self):
        """Restart task loop with new interval"""
        # Stop existing loop
        if self.task_allocation_loop is not None and self.task_allocation_loop.is_running():
            self.task_allocation_loop.cancel()
            
        # Wait for cleanup
        await asyncio.sleep(0.1)
        
        # Create new loop with updated interval
        self.task_allocation_loop = tasks.loop(minutes=self.interval_minutes)(self.task_allocation_loop_impl) # type: ignore
        
        # Start if configured
        if self.configured and self.announce_channel:
            self.task_allocation_loop.start()
    
    async def task_allocation_loop_impl(self):
        """Implementation moved to separate method"""
        if not self.configured or not self.announce_channel:
            return
            
        if self.current_task > self.total_tasks:
            embed = discord.Embed(
                title="🎉 All Tasks Complete!",
                description="All tasks have been assigned!",
                color=discord.Color.green()
            )
            await self.announce_channel.send(embed=embed)
            await self.send_log("All tasks have been completed!")
            self.task_allocation_loop.stop() # type: ignore
            return
            
        try:
            # CRITICAL FIX: Store the starting task number at the very beginning
            starting_task_num = self.current_task
            
            # Calculate remaining tasks for this round
            remaining_tasks = self.total_tasks - starting_task_num + 1
            # FIXED: Don't limit by remaining tasks when we want multiple winners per task
            tasks_this_round = self.winners_per_task
            
            logger.info(f"DEBUG: starting_task_num={starting_task_num}, remaining_tasks={remaining_tasks}, winners_per_task={self.winners_per_task}, tasks_this_round={tasks_this_round}")
            
            # Post task announcement
            if tasks_this_round == 1:
                description = f"React with ✅ within {self.reaction_time} seconds to claim task #{starting_task_num}!"
                people_needed = "1"
            else:
                task_range = f"#{starting_task_num}-#{starting_task_num + tasks_this_round - 1}"
                description = f"React with ✅ within {self.reaction_time} seconds to claim tasks {task_range}!"
                people_needed = str(tasks_this_round)

            embed = discord.Embed(
                title="🔔 Task Available!",
                description=description,
                color=discord.Color.blue()
            )
            embed.add_field(name="Task Type", value=self.task_type.title(), inline=True)
            embed.add_field(name="People Needed", value=people_needed, inline=True)

            # Send the embed message
            message = await self.announce_channel.send(embed=embed)
            self.reaction_timestamps[message.id] = {}
            await message.add_reaction('✅')

            # Send role ping immediately after embed
            role_to_ping = discord.utils.get(self.announce_channel.guild.roles, name=self.ping_role_name)
            if role_to_ping:
                try:
                    ping_message = await self.announce_channel.send(content=role_to_ping.mention, delete_after=1)
                    logger.info(f"Role ping sent for {self.ping_role_name}")
                except Exception as ping_error:
                    logger.error(f"Failed to send role ping: {ping_error}")
                    await self.send_log(f"⚠️ Failed to send role ping: {ping_error}")
            else:
                logger.warning(f"Role '{self.ping_role_name}' not found!")
                await self.send_log(f"⚠️ Role '{self.ping_role_name}' not found - no ping sent")

            # Log task creation
            if tasks_this_round == 1:
                await self.send_log(f"Task #{starting_task_num} created and posted")
            else:
                await self.send_log(f"Tasks #{starting_task_num}-#{starting_task_num + tasks_this_round - 1} created and posted")
            
            # Wait for configured reaction time
            await asyncio.sleep(self.reaction_time)
            
            # Get reactions with error handling
            try:
                message = await self.announce_channel.fetch_message(message.id)
            except discord.NotFound:
                logger.error(f"Message {message.id} not found")
                if message.id in self.reaction_timestamps:
                    del self.reaction_timestamps[message.id]
                return
            except discord.HTTPException as e:
                logger.error(f"Failed to fetch message: {e}")
                if message.id in self.reaction_timestamps:
                    del self.reaction_timestamps[message.id]
                return
                
            checkmark_reaction = None
            
            for reaction in message.reactions:
                if reaction.emoji == '✅':
                    checkmark_reaction = reaction
                    break
                    
            if not checkmark_reaction:
                return
                
            # Get non-bot reactors
            reactors = []
            async for user in checkmark_reaction.users():
                if not user.bot:
                    reactors.append(user)
                    
            logger.info(f"DEBUG: Found {len(reactors)} reactors: {[r.name for r in reactors]}")
            logger.info(f"DEBUG: tasks_this_round = {tasks_this_round}")
            logger.info(f"DEBUG: winners_per_task = {self.winners_per_task}")
                    
            if reactors:
                # Sort reactors by timestamp (earliest first)
                sorted_reactors = []
                for reactor in reactors:
                    if (message.id in self.reaction_timestamps and 
                        reactor.id in self.reaction_timestamps[message.id]):
                        timestamp = self.reaction_timestamps[message.id][reactor.id]
                        sorted_reactors.append((reactor, timestamp))
                    else:
                        # If no timestamp recorded, add to end
                        sorted_reactors.append((reactor, datetime.now()))
                
                # Sort by timestamp
                sorted_reactors.sort(key=lambda x: x[1])
                
                # Select winners (up to tasks_this_round)
                winners = [reactor for reactor, _ in sorted_reactors[:tasks_this_round]]
                
                logger.info(f"DEBUG: Selected {len(winners)} winners: {[w.name for w in winners]}")
                logger.info(f"DEBUG: Should assign tasks {starting_task_num} to {starting_task_num + len(winners) - 1}")
                
                # Process each winner
                winners_assigned = []
                logger.info(f"DEBUG: Processing {len(winners)} winners starting from task #{starting_task_num}")
                for i, winner in enumerate(winners):
                    # FIXED: Use the stored starting task number + index
                    current_task_num = starting_task_num + i
                    logger.info(f"DEBUG: Winner {i}: {winner.name} -> Task #{current_task_num}")
                    
                    # Convert User to Member for role assignment
                    winner_member = await self.get_member_safely(message.guild, winner.id) if message.guild else None
                    if not winner_member:
                        logger.error(f"User {winner.name} not found in guild")
                        await self.announce_channel.send(f"❌ Could not find {winner.mention} in server")
                        continue
                    
                    # Assign role
                    try:
                        if winner_member and message.guild and winner_member in message.guild.members:
                            role = await self.get_or_create_role(message.guild, TASK_ROLE_NAME)
                            
                            # Check if bot has permission to manage roles
                            bot_member = message.guild.me
                            if not bot_member.guild_permissions.manage_roles:
                                logger.error("Bot lacks manage_roles permission")
                                await self.announce_channel.send("❌ Bot needs 'Manage Roles' permission!")
                                continue
                                
                            # Check if bot's role is higher than target role
                            if bot_member.top_role.position <= role.position:
                                logger.error(f"Bot role too low to assign {role.name}")
                                await self.announce_channel.send(f"❌ Bot role must be higher than {role.name}!")
                                continue
                            
                            await winner_member.add_roles(role, reason=f"TaskBot: Task #{current_task_num}")
                            # Schedule role removal as background task
                            removal_task = asyncio.create_task(self.schedule_role_removal(winner_member, role))
                            # Store task reference to prevent garbage collection
                            if not hasattr(self, '_role_removal_tasks'):
                                self._role_removal_tasks = set()
                            self._role_removal_tasks.add(removal_task)
                            removal_task.add_done_callback(self._role_removal_tasks.discard)

                            logger.info(f"Successfully assigned {role.name} to {winner_member.name}")
                            
                            # Log role assignment
                            await self.send_log(f"TaskHolder role given to {winner_member.mention} for Task #{current_task_num}")
                            
                            # CRITICAL FIX: Explicitly pass the task number to DM method
                            logger.info(f"DEBUG: DMing {winner_member.name} with task #{current_task_num}")
                            await self.dm_winner(winner_member, current_task_num)
                            
                            # Update sheet with correct task number
                            await self.write_to_sheet(current_task_num, winner.name)
                            
                            # Add to successful assignments
                            winners_assigned.append((winner_member, current_task_num))
                            
                            # Confirm role assignment
                            if role in winner_member.roles:
                                logger.info(f"Role assignment confirmed for {winner_member.name}")
                            else:
                                logger.error(f"Role assignment failed for {winner_member.name}")
                                await self.announce_channel.send(f"❌ Failed to assign role to {winner_member.mention}")
                                
                        else:
                            logger.error(f"Could not get member object for user {winner.name}")
                            await self.announce_channel.send(f"❌ Could not assign role to {winner.mention}")
                            
                    except discord.Forbidden:
                        logger.error("Bot forbidden from managing roles")
                        await self.announce_channel.send("❌ Bot lacks permission to manage roles!")
                    except discord.HTTPException as e:
                        logger.error(f"HTTP error assigning role: {e}")
                        await self.announce_channel.send(f"❌ Error assigning role: {e}")
                    except Exception as e:
                        logger.error(f"Unexpected error assigning role: {e}")
                        await self.announce_channel.send(f"❌ Unexpected error: {e}")

                # CRITICAL FIX: Update current task counter AFTER all processing
                # For multiple winners per task, we only increment by 1 (one task assigned to multiple people)
                if winners_assigned:
                    self.current_task += 1

                # Create announcement with correct task numbers
                if winners_assigned:
                    if len(winners_assigned) == 1:
                        winner_member, task_num = winners_assigned[0]
                        embed = discord.Embed(
                            title="➡️ Task Assigned!",
                            description=f"Task #{task_num} goes to {winner_member.mention}!",
                            color=discord.Color.green()
                        )
                    else:
                        # Create detailed announcement for multiple winners
                        winner_list = []
                        for winner_member, task_num in winners_assigned:
                            winner_list.append(f"**Task #{task_num}:** {winner_member.mention}")
                        
                        embed = discord.Embed(
                            title="➡️ Tasks Assigned!",
                            description="\n".join(winner_list),
                            color=discord.Color.green()
                        )
                    
                    await self.announce_channel.send(embed=embed)
                else:
                    # No winners assigned successfully
                    embed = discord.Embed(
                        title="❌ Assignment Failed",
                        description="No tasks could be assigned due to errors. Retrying in next round.",
                        color=discord.Color.red()
                    )
                    await self.announce_channel.send(embed=embed)
                    
            else:
                # No reactors
                if tasks_this_round == 1:
                    description = f"No one claimed task #{starting_task_num}. Reposting in {self.interval_minutes} minutes…"
                    log_msg = f"No one claimed Task #{starting_task_num}. Reposting soon."
                else:
                    task_range = f"#{starting_task_num}-#{starting_task_num + tasks_this_round - 1}"
                    description = f"No one claimed tasks {task_range}. Reposting in {self.interval_minutes} minutes…"
                    log_msg = f"No one claimed Tasks {task_range}. Reposting soon."
                
                embed = discord.Embed(
                    title="⚠️ No Claims",
                    description=description,
                    color=discord.Color.orange()
                )
                await self.announce_channel.send(embed=embed)
                await self.send_log(log_msg)
                
            # Cleanup with safety check
            try:
                if message and message.id in self.reaction_timestamps:
                    del self.reaction_timestamps[message.id]
            except Exception as e:
                logger.error(f"Cleanup error: {e}")
                
        except Exception as e:
            logger.error(f"Task allocation error: {e}")


    async def send_log(self, message: str):
        """Send log message to logs channel"""
        if self.logs_channel:
            try:
                embed = discord.Embed(
                    title="📋 Task Bot Log",
                    description=message,
                    color=discord.Color.blue(),
                    timestamp=datetime.now()
                )
                await self.logs_channel.send(embed=embed)
            except Exception as e:
                logger.error(f"Failed to send log: {e}")


    async def get_reddit_karma(self, username: str) -> tuple[bool, str, int, int]:
        """Get Reddit user karma with proper session management"""
        try:
            if not all([REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET]):
                return False, "Reddit API not configured", 0, 0

            async with asyncpraw.Reddit(
                client_id=REDDIT_CLIENT_ID,
                client_secret=REDDIT_CLIENT_SECRET,
                user_agent=REDDIT_USER_AGENT
            )as reddit:
                user = await reddit.redditor(str(username))
                await user.load()
                link_karma = user.link_karma
                comment_karma = user.comment_karma
                return True, "Success", link_karma, comment_karma


        except Exception as e:
            if "404" in str(e) or "Not Found" in str(e):
                return False, "User not found", 0, 0
            return False, f"Error: {str(e)}", 0, 0
    
    def check_admin_permissions(self, member: discord.Member) -> bool:
        """Check if member has admin permissions or higher"""
        return member.guild_permissions.administrator or member.guild_permissions.manage_guild
            

            
    @task_allocation_loop.before_loop
    async def before_task_loop(self):
        """Wait for bot ready"""
        await self.wait_until_ready()

# Initialize bot
bot = TaskBot()

# Check admin permissions decorator
def admin_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return False
        member = interaction.guild.get_member(interaction.user.id)
        if not member:
            return False
        return bot.check_admin_permissions(member)
    return app_commands.check(predicate)

# Slash Commands
@bot.tree.command(name="configure_settings", description="Configure bot settings (Admin only)")
@app_commands.describe(
    interval_minutes="Time interval between tasks in minutes",
    announce_channel="Channel for task announcements",
    logs_channel="Channel for bot logs",
    reaction_time="Reaction time in seconds (1-60)",
    role_removal_hours="Hours after which TaskHolder role is removed (1-168)",
    ping_role_name="What role to ping when tasks are sent",
    sheets_url="Google Sheets URL (optional)"
)
@admin_only()
async def configure_settings(
    interaction: discord.Interaction,
    interval_minutes: int,
    announce_channel: discord.TextChannel,
    logs_channel: discord.TextChannel,
    reaction_time: int = 5,
    role_removal_hours: int = 12,
    ping_role_name: str = "✅・VERIFIED",
    sheets_url: Optional[str] = None
):
    await interaction.response.defer()
    
    try:
        # Validate inputs
        if interval_minutes < 1:
            await interaction.followup.send("❌ Interval must be at least 1 minute", ephemeral=True)
            return
            
        if reaction_time < 1 or reaction_time > 60:
            await interaction.followup.send("❌ Reaction time must be between 1-60 seconds", ephemeral=True)
            return
            
        if role_removal_hours < 1 or role_removal_hours > 168:
            await interaction.followup.send("❌ Role removal hours must be between 1-168 hours (1 week)", ephemeral=True)
            return
            
        if not interaction.guild:
            await interaction.followup.send("❌ Server only command", ephemeral=True)
            return
            
        # Check permissions on announce channel
        announce_perms = announce_channel.permissions_for(interaction.guild.me)
        if not all([announce_perms.send_messages, announce_perms.manage_messages, announce_perms.add_reactions]):
            await interaction.followup.send(f"❌ Missing bot permissions in {announce_channel.mention}", ephemeral=True)
            return
            
        # Check permissions on logs channel
        logs_perms = logs_channel.permissions_for(interaction.guild.me)
        if not logs_perms.send_messages:
            await interaction.followup.send(f"❌ Missing send messages permission in {logs_channel.mention}", ephemeral=True)
            return
            
        # Validate sheet URL if provided
        if sheets_url:
            if not bot.extract_sheet_id(sheets_url):
                await interaction.followup.send("❌ Invalid Google Sheets URL", ephemeral=True)
                return
                
            # Validate sheet access
            sheet_valid, message = await bot.validate_sheet_access(sheets_url)
            if not sheet_valid:
                await interaction.followup.send(f"❌ Sheet validation failed: {message}", ephemeral=True)
                return
            
        # Configure bot settings
        bot.interval_minutes = interval_minutes
        bot.announce_channel = announce_channel
        bot.logs_channel = logs_channel
        bot.reaction_time = reaction_time
        bot.role_removal_hours = role_removal_hours
        bot.ping_role_name = ping_role_name
        
        # Set sheet URL if provided
        if sheets_url:
            bot.sheet_url = sheets_url
        
        # Success message
        embed = discord.Embed(title="✅ Settings Configured!", color=discord.Color.green())
        embed.add_field(name="Interval", value=f"{interval_minutes} minutes", inline=True)
        embed.add_field(name="Reaction Time", value=f"{reaction_time} seconds", inline=True)
        embed.add_field(name="Role Removal", value=f"{role_removal_hours} hours", inline=True)
        embed.add_field(name="Announce Channel", value=announce_channel.mention, inline=True)
        embed.add_field(name="Logs Channel", value=logs_channel.mention, inline=True)
        embed.add_field(name="Ping Role", value=ping_role_name, inline=True)
        embed.add_field(name="Sheets URL", value="✅ Set" if sheets_url else "Not provided", inline=True)
        
        await interaction.followup.send(embed=embed)
        
        # Log configuration
        await bot.send_log(f"Bot settings configured by {interaction.user.mention}")
        
        logger.info(f"Settings configured: interval={interval_minutes}min, reaction_time={reaction_time}s, role_removal={role_removal_hours}h")
        
    except Exception as e:
        logger.error(f"Configure settings error: {e}")
        await interaction.followup.send(f"❌ Configuration failed: {e}", ephemeral=True)

@bot.tree.command(name="create_task", description="Create and start task allocation (Admin only)")
@app_commands.describe(
    tasks="Total number of tasks",
    task_type="Type of task (post, upvote, comment, poll vote)",
    winners_per_task="Number of winners per task round (default: 1)"
)
@app_commands.choices(task_type=[
    app_commands.Choice(name="Post", value=" Post"),
    app_commands.Choice(name="Upvote", value=" Upvote"),
    app_commands.Choice(name="Comment", value=" Comment"),
    app_commands.Choice(name="Poll Vote", value=" Poll vote")
])
@admin_only()
async def create_task(
    interaction: discord.Interaction,
    tasks: int,
    task_type: str,
    winners_per_task: int = 1
):
    await interaction.response.defer()
    
    try:
        # Check if settings are configured
        if not bot.announce_channel:
            await interaction.followup.send("❌ **Settings not configured!** Use `/configure_settings` first.", ephemeral=True)
            return
            
        # Check if sheet URL is set
        if not bot.sheet_url:
            await interaction.followup.send("❌ **Google Sheets URL not configured!** Use `/configure_settings` with `sheets_url` parameter.", ephemeral=True)
            return
            
        # Validate inputs
        if tasks <= 0:
            await interaction.followup.send("❌ Tasks must be > 0", ephemeral=True)
            return
            
        if winners_per_task <= 0 or winners_per_task > 20:
            await interaction.followup.send("❌ Winners per task must be between 1-20", ephemeral=True)
            return
            
        if not interaction.guild:
            await interaction.followup.send("❌ Server only command", ephemeral=True)
            return
            
        # Validate sheet access
        sheet_valid, message = await bot.validate_sheet_access(bot.sheet_url)
        if not sheet_valid:
            await interaction.followup.send(f"❌ Sheet validation failed: {message}", ephemeral=True)
            return
            
        # Configure task settings
        bot.total_tasks = tasks
        bot.current_task = 1
        bot.winners_per_task = winners_per_task
        bot.task_type = task_type
        bot.configured = True

        # Calculate remaining tasks for this round
        remaining_tasks = tasks
        tasks_this_round = min(winners_per_task, remaining_tasks)
        
        # Success message
        embed = discord.Embed(title="✅ Task Creation Started!", color=discord.Color.green())
        embed.add_field(name="Total Tasks", value=str(tasks), inline=True)
        embed.add_field(name="Task Type", value=task_type.title(), inline=True)
        embed.add_field(name="Winners Per Task", value=str(winners_per_task), inline=True)
        embed.add_field(name="Announce Channel", value=bot.announce_channel.mention, inline=True)
        embed.add_field(name="Logs Channel", value=bot.logs_channel.mention if bot.logs_channel else "Not set", inline=True)
        embed.add_field(name="Interval", value=f"{bot.interval_minutes} minutes", inline=True)
        embed.add_field(name="Sheet", value="✅ Verified", inline=True)
        
        await interaction.followup.send(embed=embed)
        
        # Log task creation
        if tasks_this_round == 1:
            await bot.send_log(f"Task #{bot.current_task} created and posted")
        else:
            await bot.send_log(f"Tasks #{bot.current_task}-#{bot.current_task + tasks_this_round - 1} created and posted ({tasks_this_round} people needed)")
        
        # Start task loop
        await bot.restart_task_loop()
            
        logger.info(f"Task creation started: {tasks} {task_type} tasks, {winners_per_task} winners per task by {interaction.user.name}")
        
    except Exception as e:
        logger.error(f"Create task error: {e}")
        await interaction.followup.send(f"❌ Task creation failed: {e}", ephemeral=True)

@bot.tree.command(name="test_bot", description="Test if bot is working")
async def test_bot(interaction: discord.Interaction):
    embed = discord.Embed(
        title="✅ Bot Status",
        description="Bot is online and slash commands are working!",
        color=discord.Color.green()
    )
    embed.add_field(name="Commands", value=len(bot.tree.get_commands()), inline=True)
    embed.add_field(name="Configured", value="✅" if bot.configured else "❌", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="stop_tasks", description="Stop current task execution (Admin only)")
@admin_only()
async def stop_tasks(interaction: discord.Interaction):
    await interaction.response.defer()
    
    try:
        if bot.task_allocation_loop is None or not bot.task_allocation_loop.is_running():
            await interaction.followup.send("❌ Task allocation is not currently running", ephemeral=True)
            return
            
        # Stop the task loop
        bot.task_allocation_loop.stop()
        bot.configured = False
        
        # Success message
        embed = discord.Embed(
            title="🛑 Task Execution Stopped!",
            description=f"Task allocation has been stopped at Task #{bot.current_task}",
            color=discord.Color.red()
        )
        embed.add_field(name="Current Task", value=str(bot.current_task), inline=True)
        embed.add_field(name="Total Tasks", value=str(bot.total_tasks), inline=True)
        embed.add_field(name="Stopped By", value=interaction.user.mention, inline=True)
        
        await interaction.followup.send(embed=embed)
        
        # Log task stop
        await bot.send_log(f"Task allocation stopped by {interaction.user.mention} at Task #{bot.current_task}")
        
        logger.info(f"Task allocation stopped by {interaction.user.name} at task {bot.current_task}")
        
    except Exception as e:
        logger.error(f"Stop tasks error: {e}")
        await interaction.followup.send(f"❌ Failed to stop tasks: {e}", ephemeral=True)

@bot.tree.command(name="bot_info", description="Show bot information")
async def bot_info(interaction: discord.Interaction):
    embed = discord.Embed(title="Bot Information", color=discord.Color.blue())
    embed.add_field(name="Current Task", value=str(bot.current_task), inline=True)
    embed.add_field(name="Total Tasks", value=str(bot.total_tasks), inline=True)
    embed.add_field(name="Interval", value=f"{bot.interval_minutes} minutes", inline=True)
    embed.add_field(name="Reaction Time", value=f"{bot.reaction_time} seconds", inline=True)
    embed.add_field(name="Role Removal", value=f"{bot.role_removal_hours} hours", inline=True)
    embed.add_field(name="Announce Channel", value=bot.announce_channel.mention if bot.announce_channel else "Not set", inline=True)
    embed.add_field(name="Logs Channel", value=bot.logs_channel.mention if bot.logs_channel else "Not set", inline=True)
    embed.add_field(name="Sheet Connected", value="✅" if bot.gc else "❌", inline=True)
    embed.add_field(name="Loop Running", value="✅" if bot.task_allocation_loop and bot.task_allocation_loop.is_running() else "❌", inline=True)
    embed.add_field(name="Winners Per Task", value=str(bot.winners_per_task), inline=True)
    embed.add_field(name="Task Type", value=bot.task_type.title(), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="reddit_verify", description="Verify Reddit account and get verified role")
@app_commands.describe(reddit_id="Reddit username (without u/)")
async def reddit_verify(interaction: discord.Interaction, reddit_id: str):
    await interaction.response.defer(ephemeral=True)
    
    try:
        # Check if command is used in a server
        if not interaction.guild:
            await interaction.followup.send("❌ This command can only be used in a server", ephemeral=True)
            return
            
        # Clean username (remove u/ if present)
        clean_username = reddit_id.replace("u/", "").replace("/u/", "").strip()
        
        if not clean_username:
            await interaction.followup.send("❌ Please provide a valid Reddit username", ephemeral=True)
            return
            
        # Get member object
        member = interaction.guild.get_member(interaction.user.id)
        if not member:
            await interaction.followup.send("❌ Could not find you in this server", ephemeral=True)
            return
            
        # Get karma
        success, message, link_karma, comment_karma = await bot.get_reddit_karma(clean_username)
        
        if success:
            total_karma = link_karma + comment_karma
            
            # Check if karma meets requirement (500+)
            if total_karma >= 500:
                # Get or create verified role
                try:
                    verified_role = discord.utils.get(interaction.guild.roles, name="✅・VERIFIED")
                    
                    if not verified_role:
                        # Create the role if it doesn't exist
                        verified_role = await interaction.guild.create_role(
                            name="✅・VERIFIED",
                            reason="Reddit verification role created by TaskBot"
                        )
                        logger.info(f"Created verified role in {interaction.guild.name}")
                    
                    # Check if user already has the role
                    if verified_role in member.roles:
                        await interaction.followup.send(f"✅ You already have the {verified_role.name} role!", ephemeral=True)
                        return
                    
                    # Check bot permissions
                    bot_member = interaction.guild.me
                    if not bot_member.guild_permissions.manage_roles:
                        await interaction.followup.send("❌ Bot lacks permission to manage roles", ephemeral=True)
                        return
                        
                    # Check if bot's role is higher than verified role
                    if bot_member.top_role.position <= verified_role.position:
                        await interaction.followup.send("❌ Bot role must be higher than verified role", ephemeral=True)
                        return

                    # Assign verified role
                    await member.add_roles(verified_role, reason=f"Reddit verification: u/{clean_username} ({total_karma:,} karma)")
                    
                    # Success message
                    embed = discord.Embed(
                        title="✅ Reddit Verification Successful!",
                        description=f"**Reddit:** u/{clean_username}\n**Total Karma:** {total_karma:,}\n**Role Assigned:** {verified_role.name}",
                        color=discord.Color.green()
                    )
                    embed.add_field(name="📥 Post Karma", value=f"{link_karma:,}", inline=True)
                    embed.add_field(name="💬 Comment Karma", value=f"{comment_karma:,}", inline=True)
                    embed.set_footer(text="Congratulations! You are now verified.")
                    
                    await interaction.followup.send(embed=embed, ephemeral=True)

                    # Log to verification channel
                    verification_channel_id = 1391836901186474044
                    verification_channel = bot.get_channel(verification_channel_id)
                    if verification_channel:
                        try:  
                            verification_message = (
                                f"**Discord:** {member.mention} (`{member.name}`) \n"
                                f"**Reddit:** [u/{clean_username}](https://www.reddit.com/user/{clean_username}/) \n"
                                f"**Total Karma:** {total_karma:,}\n"
                            )
                            
                            await verification_channel.send(verification_message) # type: ignore
                            logger.info(f"Sent verification log to channel {verification_channel_id}")
                        except Exception as log_error:
                            logger.error(f"Failed to send verification log: {log_error}")
                    else:
                        logger.warning(f"Verification channel {verification_channel_id} not found")
                    
                    # Log verification
                    await bot.send_log(f"✅ Reddit verification successful: {member.mention} verified as u/{clean_username} with {total_karma:,} karma")
                    
                    logger.info(f"Reddit verification successful: {member.name} verified as u/{clean_username} with {total_karma} karma")
                    
                except discord.Forbidden:
                    logger.error("Bot forbidden from managing roles")
                    await interaction.followup.send("❌ Bot lacks permission to manage roles", ephemeral=True)
                except discord.HTTPException as e:
                    logger.error(f"HTTP error assigning verified role: {e}")
                    await interaction.followup.send(f"❌ Error assigning role: {e}", ephemeral=True)
                except Exception as e:
                    logger.error(f"Unexpected error assigning verified role: {e}")
                    await interaction.followup.send(f"❌ Unexpected error: {e}", ephemeral=True)
                    
            else:
                # Insufficient karma
                await interaction.followup.send(
                    f"❌ Sorry, you do not meet our minimum Karma requirements.\n\n"
                    f"**Your Karma:** {total_karma:,}\n"
                    f"**Required:** 500+\n"
                    f"**Needed:** {500 - total_karma:,} more karma",
                    ephemeral=True
                )
                
                # Log failed verification
                await bot.send_log(f"❌ Reddit verification failed: {member.mention} (u/{clean_username}) has {total_karma:,} karma (need 500+)")
                
        else:
            # Error getting karma
            await interaction.followup.send(f"❌ Could not verify Reddit account: {message}", ephemeral=True)
            
    except Exception as e:
        logger.error(f"Reddit verify command error: {e}")
        await interaction.followup.send("❌ An error occurred while verifying your Reddit account", ephemeral=True)


# Error handler for admin
@configure_settings.error
@create_task.error
@stop_tasks.error
async def admin_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("❌ Admin permissions required", ephemeral=True)
    else:
        logger.error(f"Admin command error: {error}")
        await interaction.response.send_message("❌ Command failed", ephemeral=True)


# Run bot
if __name__ == "__main__":
    try:
        bot.run(DISCORD_TOKEN)
    except Exception as e:
        logger.error(f"Bot failed to start: {e}")
