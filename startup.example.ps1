# startup.example.ps1 — Local startup configuration template
#
# Copy this file to startup.local.ps1 in the same directory, fill in your
# local values, and run start_all.ps1 / stop_all.ps1.
#
# Do NOT edit this example file with real values.
# Do NOT commit startup.local.ps1 — it is gitignored.

$StartupConfig = @{
    # Full path to the NapCatQQ installation directory.
    # Must contain NapCatWinBootMain.exe.
    NapCatDir     = ""

    # QQ account number for NapCat to log into.
    NapCatAccount = ""

    # Port the bot backend listens on. Must match PORT in .env.
    # Valid range: 1..65535
    BotPort       = 8081
}
