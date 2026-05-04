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