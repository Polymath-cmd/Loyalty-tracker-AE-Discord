#!/usr/bin/env python3
"""
Loyalty Tracker Discord Bot
Track settlement tier-ups and loyalty recovery for optimal attack timing
"""

import discord
from discord import app_commands
from discord.ext import commands, tasks
import requests
import json
import os
from datetime import datetime, timezone
import sys

# Try to load from .env file if python-dotenv is installed
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv not installed, use environment variables directly

# Bot configuration - read from environment variable
BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')

API_URL = 'https://forest1.aetherkingdoms.com/api/public/mapExport.json'
DATA_FILE = 'loyalty_data.json'

# Loyalty mechanics
LOYALTY_MAX = {'village': 100, 'town': 200, 'city': 300}
RECOVERY_RATE = {'village': 0, 'town': 4, 'city': 6}

# Auto-check interval (minutes)
CHECK_INTERVAL = 10  # Check every 10 minutes

# Discord channel ID to send auto-alerts (loaded from data file or set manually)
ALERT_CHANNEL_ID = None

class LoyaltyTracker:
    def __init__(self):
        self.data = self.load_data()
    
    def load_data(self):
        print(f"[LOAD] Looking for data file: {DATA_FILE}")
        print(f"[LOAD] Current directory: {os.getcwd()}")
        print(f"[LOAD] File exists: {os.path.exists(DATA_FILE)}")
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    print(f"[LOAD] Loaded data - snapshot: {len(data.get('snapshot', {}))}, history: {len(data.get('history', {}))}")
                    return data
            except Exception as e:
                print(f"[LOAD] Error loading data: {e}")
                pass
        print("[LOAD] Returning empty data")
        return {'snapshot': {}, 'history': {}, 'last_check': None}
    
    def save_data(self):
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, indent=2)
        print(f"[SAVE] Data saved - snapshot: {len(self.data.get('snapshot', {}))}, history: {len(self.data.get('history', {}))}")
    
    def fetch_api(self):
        try:
            response = requests.get(API_URL, timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"API error: {e}")
            return None
    
    def extract_settlements(self, api_data):
        settlements = {}
        for tile in api_data['map']:
            if tile.get('tile_type') in ['village', 'town', 'city']:
                vid = tile.get('village_id')
                if vid:
                    vid_str = str(vid)  # Convert to string for consistent JSON keys
                    settlements[vid_str] = {
                        'name': tile.get('village_name', 'Unknown'),
                        'player': tile.get('username', 'Unknown'),
                        'type': tile.get('tile_type'),
                        'x': tile.get('x'),
                        'y': tile.get('y'),
                        'empire': tile.get('empire_tag', ''),
                        'population': tile.get('population', 0)
                    }
        return settlements
    
    def check_tier_ups(self):
        api_data = self.fetch_api()
        if not api_data:
            return None, "Failed to fetch API data"
        
        current = self.extract_settlements(api_data)
        previous = self.data.get('snapshot', {})
        history = self.data.get('history', {})
        now = datetime.now(timezone.utc)
        
        new_detections = []
        
        new_tier_ups = []
        new_conquests = []
        conquest_history = self.data.get('conquest_history', {})
        
        # Detect tier-ups and conquests
        for vid, curr in current.items():
            if vid in previous:
                prev = previous[vid]
                
                # Check for CONQUEST (same settlement, different owner)
                if prev['player'] != curr['player']:
                    rec_rates = {'village': 2, 'town': 4, 'city': 6}
                    max_loyalties = {'village': 100, 'town': 200, 'city': 300}
                    
                    conquest_history[vid] = {
                        **curr,
                        'previous_player': prev['player'],
                        'detected_at': now.isoformat(),
                        'base_loyalty': 0,
                        'max_loyalty': max_loyalties.get(curr['type'], 100),
                        'recovery_rate': rec_rates.get(curr['type'], 2),
                        'event_type': 'conquest'
                    }
                    new_conquests.append(conquest_history[vid])
                
                # Check for TIER-UP (same owner, type upgraded)
                elif prev['type'] == 'village' and curr['type'] == 'town':
                    history[vid] = {
                        **curr,
                        'from_type': 'village',
                        'detected_at': now.isoformat(),
                        'base_loyalty': 100,
                        'max_loyalty': 200,
                        'recovery_rate': 4,
                        'event_type': 'tier_up'
                    }
                    new_tier_ups.append(history[vid])
                    
                elif prev['type'] == 'town' and curr['type'] == 'city':
                    history[vid] = {
                        **curr,
                        'from_type': 'town',
                        'detected_at': now.isoformat(),
                        'base_loyalty': 200,
                        'max_loyalty': 300,
                        'recovery_rate': 6,
                        'event_type': 'tier_up'
                    }
                    new_tier_ups.append(history[vid])
        
        # Check for NEW SETTLEMENTS (settlement in current but NOT in previous)
        # Skip if no previous snapshot (first run) to avoid false positives
        settlements = []
        settlement_history = self.data.get('settlement_history', [])
        if previous:  # Only detect if we have a previous snapshot
            for vid, curr_data in current.items():
                if vid not in previous:
                    # Check if we already reported this settlement
                    already_reported = any(
                        s.get('village_id') == vid and 
                        s.get('x') == curr_data.get('x') and 
                        s.get('y') == curr_data.get('y')
                        for s in settlement_history
                    )
                    if not already_reported:
                        settlement_item = {
                            **curr_data,
                            'village_id': vid,
                            'detected_at': now.isoformat(),
                            'event_type': 'settlement'
                        }
                        settlements.append(settlement_item)
                        settlement_history.append(settlement_item)
            
            # Keep only last 100 settlements to prevent memory bloat
            self.data['settlement_history'] = settlement_history[-100:]
        
        # Check for DESTRUCTIONS (settlement in previous but NOT in current)
        # Skip if no previous snapshot (first run) to avoid false positives
        destructions = []
        destruction_history = self.data.get('destruction_history', [])
        if previous:  # Only detect if we have a previous snapshot
            for vid, prev_data in previous.items():
                if vid not in current:
                    # Check if we already reported this destruction
                    already_reported = any(
                        d.get('village_id') == vid and 
                        d.get('x') == prev_data.get('x') and 
                        d.get('y') == prev_data.get('y')
                        for d in destruction_history
                    )
                    if not already_reported:
                        destruction_item = {
                            **prev_data,
                            'village_id': vid,
                            'detected_at': now.isoformat(),
                            'event_type': 'destruction'
                        }
                        destructions.append(destruction_item)
                        destruction_history.append(destruction_item)
            
            # Keep only last 100 destructions to prevent memory bloat
            self.data['destruction_history'] = destruction_history[-100:]
        
        # Update snapshot
        self.data['snapshot'] = current
        self.data['last_check'] = now.isoformat()
        
        # Clean up tier-up history (remove maxed out)
        to_remove = []
        for vid, data in history.items():
            detected = datetime.fromisoformat(data['detected_at'])
            hours = (now - detected).total_seconds() / 3600
            current_loyalty = data['base_loyalty'] + (data['recovery_rate'] * hours)
            if current_loyalty >= data['max_loyalty']:
                to_remove.append(vid)
        
        for vid in to_remove:
            del history[vid]
        
        # Clean up conquest history (remove maxed out)
        to_remove_cq = []
        for vid, data in conquest_history.items():
            detected = datetime.fromisoformat(data['detected_at'])
            hours = (now - detected).total_seconds() / 3600
            current_loyalty = data['base_loyalty'] + (data['recovery_rate'] * hours)
            if current_loyalty >= data['max_loyalty']:
                to_remove_cq.append(vid)
        
        for vid in to_remove_cq:
            del conquest_history[vid]
        
        self.data['history'] = history
        self.data['conquest_history'] = conquest_history
        self.save_data()
        
        return {'tier_ups': new_tier_ups, 'conquests': new_conquests, 'settlements': settlements, 'destructions': destructions}, None
    
    def get_targets(self):
        """Get all current targets with loyalty info (tier-ups and conquests)"""
        now = datetime.now(timezone.utc)
        history = self.data.get('history', {})
        conquest_history = self.data.get('conquest_history', {})
        targets = []
        
        # Process tier-ups
        for vid, data in history.items():
            detected = datetime.fromisoformat(data['detected_at'])
            hours = (now - detected).total_seconds() / 3600
            current = data['base_loyalty'] + (data['recovery_rate'] * hours)
            max_loyalty = data['max_loyalty']
            
            if current < max_loyalty:
                targets.append({
                    'name': data['name'],
                    'player': data['player'],
                    'empire': data.get('empire', ''),
                    'type': data['type'],
                    'x': data['x'],
                    'y': data['y'],
                    'population': data.get('population', 'Unknown'),
                    'loyalty': round(current, 1),
                    'max': max_loyalty,
                    'hours_to_max': round((max_loyalty - current) / data['recovery_rate'], 1),
                    'recovery_rate': data['recovery_rate'],
                    'event_type': 'tier_up'
                })
        
        # Process conquests
        for vid, data in conquest_history.items():
            detected = datetime.fromisoformat(data['detected_at'])
            hours = (now - detected).total_seconds() / 3600
            current = data['base_loyalty'] + (data['recovery_rate'] * hours)
            max_loyalty = data['max_loyalty']
            
            if current < max_loyalty:
                targets.append({
                    'name': data['name'],
                    'player': data['player'],
                    'previous_player': data.get('previous_player', 'Unknown'),
                    'empire': data.get('empire', ''),
                    'type': data['type'],
                    'x': data['x'],
                    'y': data['y'],
                    'population': data.get('population', 'Unknown'),
                    'loyalty': round(current, 1),
                    'max': max_loyalty,
                    'hours_to_max': round((max_loyalty - current) / data['recovery_rate'], 1),
                    'recovery_rate': data['recovery_rate'],
                    'event_type': 'conquest'
                })
        
        targets.sort(key=lambda x: x['loyalty'])
        return targets

# Initialize tracker
tracker = LoyaltyTracker()

# Discord bot setup
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

@client.event
async def on_ready():
    await tree.sync()
    print(f'Logged in as {client.user}')
    
    # Load saved alert channel from data file
    global ALERT_CHANNEL_ID
    saved_channel = tracker.data.get('alert_channel_id')
    if saved_channel:
        ALERT_CHANNEL_ID = saved_channel
        print(f'Loaded alert channel: {ALERT_CHANNEL_ID}')
    
    # Start auto-check task
    auto_check.start()

@tasks.loop(minutes=CHECK_INTERVAL)
async def auto_check():
    """Auto-check for events every 10 minutes"""
    print(f"[AUTO-CHECK] Running at {datetime.now(timezone.utc).isoformat()}")
    
    # Check if we have a previous snapshot (skip alerts on first run)
    had_previous = bool(tracker.data.get('snapshot'))
    print(f"[AUTO-CHECK] Had previous snapshot: {had_previous}")
    
    result, error = tracker.check_tier_ups()
    if error:
        print(f"Auto-check error: {error}")
        return
    
    tier_ups = result.get('tier_ups', [])
    conquests = result.get('conquests', [])
    settlements = result.get('settlements', [])
    destructions = result.get('destructions', [])
    total = len(tier_ups) + len(conquests) + len(settlements) + len(destructions)
    
    # Skip alerting if this was the first run (no previous snapshot)
    if not had_previous:
        print("[INFO] First run - established baseline, no alerts sent")
        return
    
    print(f"[AUTO-CHECK] Total events found: {total}")
    print(f"[AUTO-CHECK]   - Tier-ups: {len(tier_ups)}")
    print(f"[AUTO-CHECK]   - Conquests: {len(conquests)}")
    print(f"[AUTO-CHECK]   - Settlements: {len(settlements)}")
    print(f"[AUTO-CHECK]   - Destructions: {len(destructions)}")
    
    if total > 0:
        print(f"Auto-check found {len(tier_ups)} tier-up(s), {len(conquests)} conquest(s), {len(settlements)} settlement(s), {len(destructions)} destruction(s)")
        
        # Send Discord notification if channel is configured
        if ALERT_CHANNEL_ID:
            channel = client.get_channel(ALERT_CHANNEL_ID)
            if channel:
                # Build embed title
                parts = []
                if tier_ups:
                    parts.append(f"{len(tier_ups)} Tier-up(s)")
                if conquests:
                    parts.append(f"{len(conquests)} Conquest(s)")
                if settlements:
                    parts.append(f"{len(settlements)} Settlement(s)")
                if destructions:
                    parts.append(f"{len(destructions)} Destruction(s)")
                title = "🚨 " + " + ".join(parts) + "!"
                
                # Dynamic description based on event types
                actionable = tier_ups or conquests
                if actionable:
                    description = "New targets detected!"
                else:
                    description = "New events detected:"
                
                embed = discord.Embed(
                    title=title,
                    description=description,
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc)
                )
                
                # Add tier-ups
                for i, t in enumerate(tier_ups[:3], 1):
                    from_to = f"{t['from_type'].capitalize()} -> {t['type'].capitalize()}"
                    empire_tag = f"[{t['empire']}] " if t.get('empire') else ""
                    pop = t.get('population', 'Unknown')
                    
                    embed.add_field(
                        name=f"📈 {empire_tag}{t['player']} - {t['name']}",
                        value=(
                            f"**Type:** {from_to}\n"
                            f"**Coords:** ({t['x']}, {t['y']})\n"
                            f"**Population:** {pop}\n"
                            f"**Loyalty:** {t['base_loyalty']}/{t['max_loyalty']} (starting)\n"
                            f"**Recovery:** +{t['recovery_rate']}/hour"
                        ),
                        inline=False
                    )
                
                # Add conquests
                for i, t in enumerate(conquests[:3], 1):
                    empire_tag = f"[{t['empire']}] " if t.get('empire') else ""
                    prev = t.get('previous_player', 'Unknown')
                    pop = t.get('population', 'Unknown')
                    
                    embed.add_field(
                        name=f"⚔️ {empire_tag}{t['player']} - {t['name']}",
                        value=(
                            f"**Type:** {t['type'].capitalize()} (Conquest)\n"
                            f"**Coords:** ({t['x']}, {t['y']})\n"
                            f"**Population:** {pop}\n"
                            f"**From:** {prev}\n"
                            f"**Loyalty:** {t['base_loyalty']}/{t['max_loyalty']} (starting)\n"
                            f"**Recovery:** +{t['recovery_rate']}/hour"
                        ),
                        inline=False
                    )
                
                # Add settlements
                if settlements:
                    settlement_text = ""
                    for i, s in enumerate(settlements[:5], 1):
                        empire_tag = f"[{s['empire']}] " if s.get('empire') else ""
                        settlement_text += f"{i}. {empire_tag}{s['player']} - {s['name']}\n    ({s['x']}, {s['y']})\n"
                    
                    embed.add_field(
                        name=f"🏠 {len(settlements)} New Settlement(s)",
                        value=settlement_text,
                        inline=False
                    )
                
                # Add destructions
                if destructions:
                    destruction_text = ""
                    for i, d in enumerate(destructions[:5], 1):
                        empire_tag = f"[{d['empire']}] " if d.get('empire') else ""
                        destruction_text += f"{i}. {empire_tag}{d['player']} - {d['name']}\n    ({d['x']}, {d['y']}) - {d['type'].capitalize()}\n"
                    
                    embed.add_field(
                        name=f"💀 {len(destructions)} Settlement(s) Destroyed",
                        value=destruction_text,
                        inline=False
                    )
                
                try:
                    await channel.send(embed=embed)
                    print(f"Sent Discord alert to channel {ALERT_CHANNEL_ID}")
                except Exception as e:
                    print(f"Failed to send Discord alert: {e}")

@tree.command(name="loyaltyhelp", description="Show help for Loyalty Tracker")
async def loyaltyhelp(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 Loyalty Tracker Help",
        description="Track settlement tier-ups for optimal attack timing!",
        color=discord.Color.blue()
    )
    
    embed.add_field(
        name="🎯 Purpose",
        value=(
            "Track settlement events to find easy nobling targets! "
            "New tier-ups and conquests start with low loyalty and recover over time."
        ),
        inline=False
    )
    
    embed.add_field(
        name="📊 Tracked Events",
        value=(
            "📈 **Tier-ups:** Village→Town (100/200), Town→City (200/300)\n"
            "⚔️ **Conquests:** Settlements change owner (0 loyalty)\n"
            "🏠 **Settlements:** New villages founded\n"
            "💀 **Destructions:** Settlements destroyed"
        ),
        inline=False
    )
    
    embed.add_field(
        name="📈 Loyalty Recovery",
        value=(
            "**Tier-ups:**\n"
            "  Village→Town: 100→200, +4/hour\n"
            "  Town→City: 200→300, +6/hour\n"
            "**Conquests:**\n"
            "  Village: 0→100, +2/hour\n"
            "  Town: 0→200, +4/hour\n"
            "  City: 0→300, +6/hour\n"
            "Lower loyalty = fewer nobles needed!"
        ),
        inline=False
    )
    
    embed.add_field(
        name="🔍 Commands",
        value=(
            "`/check` - Check for all new events now\n"
            "`/targets` - Show tracked targets with current loyalty\n"
            "`/setalert #channel` - Set alert channel\n"
            "`/status` - Bot status"
        ),
        inline=False
    )
    
    embed.set_footer(text=f"Auto-checks every {CHECK_INTERVAL} minutes")
    await interaction.response.send_message(embed=embed)

@tree.command(name="check", description="Check for new tier-ups, conquests, and destructions now")
async def check(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    
    result, error = tracker.check_tier_ups()
    
    if error:
        await interaction.followup.send(f"❌ Error: {error}")
        return
    
    tier_ups = result.get('tier_ups', [])
    conquests = result.get('conquests', [])
    settlements = result.get('settlements', [])
    destructions = result.get('destructions', [])
    total = len(tier_ups) + len(conquests) + len(settlements) + len(destructions)
    
    if total == 0:
        await interaction.followup.send("✅ No new events detected.")
        return
    
    # Build title
    parts = []
    if tier_ups:
        parts.append(f"{len(tier_ups)} Tier-up(s)")
    if conquests:
        parts.append(f"{len(conquests)} Conquest(s)")
    if settlements:
        parts.append(f"{len(settlements)} Settlement(s)")
    if destructions:
        parts.append(f"{len(destructions)} Destruction(s)")
    title = "🚨 " + " + ".join(parts) + "!"
    
    embed = discord.Embed(
        title=title,
        description="New events detected!",
        color=discord.Color.red()
    )
    
    # Add tier-ups
    for i, t in enumerate(tier_ups[:10], 1):
        from_to = f"{t['from_type'].capitalize()} -> {t['type'].capitalize()}"
        empire_tag = f"[{t['empire']}] " if t.get('empire') else ""
        pop = t.get('population', 'Unknown')
        
        embed.add_field(
            name=f"📈 {empire_tag}{t['player']} - {t['name']}",
            value=(
                f"**Type:** {from_to}\n"
                f"**Coords:** ({t['x']}, {t['y']})\n"
                f"**Population:** {pop}\n"
                f"**Loyalty:** {t['base_loyalty']}/{t['max_loyalty']} (starting)\n"
                f"**Recovery:** +{t['recovery_rate']}/hour"
            ),
            inline=False
        )
    
    # Add conquests
    for i, t in enumerate(conquests[:10], 1):
        empire_tag = f"[{t['empire']}] " if t.get('empire') else ""
        prev = t.get('previous_player', 'Unknown')
        pop = t.get('population', 'Unknown')
        
        embed.add_field(
            name=f"⚔️ {empire_tag}{t['player']} - {t['name']}",
            value=(
                f"**Type:** {t['type'].capitalize()} (Conquest)\n"
                f"**Coords:** ({t['x']}, {t['y']})\n"
                f"**Population:** {pop}\n"
                f"**From:** {prev}\n"
                f"**Loyalty:** {t['base_loyalty']}/{t['max_loyalty']} (starting)\n"
                f"**Recovery:** +{t['recovery_rate']}/hour"
            ),
            inline=False
        )
    
    # Add settlements
    if settlements:
        settlement_text = ""
        for i, s in enumerate(settlements[:10], 1):
            empire_tag = f"[{s['empire']}] " if s.get('empire') else ""
            settlement_text += f"{i}. {empire_tag}{s['player']} - {s['name']}\n    ({s['x']}, {s['y']})\n"
        
        embed.add_field(
            name=f"🏠 {len(settlements)} New Settlement(s)",
            value=settlement_text,
            inline=False
        )
    
    # Add destructions
    if destructions:
        destruction_text = ""
        for i, d in enumerate(destructions[:10], 1):
            empire_tag = f"[{d['empire']}] " if d.get('empire') else ""
            destruction_text += f"{i}. {empire_tag}{d['player']} - {d['name']}\n    ({d['x']}, {d['y']}) - {d['type'].capitalize()}\n"
        
        embed.add_field(
            name=f"💀 {len(destructions)} Settlement(s) Destroyed",
            value=destruction_text,
            inline=False
        )
    
    await interaction.followup.send(embed=embed)

@tree.command(name="targets", description="Show tracked attack targets (optionally filter out your empire)")
@app_commands.describe(empire="Your empire tag to exclude from targets (exact match). Settlements without empire are always shown.")
async def targets(interaction: discord.Interaction, empire: str = None):
    await interaction.response.defer(thinking=True)
    
    target_list = tracker.get_targets()
    
    # Filter out settlements matching the specified empire
    # Keep settlements without empire (they are always targets)
    if empire:
        empire_filter = empire.strip().upper()
        target_list = [t for t in target_list if not t.get('empire') or t.get('empire', '').upper() != empire_filter]
    
    if not target_list:
        await interaction.followup.send("📭 No active targets.")
        return
    
    embed = discord.Embed(
        title=f"🎯 {len(target_list)} Active Target(s)",
        color=discord.Color.orange()
    )
    
    for i, t in enumerate(target_list[:15], 1):
        empire_tag = f"[{t['empire']}] " if t.get('empire') else ""
        pop = t.get('population', 'Unknown')
        
        # Check if it's a conquest
        if t.get('event_type') == 'conquest':
            prev = t.get('previous_player', 'Unknown')
            name = f"⚔️ {empire_tag}{t['player']} - {t['name']}"
            value_text = (
                f"**Coords:** ({t['x']}, {t['y']})\n"
                f"**Type:** {t['type'].upper()} (from {prev})\n"
                f"**Population:** {pop}\n"
                f"**Loyalty:** {t['loyalty']}/{t['max']}\n"
                f"**Recovery:** +{t['recovery_rate']}/hour"
            )
        else:
            name = f"📈 {empire_tag}{t['player']} - {t['name']}"
            value_text = (
                f"**Coords:** ({t['x']}, {t['y']})\n"
                f"**Type:** {t['type'].upper()}\n"
                f"**Population:** {pop}\n"
                f"**Loyalty:** {t['loyalty']}/{t['max']}\n"
                f"**Recovery:** +{t['recovery_rate']}/hour"
            )
        
        embed.add_field(name=name, value=value_text, inline=False)
    
    await interaction.followup.send(embed=embed)

@tree.command(name="setalert", description="Set the channel for auto-check alerts")
@app_commands.describe(channel="The channel to send auto-alerts to")
async def setalert(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    
    global ALERT_CHANNEL_ID
    
    ALERT_CHANNEL_ID = channel.id
    # Save to data file so it persists after restart
    tracker.data['alert_channel_id'] = channel.id
    tracker.save_data()
    
    await interaction.followup.send(f"✅ Auto-alerts will be sent to {channel.mention}", ephemeral=True)

@tree.command(name="status", description="Show bot status")
async def status(interaction: discord.Interaction):
    last_check = tracker.data.get('last_check', 'Never')
    history_count = len(tracker.data.get('history', {}))
    
    embed = discord.Embed(
        title="📊 Loyalty Tracker Status",
        color=discord.Color.green()
    )
    
    embed.add_field(name="Last Check", value=last_check, inline=True)
    embed.add_field(name="Active Targets", value=str(history_count), inline=True)
    embed.add_field(name="Auto-check Interval", value=f"{CHECK_INTERVAL} min", inline=True)
    
    alert_status = f"<#{ALERT_CHANNEL_ID}>" if ALERT_CHANNEL_ID else "Not set (use /setalert)"
    embed.add_field(name="Alert Channel", value=alert_status, inline=True)
    
    await interaction.response.send_message(embed=embed)

if __name__ == "__main__":
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("ERROR: Bot token not configured!")
        print("\nOptions:")
        print("1. Create a .env file with: DISCORD_BOT_TOKEN=your_token_here")
        print("2. Or set environment variable: DISCORD_BOT_TOKEN=your_token_here")
        print("3. Or edit loyalty_discord_bot.py and replace YOUR_BOT_TOKEN_HERE")
        print("\nGet your token from: https://discord.com/developers/applications/")
        sys.exit(1)
    
    print(f"Using token: {BOT_TOKEN[:20]}... (length: {len(BOT_TOKEN)})")
    
    client.run(BOT_TOKEN)
