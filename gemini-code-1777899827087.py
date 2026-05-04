import discord
from discord import app_commands

# --- 1. REMOVE OLD COMMANDS ---
# This clears the existing /my_predictions so we can replace it
bot.tree.remove_command("my_predictions")

# --- 2. ADD UPDATED PRIVATE PREDICTIONS ---
@bot.tree.command(
    name="my_predictions",
    description="PRIVATE: See only your own picks"
)
async def my_predictions(interaction: discord.Interaction, match_number: str):
    target = interaction.user
    label = f"MATCH {match_number.strip()}"
    
    if label not in active_predictions or target.id not in active_predictions[label]:
        return await interaction.response.send_message(
            f"❌ No predictions found for you in **{label}**.",
            ephemeral=True
        )
        
    pred = active_predictions[label][target.id]
    embed = discord.Embed(
        title=f"🔒 Your Picks — {label}",
        color=discord.Color.blue()
    )
    embed.add_field(name="🪙 Toss", value=pred["toss"], inline=True)
    embed.add_field(name="🏆 Match", value=pred["match"], inline=True)
    embed.add_field(name="🏏 Batter", value=pred["batter"], inline=True)
    embed.add_field(name="🎳 Bowler", value=pred["bowler"], inline=True)
    embed.add_field(name="⭐ MOTM", value=pred["motm"], inline=True)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

# --- 3. ADD END SEASON COMMAND ---
@bot.tree.command(
    name="end_season",
    description="Finalize the tournament and announce winners"
)
async def end_season(interaction: discord.Interaction):
    # Only the Admin (You) can run this
    if interaction.user.id != ADMIN_ID:
        return await interaction.response.send_message("❌ Admin only!", ephemeral=True)

    if not leaderboard:
        return await interaction.response.send_message("❌ No data in leaderboard.")

    # Sort leaderboard by points
    sorted_users = sorted(leaderboard.items(), key=lambda x: x[1], reverse=True)
    
    embed = discord.Embed(
        title="🎊 IPL 2026 TOURNAMENT FINALE 🎊",
        description="The season has ended! Here are our champions:",
        color=discord.Color.gold()
    )

    # Top 3 Logic
    ranks = ["🥇 1st Place", "🥈 2nd Place", "🥉 3rd Place"]
    for i in range(min(3, len(sorted_users))):
        user_id, points = sorted_users[i]
        user_obj = bot.get_user(int(user_id))
        name = user_obj.name if user_obj else f"User {user_id}"
        
        embed.add_field(name=ranks[i], value=f"**{name}**\nScore: {points} pts", inline=False)
        
        # Set the Champion's photo as the main image
        if i == 0 and user_obj:
            embed.set_thumbnail(url=user_obj.display_avatar.url)

    await interaction.response.send_message(embed=embed)
    
    # Optional: Clear data for next season
    # leaderboard.clear()
    # save_data()