import os
import asyncio
import json
import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timezone, timedelta
from flask import Flask, request, session, redirect, render_template_string, jsonify
from threading import Thread

# ---------------------------------------------------------------
# 1. CONFIGURATION
# ---------------------------------------------------------------
TOKEN = os.environ["TOKEN"]
GUILD_ID = 1480064632310857769
ADMIN_ID = 1001845425185230990
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "homies2024")
IST = timezone(timedelta(hours=5, minutes=30))
DATA_FILE = "data.json"


# ---------------------------------------------------------------
# 2. DATA PERSISTENCE
# ---------------------------------------------------------------
def load_data():
    defaults = {}
    for path in [DATA_FILE, DATA_FILE + ".bak"]:
        try:
            with open(path, "r") as f:
                data = json.load(f)
            lb = data.get("leaderboard", defaults)
            raw_ap = data.get("active_predictions", {})
            ap = {
                label: {int(uid): pred for uid, pred in preds.items()}
                for label, preds in raw_ap.items()
            }
            if path == DATA_FILE + ".bak":
                print("⚠️  Recovered data from backup file.")
            return lb, ap
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    print("⚠️  No data file found — starting with defaults.")
    return defaults, {}


def save_data():
    data = {
        "leaderboard": leaderboard,
        "active_predictions": {
            label: {str(uid): pred for uid, pred in preds.items()}
            for label, preds in active_predictions.items()
        },
    }
    # Atomic write: write to temp file first, then rename — prevents corruption
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, DATA_FILE)
    # Keep a rolling backup
    import shutil

    shutil.copy2(DATA_FILE, DATA_FILE + ".bak")


leaderboard, active_predictions = load_data()
POINTS_PER_HIT = 10
scheduled_reveals = {}


# ---------------------------------------------------------------
# NAME MATCHING HELPER
# ---------------------------------------------------------------
def find_leaderboard_key(display_name):
    name = display_name.upper()
    name_words = set(name.split())
    for key in leaderboard:
        key_words = set(key.split())
        # Match if any word overlaps between leaderboard key and display name
        if key_words & name_words:
            return key
        # Also match if one is a substring of the other
        if key in name or name in key:
            return key
    return name  # fallback: use display name as-is


# ---------------------------------------------------------------
# 3. BOT MANAGEMENT
# ---------------------------------------------------------------
_bot = None
_bot_loop = None
_bot_thread = None
_bot_running = False


class IPLBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        pass


def setup_commands(b):
    @b.event
    async def on_ready():
        global _bot_running
        _bot_running = True
        guild = discord.Object(id=GUILD_ID)
        b.tree.copy_global_to(guild=guild)
        synced = await b.tree.sync(guild=guild)
        print(f"✅ {b.user.name} is online! Synced {len(synced)} commands.")
        if not auto_reveal.is_running():
            auto_reveal.start()

    @b.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        print(
            f"❌ Command error in /{interaction.command.name if interaction.command else 'unknown'}: {error}"
        )
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    f"❌ Something went wrong: `{error}`", ephemeral=True
                )
            else:
                await interaction.followup.send(
                    f"❌ Something went wrong: `{error}`", ephemeral=True
                )
        except Exception as e:
            print(f"Failed to send error message: {e}")

    @tasks.loop(seconds=60)
    async def auto_reveal():
        now_ist = datetime.now(IST)
        current_time = now_ist.strftime("%H:%M")
        to_remove = []
        for label, info in scheduled_reveals.items():
            if info["time"] == current_time:
                channel = b.get_channel(info["channel_id"])
                if channel:
                    if label in active_predictions and active_predictions[label]:
                        embed = discord.Embed(
                            title=f"⏰ Auto-Reveal — {label}",
                            description="Prediction window closed! Here's what everyone picked:",
                            color=discord.Color.orange(),
                        )
                        for pid, data in active_predictions[label].items():
                            val = (
                                f"**Toss:** {data['toss']} | **Win:** {data['match']}\n"
                                f"**Bat:** {data['batter']} | **Bowl:** {data['bowler']}\n"
                                f"**MOTM:** {data['motm']}"
                            )
                            embed.add_field(name=data["user"], value=val, inline=False)
                        await channel.send(embed=embed)
                    else:
                        await channel.send(
                            f"⏰ Prediction window for **{label}** closed — no picks submitted."
                        )
                to_remove.append(label)
        for label in to_remove:
            del scheduled_reveals[label]

    @b.tree.command(
        name="predict",
        description="Lock in your picks (use match_number: 1 or 2 if two matches today)",
    )
    async def predict(
        interaction: discord.Interaction,
        match_number: str,
        toss_winner: str,
        match_winner: str,
        best_batter: str,
        best_bowler: str,
        motm: str,
    ):
        label = f"MATCH {match_number.strip()}"
        user_name = interaction.user.display_name.upper()
        if label not in active_predictions:
            active_predictions[label] = {}
        if interaction.user.id in active_predictions[label]:
            return await interaction.response.send_message(
                f"❌ {user_name}, you already submitted picks for **{label}**! Predictions cannot be changed once locked.",
                ephemeral=True,
            )
        active_predictions[label][interaction.user.id] = {
            "user": user_name,
            "toss": toss_winner.upper(),
            "match": match_winner.upper(),
            "batter": best_batter.title(),
            "bowler": best_bowler.title(),
            "motm": motm.title(),
        }
        save_data()
        await interaction.response.send_message(
            f"🔒 {user_name}, your picks for **{label}** are LOCKED!", ephemeral=True
        )

    @b.tree.command(
        name="reveal",
        description="Show everyone's predictions (use match_number: 1 or 2)",
    )
    async def reveal(interaction: discord.Interaction, match_number: str):
        await interaction.response.defer()
        label = f"MATCH {match_number.strip()}"
        if label not in active_predictions or not active_predictions[label]:
            return await interaction.followup.send(
                f"No picks submitted for **{label}** yet."
            )
        embed = discord.Embed(
            title=f"🏏 Predictions — {label}", color=discord.Color.blue()
        )
        for pid, data in active_predictions[label].items():
            val = (
                f"**Toss:** {data['toss']} | **Win:** {data['match']}\n"
                f"**Bat:** {data['batter']} | **Bowl:** {data['bowler']}\n"
                f"**MOTM:** {data['motm']}"
            )
            embed.add_field(name=data["user"], value=val, inline=False)
        await interaction.followup.send(embed=embed)

    @b.tree.command(
        name="settle_match",
        description="Admin: Enter results and award points (use match_number: 1 or 2)",
    )
    async def settle(
        interaction: discord.Interaction,
        match_number: str,
        toss: str,
        match: str,
        batter: str,
        bowler: str,
        motm: str,
    ):
        await interaction.response.defer()
        label = f"MATCH {match_number.strip()}"
        if label not in active_predictions or not active_predictions[label]:
            return await interaction.followup.send(
                f"No predictions found for **{label}**."
            )
        res = {
            "toss": toss.upper(),
            "match": match.upper(),
            "batter": batter.title(),
            "bowler": bowler.title(),
            "motm": motm.title(),
        }
        summary = f"📊 **RESULTS — {label}**\n"
        for uid, pred in active_predictions[label].items():
            name = pred["user"]
            key = find_leaderboard_key(name)
            if key not in leaderboard:
                leaderboard[key] = 0
            pts = 0
            if pred["toss"] == res["toss"]:
                pts += POINTS_PER_HIT
            if pred["match"] == res["match"]:
                pts += POINTS_PER_HIT
            if pred["batter"] == res["batter"]:
                pts += POINTS_PER_HIT
            if pred["bowler"] == res["bowler"]:
                pts += POINTS_PER_HIT
            if pred["motm"] == res["motm"]:
                pts += POINTS_PER_HIT
            leaderboard[key] += pts
            summary += f"• **{key}**: +{pts} (Total: {leaderboard[key]})\n"
        del active_predictions[label]
        save_data()
        await interaction.followup.send(summary)

    @b.tree.command(
        name="set_reveal_time",
        description="Schedule auto-reveal of predictions at a time in IST",
    )
    async def set_reveal_time(
        interaction: discord.Interaction, match_number: str, time_ist: str
    ):
        if interaction.user.id != ADMIN_ID:
            return await interaction.response.send_message(
                "❌ You don't have permission.", ephemeral=True
            )
        label = f"MATCH {match_number.strip()}"
        try:
            parts = time_ist.strip().split(":")
            hour, minute = int(parts[0]), int(parts[1])
            assert 0 <= hour <= 23 and 0 <= minute <= 59
            time_formatted = f"{hour:02d}:{minute:02d}"
        except Exception:
            return await interaction.response.send_message(
                "❌ Invalid time. Use **HH:MM** in 24hr IST (e.g. `19:30` for 7:30 PM).",
                ephemeral=True,
            )
        scheduled_reveals[label] = {
            "time": time_formatted,
            "channel_id": interaction.channel_id,
        }
        await interaction.response.send_message(
            f"⏰ **{label}** predictions will auto-reveal at **{time_formatted} IST** in this channel.",
            ephemeral=True,
        )

    @b.tree.command(
        name="set_points", description="Admin only: Manually set a player's points"
    )
    async def set_points(interaction: discord.Interaction, player: str, points: int):
        if interaction.user.id != ADMIN_ID:
            return await interaction.response.send_message(
                "❌ You don't have permission.", ephemeral=True
            )
        key = player.upper()
        leaderboard[key] = points
        save_data()
        await interaction.response.send_message(
            f"✅ **{key}**'s points set to `{points}`.", ephemeral=True
        )

    @b.tree.command(
        name="rename_player",
        description="Admin: Rename a leaderboard entry to match someone's Discord username",
    )
    async def rename_player(
        interaction: discord.Interaction, old_name: str, new_name: str
    ):
        if interaction.user.id != ADMIN_ID:
            return await interaction.response.send_message(
                "❌ You don't have permission.", ephemeral=True
            )
        old_key = old_name.upper()
        new_key = new_name.upper()
        if old_key not in leaderboard:
            return await interaction.response.send_message(
                f"❌ **{old_key}** not found. Current names: {', '.join(leaderboard.keys())}",
                ephemeral=True,
            )
        if new_key in leaderboard:
            return await interaction.response.send_message(
                f"❌ **{new_key}** already exists in the leaderboard.", ephemeral=True
            )
        leaderboard[new_key] = leaderboard.pop(old_key)
        save_data()
        await interaction.response.send_message(
            f"✅ Renamed **{old_key}** → **{new_key}** (points carried over: `{leaderboard[new_key]}`)",
            ephemeral=True,
        )

    @b.tree.command(
        name="remove_player", description="Admin: Remove a player from the leaderboard"
    )
    async def remove_player(interaction: discord.Interaction, player: str):
        if interaction.user.id != ADMIN_ID:
            return await interaction.response.send_message(
                "❌ You don't have permission.", ephemeral=True
            )
        key = player.upper()
        if key not in leaderboard:
            return await interaction.response.send_message(
                f"❌ **{key}** not found. Current players: {', '.join(leaderboard.keys())}",
                ephemeral=True,
            )
        pts = leaderboard.pop(key)
        save_data()
        await interaction.response.send_message(
            f"🗑️ Removed **{key}** from the leaderboard (had `{pts} pts`).",
            ephemeral=True,
        )

    @b.tree.command(
        name="remove_all_players",
        description="Admin: Remove every player from the leaderboard entirely",
    )
    async def remove_all_players(interaction: discord.Interaction, confirm: str):
        if interaction.user.id != ADMIN_ID:
            return await interaction.response.send_message(
                "❌ You don't have permission.", ephemeral=True
            )
        if confirm.upper() != "YES":
            return await interaction.response.send_message(
                "⚠️ To confirm, run the command with `confirm: YES`", ephemeral=True
            )
        leaderboard.clear()
        active_predictions.clear()
        save_data()
        await interaction.response.send_message(
            "🗑️ **All players removed.** Add new ones with `/set_points`."
        )

    @b.tree.command(
        name="missing_predictions",
        description="See who hasn't submitted picks yet for a match",
    )
    async def missing_predictions(interaction: discord.Interaction, match_number: str):
        await interaction.response.defer(ephemeral=True)
        label = f"MATCH {match_number.strip()}"
        submitted_names = [
            pred["user"].upper() for pred in active_predictions.get(label, {}).values()
        ]
        missing_names = [
            name for name in leaderboard.keys() if name.upper() not in submitted_names
        ]
        if not missing_names:
            return await interaction.followup.send(
                f"✅ Everyone has submitted picks for **{label}**!", ephemeral=True
            )
        await interaction.followup.send(
            f"⏳ **{len(missing_names)} player(s)** haven't submitted for **{label}** yet:\n"
            + "\n".join(f"• {n}" for n in missing_names),
            ephemeral=True,
        )

    @b.tree.command(
        name="count_predictions",
        description="See how many players have submitted picks for a match",
    )
    async def count_predictions(interaction: discord.Interaction, match_number: str):
        await interaction.response.defer(ephemeral=True)
        label = f"MATCH {match_number.strip()}"
        count = len(active_predictions.get(label, {}))
        names = [pred["user"] for pred in active_predictions.get(label, {}).values()]
        if count == 0:
            return await interaction.followup.send(
                f"📭 No picks submitted for **{label}** yet.", ephemeral=True
            )
        await interaction.followup.send(
            f"📬 **{count} player(s)** have submitted picks for **{label}**:\n"
            + "\n".join(f"• {n}" for n in names),
            ephemeral=True,
        )

    @b.tree.command(
        name="my_predictions",
        description="See your own picks — or check someone else's (only visible to you)",
    )
    async def my_predictions(
        interaction: discord.Interaction,
        match_number: str,
        member: discord.Member = None,
    ):
        target = member if member else interaction.user
        label = f"MATCH {match_number.strip()}"
        if (
            label not in active_predictions
            or target.id not in active_predictions[label]
        ):
            return await interaction.response.send_message(
                f"❌ No predictions found for **{target.display_name.upper()}** in **{label}**.",
                ephemeral=True,
            )
        pred = active_predictions[label][target.id]
        embed = discord.Embed(
            title=f"🔒 Picks for {pred['user']} — {label}",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="🪙 Toss Winner", value=pred["toss"], inline=True)
        embed.add_field(name="🏆 Match Winner", value=pred["match"], inline=True)
        embed.add_field(name="🏏 Best Batter", value=pred["batter"], inline=True)
        embed.add_field(name="🎳 Best Bowler", value=pred["bowler"], inline=True)
        embed.add_field(name="⭐ MOTM", value=pred["motm"], inline=True)
        embed.set_footer(text="Only you can see this message")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @b.tree.command(
        name="standings", description="View current IPL prediction standings"
    )
    async def standings(interaction: discord.Interaction):
        if not leaderboard:
            return await interaction.response.send_message(
                "📋 No players on the board yet.", ephemeral=True
            )
        sorted_board = sorted(leaderboard.items(), key=lambda x: x[1], reverse=True)
        medals = ["🥇", "🥈", "🥉"]
        embed = discord.Embed(
            title="🏏 IPL 2026 Prediction Standings", color=discord.Color.gold()
        )
        lines = ""
        for i, (name, score) in enumerate(sorted_board):
            prefix = medals[i] if i < 3 else f"`{i + 1}.`"
            lines += f"{prefix} **{name}** — `{score} pts`\n"
        embed.description = lines
        embed.set_footer(
            text="10 pts per correct pick • 5 picks per match • 50 pts max"
        )
        await interaction.response.send_message(embed=embed)

    @b.tree.command(name="help", description="How to use the IPL prediction bot")
    async def help_command(interaction: discord.Interaction):
        embed = discord.Embed(
            title="🏏 IPL Prediction Bot — How To Play", color=discord.Color.gold()
        )
        embed.add_field(
            name="📌 How It Works",
            value=(
                "Each correct prediction earns you **10 points**.\n"
                "5 things to predict = up to **50 points per match**.\n"
                "Predictions are **locked once submitted** — no edits!\n"
                "Use `match_number: 1` or `2` for double match days."
            ),
            inline=False,
        )
        embed.add_field(
            name="🟢 Single Match Day",
            value=(
                "`/predict match_number:1 toss_winner:CSK match_winner:CSK best_batter:Kohli best_bowler:Bumrah motm:Kohli`\n"
                "`/reveal match_number:1`\n"
                "`/settle_match match_number:1 toss:CSK match:CSK batter:Kohli bowler:Bumrah motm:Kohli`"
            ),
            inline=False,
        )
        embed.add_field(
            name="🟡 Double Match Day",
            value=(
                "Submit `/predict match_number:1 ...` and `/predict match_number:2 ...` separately.\n"
                "Reveal and settle each match independently."
            ),
            inline=False,
        )
        embed.add_field(
            name="📋 Commands",
            value=(
                "`/predict` `/reveal` `/settle_match` `/leaderboard` `/set_reveal_time` `/help`"
            ),
            inline=False,
        )
        embed.set_footer(text="Good luck! May the best predictor win 🏆")
        await interaction.response.send_message(embed=embed)

    @b.tree.command(
        name="predict_for", description="Admin: Submit predictions on behalf of someone"
    )
    async def predict_for(
        interaction: discord.Interaction,
        member: discord.Member,
        match_number: str,
        toss_winner: str,
        match_winner: str,
        best_batter: str,
        best_bowler: str,
        motm: str,
    ):
        if interaction.user.id != ADMIN_ID:
            return await interaction.response.send_message(
                "❌ Only the admin can use this command.", ephemeral=True
            )
        label = f"MATCH {match_number.strip()}"
        user_name = member.display_name.upper()
        if label not in active_predictions:
            active_predictions[label] = {}
        if member.id in active_predictions[label]:
            return await interaction.response.send_message(
                f"❌ **{user_name}** already has picks locked for **{label}**. Use `/settle_match` to clear them first.",
                ephemeral=True,
            )
        active_predictions[label][member.id] = {
            "user": user_name,
            "toss": toss_winner.upper(),
            "match": match_winner.upper(),
            "batter": best_batter.title(),
            "bowler": best_bowler.title(),
            "motm": motm.title(),
        }
        save_data()
        await interaction.response.send_message(
            f"✅ Picks locked for **{user_name}** in **{label}**!\n"
            f"🪙 Toss: `{toss_winner.upper()}` | 🏆 Win: `{match_winner.upper()}` | "
            f"🏏 Bat: `{best_batter.title()}` | 🎳 Bowl: `{best_bowler.title()}` | ⭐ MOTM: `{motm.title()}`",
            ephemeral=True,
        )

    @b.tree.command(
        name="remove_prediction",
        description="Admin: Remove someone's locked prediction so they can re-submit",
    )
    async def remove_prediction(
        interaction: discord.Interaction, member: discord.Member, match_number: str
    ):
        if interaction.user.id != 1001845425185230990:
            return await interaction.response.send_message(
                "❌ You don't have permission to use this command.", ephemeral=True
            )
        label = f"MATCH {match_number.strip()}"
        if (
            label not in active_predictions
            or member.id not in active_predictions[label]
        ):
            return await interaction.response.send_message(
                f"❌ No prediction found for **{member.display_name.upper()}** in **{label}**.",
                ephemeral=True,
            )
        removed = active_predictions[label].pop(member.id)
        if not active_predictions[label]:
            del active_predictions[label]
        save_data()
        await interaction.response.send_message(
            f"🗑️ Removed **{removed['user']}**'s picks for **{label}**. They can now re-submit with `/predict`.",
            ephemeral=True,
        )

    # Load any custom commands added via the dashboard
    try:
        with open("custom_commands.py", "r") as f:
            code = f.read()
        if code.strip():
            exec(
                code,
                {
                    "bot": b,
                    "discord": discord,
                    "app_commands": app_commands,
                    "leaderboard": leaderboard,
                    "active_predictions": active_predictions,
                    "ADMIN_ID": ADMIN_ID,
                    "POINTS_PER_HIT": POINTS_PER_HIT,
                    "save_data": save_data,
                },
            )
    except FileNotFoundError:
        pass


def _bot_thread_func():
    global _bot_loop, _bot, _bot_running
    _bot_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_bot_loop)
    _bot = IPLBot()
    setup_commands(_bot)
    try:
        _bot_loop.run_until_complete(_bot.start(TOKEN))
    except Exception as e:
        print(f"Bot error: {e}")
    finally:
        _bot_running = False
        try:
            _bot_loop.close()
        except Exception:
            pass


def start_bot():
    global _bot_thread
    if _bot_running:
        return
    _bot_thread = Thread(target=_bot_thread_func, daemon=True)
    _bot_thread.start()


def stop_bot():
    global _bot, _bot_loop
    if not _bot_running or not _bot_loop or not _bot:
        return
    future = asyncio.run_coroutine_threadsafe(_bot.close(), _bot_loop)
    try:
        future.result(timeout=10)
    except Exception:
        pass


# ---------------------------------------------------------------
# 4. DASHBOARD HTML
# ---------------------------------------------------------------
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>HOMIES BOT — Dashboard</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: 'Segoe UI', sans-serif; background: #0d0d1a; color: #e2e8f0; min-height: 100vh; }

  /* LOGIN */
  .login-wrap { display: flex; align-items: center; justify-content: center; min-height: 100vh; }
  .login-card { background: #1a1a2e; border: 1px solid #2d2d4e; border-radius: 16px; padding: 48px 40px; width: 360px; text-align: center; }
  .login-card .logo { font-size: 2.5rem; margin-bottom: 8px; }
  .login-card h1 { font-size: 1.5rem; margin-bottom: 4px; }
  .login-card p { color: #64748b; font-size: 0.9rem; margin-bottom: 32px; }
  .login-card input { width: 100%; padding: 12px 16px; background: #0d0d1a; border: 1px solid #2d2d4e; border-radius: 8px; color: #e2e8f0; font-size: 1rem; margin-bottom: 12px; outline: none; }
  .login-card input:focus { border-color: #4f46e5; }
  .login-card .btn-login { width: 100%; padding: 12px; background: #4f46e5; border: none; border-radius: 8px; color: #fff; font-size: 1rem; font-weight: 600; cursor: pointer; }
  .login-card .btn-login:hover { background: #4338ca; }
  .error-msg { background: #450a0a; color: #f87171; padding: 10px 14px; border-radius: 8px; margin-bottom: 16px; font-size: 0.875rem; }

  /* HEADER */
  header { background: #1a1a2e; border-bottom: 1px solid #2d2d4e; padding: 16px 32px; display: flex; justify-content: space-between; align-items: center; }
  header h1 { font-size: 1.25rem; font-weight: 700; }
  header a { color: #64748b; text-decoration: none; font-size: 0.875rem; }
  header a:hover { color: #e2e8f0; }

  /* DASHBOARD LAYOUT */
  .dashboard { display: grid; grid-template-columns: 300px 1fr; gap: 24px; padding: 32px; max-width: 1100px; }

  /* CARDS */
  .card { background: #1a1a2e; border: 1px solid #2d2d4e; border-radius: 16px; padding: 28px; }
  .card-title { font-size: 0.75rem; font-weight: 700; color: #64748b; text-transform: uppercase; letter-spacing: 0.1em; margin-bottom: 24px; }

  /* STATUS */
  .status-center { display: flex; flex-direction: column; align-items: center; gap: 12px; margin-bottom: 28px; }
  .pulse-ring { position: relative; width: 80px; height: 80px; }
  .pulse-dot { width: 80px; height: 80px; border-radius: 50%; }
  .pulse-dot.online { background: #22c55e; box-shadow: 0 0 0 0 rgba(34,197,94,0.5); animation: pulse-green 2s infinite; }
  .pulse-dot.offline { background: #ef4444; }
  @keyframes pulse-green { 0%{box-shadow:0 0 0 0 rgba(34,197,94,0.5)} 70%{box-shadow:0 0 0 14px rgba(34,197,94,0)} 100%{box-shadow:0 0 0 0 rgba(34,197,94,0)} }
  .status-label { font-size: 1rem; font-weight: 700; }
  .status-label.online { color: #22c55e; }
  .status-label.offline { color: #ef4444; }

  .btn { width: 100%; padding: 12px 16px; border: none; border-radius: 10px; font-size: 0.95rem; font-weight: 600; cursor: pointer; margin-bottom: 10px; transition: opacity 0.15s, transform 0.1s; }
  .btn:hover { opacity: 0.88; transform: translateY(-1px); }
  .btn:active { transform: translateY(0); }
  .btn-green { background: #16a34a; color: #fff; }
  .btn-red { background: #dc2626; color: #fff; }
  .btn-indigo { background: #4f46e5; color: #fff; }

  /* EDITOR */
  .editor-label { font-size: 0.85rem; color: #94a3b8; margin-bottom: 8px; }
  textarea { width: 100%; height: 300px; background: #0d0d1a; border: 1px solid #2d2d4e; border-radius: 10px; color: #4ade80; font-family: 'Courier New', monospace; font-size: 0.875rem; padding: 16px; resize: vertical; margin-bottom: 16px; outline: none; line-height: 1.6; }
  textarea:focus { border-color: #4f46e5; }

  .toast { padding: 10px 16px; border-radius: 8px; font-size: 0.875rem; margin-bottom: 16px; display: none; }
  .toast.success { background: #14532d; color: #4ade80; display: block; }
  .toast.error { background: #450a0a; color: #f87171; display: block; }
  .toast.info { background: #1e3a5f; color: #93c5fd; display: block; }

  .hint { font-size: 0.8rem; color: #475569; margin-top: 8px; line-height: 1.5; }
</style>
</head>
<body>

{% if page == 'login' %}
<div class="login-wrap">
  <div class="login-card">
    <div class="logo">🏏</div>
    <h1>HOMIES BOT</h1>
    <p>Admin Dashboard</p>
    {% if error %}<div class="error-msg">{{ error }}</div>{% endif %}
    <form method="POST">
      <input type="password" name="password" placeholder="Enter your password" autofocus>
      <button type="submit" class="btn-login">Login</button>
    </form>
  </div>
</div>

{% else %}
<header>
  <h1>🏏 HOMIES BOT — Control Panel</h1>
  <a href="/logout">Logout →</a>
</header>

<div class="dashboard">

  <!-- Status Panel -->
  <div class="card">
    <div class="card-title">Bot Status</div>
    <div class="status-center">
      <div class="pulse-dot {{ 'online' if bot_running else 'offline' }}" id="dot"></div>
      <span class="status-label {{ 'online' if bot_running else 'offline' }}" id="status-label">
        {{ '● ONLINE' if bot_running else '● OFFLINE' }}
      </span>
    </div>
    <button class="btn btn-green" onclick="toggleBot('start')">▶  Go Online</button>
    <button class="btn btn-red" onclick="toggleBot('stop')">⏹  Go Offline</button>
  </div>

  <!-- Command Editor -->
  <div class="card">
    <div class="card-title">Add New Command</div>
    <div id="toast" class="toast"></div>
    <div class="editor-label">Paste your Python command code below:</div>
    <textarea id="code" placeholder='@bot.tree.command(name="hello", description="Say hello")
async def hello(interaction: discord.Interaction):
    await interaction.response.send_message("Hello homies!")'></textarea>
    <button class="btn btn-indigo" onclick="addCommand()">➕  Add Command &amp; Restart Bot</button>
    <div class="hint">
      Available in your code: <code>bot</code>, <code>discord</code>, <code>app_commands</code>,
      <code>leaderboard</code>, <code>active_predictions</code>, <code>ADMIN_ID</code>, <code>save_data()</code>
    </div>
  </div>

</div>

<script>
  function showToast(msg, type) {
    const t = document.getElementById('toast');
    t.className = 'toast ' + type;
    t.innerText = msg;
    if (type !== 'info') setTimeout(() => { t.className = 'toast'; }, 5000);
  }

  async function toggleBot(action) {
    showToast(action === 'start' ? '⏳ Starting bot...' : '⏳ Stopping bot...', 'info');
    try {
      await fetch('/api/toggle', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ action }) });
      setTimeout(updateStatus, 3000);
    } catch(e) { showToast('❌ Request failed.', 'error'); }
  }

  async function updateStatus() {
    try {
      const res = await fetch('/api/status');
      const data = await res.json();
      const dot = document.getElementById('dot');
      const label = document.getElementById('status-label');
      dot.className = 'pulse-dot ' + (data.running ? 'online' : 'offline');
      label.className = 'status-label ' + (data.running ? 'online' : 'offline');
      label.innerText = data.running ? '● ONLINE' : '● OFFLINE';
      const t = document.getElementById('toast');
      if (t.classList.contains('info')) t.className = 'toast';
    } catch(e) {}
  }

  async function addCommand() {
    const code = document.getElementById('code').value.trim();
    if (!code) { showToast('❌ Please enter some command code first.', 'error'); return; }
    showToast('⏳ Saving command and restarting bot... this takes a few seconds.', 'info');
    try {
      const res = await fetch('/api/add_command', {
        method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ code })
      });
      const data = await res.json();
      if (data.success) {
        document.getElementById('code').value = '';
        showToast('✅ Command added! Bot is coming back online...', 'success');
        setTimeout(updateStatus, 5000);
      } else {
        showToast('❌ Error: ' + (data.error || 'Unknown error'), 'error');
      }
    } catch(e) { showToast('❌ Request failed.', 'error'); }
  }

  setInterval(updateStatus, 8000);
</script>
{% endif %}
</body>
</html>"""

# ---------------------------------------------------------------
# 5. FLASK APP
# ---------------------------------------------------------------
app = Flask("")
app.secret_key = os.environ.get("SECRET_KEY", "homies-bot-dashboard-9x72k")


@app.route("/")
def home():
    return redirect("/dashboard")


@app.route("/ping")
def ping():
    return "OK", 200


@app.route("/dashboard", methods=["GET", "POST"])
def dashboard():
    if request.method == "POST" and "password" in request.form:
        if request.form["password"] == DASHBOARD_PASSWORD:
            session["auth"] = True
            return redirect("/dashboard")
        else:
            return render_template_string(
                DASHBOARD_HTML, page="login", error="Wrong password — try again."
            )
    if not session.get("auth"):
        return render_template_string(DASHBOARD_HTML, page="login", error=None)
    return render_template_string(
        DASHBOARD_HTML, page="dashboard", bot_running=_bot_running
    )


@app.route("/logout")
def logout():
    session.pop("auth", None)
    return redirect("/dashboard")


@app.route("/api/status")
def api_status():
    if not session.get("auth"):
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({"running": _bot_running})


@app.route("/api/toggle", methods=["POST"])
def api_toggle():
    if not session.get("auth"):
        return jsonify({"error": "unauthorized"}), 401
    if _bot_running:
        Thread(target=stop_bot, daemon=True).start()
        return jsonify({"action": "stopping"})
    else:
        start_bot()
        return jsonify({"action": "starting"})


@app.route("/api/add_command", methods=["POST"])
def api_add_command():
    if not session.get("auth"):
        return jsonify({"error": "unauthorized"}), 401
    code = request.json.get("code", "").strip()
    if not code:
        return jsonify({"error": "No code provided"}), 400
    try:
        with open("custom_commands.py", "a") as f:
            f.write("\n\n" + code)
        if _bot_running:
            stop_bot()
            import time

            time.sleep(2)
        start_bot()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------
# 6. START (MODIFIED FOR RENDER)
# ---------------------------------------------------------------
if __name__ == "__main__":
    # Start the Discord bot thread
    start_bot()
    
    # Render assigns a port via the PORT environment variable
    # We default to 8080 if not found for local testing
    port = int(os.environ.get("PORT", 8080))
    
    # Run Flask
    # host="0.0.0.0" is required for external access on Render
    app.run(host="0.0.0.0", port=port)