CodeMonkey for Linux and Raspberry Pi
=====================================

Run it:             ./start.sh
Add it to the menu: ./install.sh

It needs Python 3.10 or newer (Raspberry Pi OS 12 and Ubuntu 22.04 or later have it):
    sudo apt install python3

Your sessions and settings are kept in ~/.config/CodeMonkey, so a newer version can be
unpacked anywhere and keeps them. Delete that folder to start over.

Saved API keys go to the system keyring (GNOME Keyring or KWallet) through secret-tool:
    sudo apt install libsecret-tools
Without it, give keys as environment variables instead (Settings > Models shows their names).

On Ubuntu 24.04 and later, Chromium's sandbox needs this once (the launcher otherwise runs
without it):
    sudo chown root:root chrome-sandbox && sudo chmod 4755 chrome-sandbox

Raspberry Pi: a Pi 4 or 5 is best. Local models need a lot of memory, so on a Pi a model on
another computer (Settings > Models) or an online provider works better.
