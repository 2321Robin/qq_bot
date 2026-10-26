# startup.example.ps1 — Local startup configuration template
#
# Copy this file to startup.local.ps1 in the same directory, fill in your
# local values, and run start_all.ps1 / stop_all.ps1.
#
# Do NOT edit this example file with real values.
# Do NOT commit startup.local.ps1 — it is gitignored.

$StartupConfig = @{
    # Protocol implementation to run: "napcat" (default) or "snowluma".
    # Omit the key entirely to keep NapCat behavior.
    Protocol      = "napcat"

    # ---- NapCat mode (Protocol = "napcat") ----

    # Full path to the NapCatQQ installation directory.
    # Must contain NapCatWinBootMain.exe.
    NapCatDir     = ""

    # QQ account number for NapCat to log into.
    NapCatAccount = ""

    # ---- SnowLuma mode (Protocol = "snowluma") ----

    # Full path to the SnowLuma extraction directory.
    # Must contain index.mjs. The full Windows bundle ships node.exe next to
    # it; a Lite bundle falls back to Node.js 24+ from PATH.
    SnowLumaDir   = ""

    # ---- Common ----

    # Port the bot backend listens on. Must match PORT in .env.
    # Valid range: 1..65535
    BotPort       = 8081
}
